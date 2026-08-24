"""Continuous, layered movement.

The firmware moves a servo as fast as it can, so smoothing belongs here, where
there are floats and a clock. The engine holds a pose model, ticks at 20Hz, and
sends only the joints whose value changed. Layers publish desired values and are
resolved per joint by priority, so no layer needs to know about any other.

Everything sent is ABSOLUTE. Not because relative drifts on the Arduino - it
does not - but because an absolute percentage is the only form whose outcome the
host can predict without a copy of the firmware's degree table, and because a
relative step under one degree of travel is silently dropped.

There is deliberately no idle layer, and nothing here self-initiates: every
movement traces back to something that asked for it.

The reasoning and the measurements behind all of this are in MOTION-DESIGN.md.
"""

import os
import random
import re
import threading
import time

# The serial round trip costs about 32ms measured on the robot, so a tick
# cannot be shorter than that. 20Hz gives a 50ms budget and leaves roughly
# 18ms of headroom for jitter; 25Hz would leave 8ms and overrun regularly.
# Splitting the serial reader from the writer removes the send from this
# budget entirely, and 25 to 30Hz becomes reasonable then.
TICK_HZ = float(20)
TICK_SECONDS = 1.0 / TICK_HZ

# A command line holds 31 usable characters, and the Arduino's receive buffer
# is 64 bytes. Two lines of ordinary length fit comfortably inside both; a
# third would risk overrunning the buffer while the sketch is still parsing.
MAX_LINE_CHARS = 31
MAX_PAYLOAD_BYTES = 60

# Joints the engine is allowed to drive. The mouth is deliberately absent: the
# speech path opens and closes it directly, and until there is a speech layer
# with an audio envelope to follow, the engine would only fight it.
DRIVEN_JOINTS = ("HH", "HV", "RB", "RW", "RT", "RI", "RM", "RR", "RP")

# Where a joint is assumed to be when nothing has ever positioned it. The
# firmware makes the same assumption for its first relative move.
UNKNOWN_POSITION = 50.0

# How long a stop holds off new movement. It has to be a cooldown rather than a
# latch: a brake that had to be released by hand would leave Gary answering
# questions without moving at all, with nothing to say why, which is
# indistinguishable from a hardware fault. Nothing has to clear this, so it
# cannot get stuck. Long enough to abort a two-second body reset and react;
# `systemctl --user stop gary-servo` is the control for "do not move while I
# work on you".
STOP_COOLDOWN = 3.0


class Suppressed(Exception):
    """Raised when a stop is still in force, carrying the seconds left."""

    def __init__(self, remaining):
        self.remaining = remaining
        Exception.__init__(self,
                           "movement stopped, %.1fs left" % remaining)


def linear(t):
    return t


def ease_in_out(t):
    """Smoothstep. A linear ramp still reads as mechanical; this reads as
    deliberate, which is most of the difference between the two."""
    return t * t * (3.0 - 2.0 * t)


EASINGS = {"linear": linear, "ease_in_out": ease_in_out}


class Pose(object):
    """Where the engine believes each joint is, as a percentage of its range.

    Commanded, never measured: hobby servos report nothing back.
    """

    def __init__(self):
        self._values = {}
        self._lock = threading.Lock()

    def get(self, code):
        with self._lock:
            value = self._values.get(code)
        return UNKNOWN_POSITION if value is None else value

    def known(self, code):
        with self._lock:
            return self._values.get(code) is not None

    def set(self, code, value):
        with self._lock:
            self._values[code] = float(value)

    def forget(self, code):
        """Mark a joint's position unknown, after something moved it relatively."""
        with self._lock:
            self._values[code] = None

    def snapshot(self):
        with self._lock:
            return dict(self._values)


class Gesture(object):
    """A movement as keyframes: (seconds, {joint: value}).

    Each joint is interpolated only across the frames that name it, and one
    first mentioned after zero starts from wherever it already is.
    """

    def __init__(self, keyframes, easing="ease_in_out", name=None):
        if not keyframes:
            raise ValueError("a gesture needs at least one keyframe")
        self.name = name
        self.easing = EASINGS.get(easing, ease_in_out)
        self.keyframes = sorted(keyframes, key=lambda kf: kf[0])
        for at, joints in self.keyframes:
            if at < 0:
                raise ValueError("keyframe time cannot be negative")
            for code in joints:
                if code not in DRIVEN_JOINTS:
                    raise ValueError("the engine does not drive %s" % code)
        self.duration = self.keyframes[-1][0]
        self.joints = set()
        for _, joints in self.keyframes:
            self.joints.update(joints)

    def _track(self, code, start_values):
        """The (time, value) points for one joint, earliest first."""
        points = [(at, joints[code]) for at, joints in self.keyframes
                  if code in joints]
        if points[0][0] > 0:
            if code in start_values:
                start = start_values[code]
            else:
                # Nothing has positioned this joint since the service started,
                # and the firmware positions nothing at boot - checked on the
                # robot. Easing from an assumed 50 would command 50 on the way
                # to the target and move a joint that may already have been
                # where it needed to be: a closed hand would half-open before
                # closing again. Start at the target instead and go straight
                # there.
                start = points[0][1]
            points.insert(0, (0.0, start))
        return points

    def value_at(self, elapsed, start_values):
        """Desired values at `elapsed` seconds in. Holds the last frame after."""
        out = {}
        for code in self.joints:
            points = self._track(code, start_values)
            if elapsed <= points[0][0]:
                out[code] = points[0][1]
                continue
            if elapsed >= points[-1][0]:
                out[code] = points[-1][1]
                continue
            for i in range(len(points) - 1):
                t0, v0 = points[i]
                t1, v1 = points[i + 1]
                if t0 <= elapsed <= t1:
                    span = t1 - t0
                    # Coincident keyframes are a step change, not a divide by
                    # zero.
                    fraction = 1.0 if span <= 0 else (elapsed - t0) / span
                    out[code] = v0 + (v1 - v0) * self.easing(fraction)
                    break
        return out


# A keyframe value as written in a gesture file.
#
#   "50"          absolute: 50% of that joint's travel
#   "+7" / "-7"   relative: exactly 7% on from where the gesture started
#   "+5..10"      relative: a random 5 to 10% on
#   "+/-5..10"    relative: 5 to 10% in a direction chosen at random
#
# Relative is measured from the gesture's start, not the previous keyframe, so
# "+0" means back where it began. A range is drawn once per play.
VALUE_RE = re.compile(r"^(?P<sign>\+/-|\+|-)?(?P<low>\d{1,3})"
                      r"(?:\.\.(?P<high>\d{1,3}))?$")


def parse_value(raw):
    """One written keyframe value as (kind, low, high, direction).

    JSON numbers are absolute. A YAML file must quote every value: unquoted
    `+7` parses as the integer 7, an absolute position near the bottom of the
    joint, which is why `load_library` refuses bare numbers.
    """
    if isinstance(raw, bool):
        raise ValueError("a keyframe value cannot be true or false")
    if isinstance(raw, (int, float)):
        value = float(raw)
        if not 0 <= value <= 100:
            raise ValueError("absolute value %g is outside 0-100" % value)
        return ("absolute", value, value, 1)

    match = VALUE_RE.match(str(raw).strip())
    if match is None:
        raise ValueError('cannot read the value %r - expected "50", "+7", '
                         '"+5..10" or "+/-5..10"' % raw)

    low = int(match.group("low"))
    high = int(match.group("high")) if match.group("high") else low
    if high < low:
        raise ValueError("the range %r runs backwards" % raw)
    if high > 100:
        raise ValueError("%r is more than the whole range of travel" % raw)

    sign = match.group("sign")
    if sign is None:
        if high != low:
            raise ValueError("%r is a range with no direction - write "
                             '"+%d..%d" or "+/-%d..%d"' % (raw, low, high,
                                                           low, high))
        return ("absolute", float(low), float(low), 1)

    return ("relative", float(low), float(high),
            0 if sign == "+/-" else (1 if sign == "+" else -1))


def resolve_value(spec, start):
    """One concrete percentage, drawing a range and a direction if asked."""
    kind, low, high, direction = spec
    if kind == "absolute":
        return low
    magnitude = low if high == low else random.uniform(low, high)
    if direction == 0:
        magnitude *= random.choice((-1.0, 1.0))
    else:
        magnitude *= direction
    return max(0.0, min(100.0, start + magnitude))


class GestureSpec(object):
    """A gesture as written down, before it is aimed at anything.

    Values may be relative or random, so resolving against the current pose is
    what produces a playable `Gesture` of absolute numbers. Each play resolves
    again, which is how one written gesture varies.
    """

    def __init__(self, keyframes, easing="ease_in_out", name=None):
        if not keyframes:
            raise ValueError("a gesture needs at least one keyframe")
        self.name = name
        self.easing = easing
        parsed = []
        for at, joints in keyframes:
            at = float(at)
            if at < 0:
                raise ValueError("keyframe time cannot be negative")
            if not joints:
                raise ValueError("the keyframe at %g moves nothing" % at)
            values = {}
            for code, raw in joints.items():
                code = str(code).upper()
                if code not in DRIVEN_JOINTS:
                    raise ValueError("the engine does not drive %s" % code)
                try:
                    values[code] = parse_value(raw)
                except ValueError as e:
                    raise ValueError("%s at %gs: %s" % (code, at, e))
            parsed.append((at, values))
        self.keyframes = sorted(parsed, key=lambda kf: kf[0])
        self.duration = self.keyframes[-1][0]
        self.joints = set()
        for _, values in self.keyframes:
            self.joints.update(values)

    def resolve(self, start_values):
        """A playable Gesture, aimed at where the joints are now."""
        # Drawn once here rather than per keyframe, so a random magnitude is
        # consistent across the whole movement - otherwise a nod would dip a
        # different distance than it rose.
        drawn = {}
        frames = []
        for at, values in self.keyframes:
            frame = {}
            for code, spec in values.items():
                start = start_values.get(code, UNKNOWN_POSITION)
                key = (code, spec)
                if spec[0] == "relative" and spec[1] != spec[2]:
                    # Same spec on the same joint draws once for the gesture.
                    if key not in drawn:
                        drawn[key] = resolve_value(spec, start)
                    frame[code] = drawn[key]
                else:
                    frame[code] = resolve_value(spec, start)
            frames.append((at, frame))
        return Gesture(frames, easing=self.easing, name=self.name)


def load_library(path):
    """Read gesture specs from a YAML file, as (specs, problems).

    A missing file or missing PyYAML is a reported problem rather than an error,
    and one gesture that will not parse is skipped rather than taking the rest
    of the file with it.
    """
    problems = []
    try:
        import yaml
    except ImportError:
        return {}, ["PyYAML is not installed, so no named gestures were loaded"]

    if not os.path.exists(path):
        return {}, ["no gesture file at %s" % path]

    try:
        with open(path) as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception as e:
        return {}, ["could not read %s: %s" % (path, e)]

    if not isinstance(raw, dict):
        return {}, ["%s should be a mapping of gesture names" % path]

    specs = {}
    for name, body in raw.items():
        try:
            if not isinstance(body, dict):
                raise ValueError("should be a mapping with a keyframes list")
            frames = body.get("keyframes")
            if not isinstance(frames, list) or not frames:
                raise ValueError("needs a non-empty keyframes list")
            keyframes = []
            for frame in frames:
                if not isinstance(frame, dict) or "at" not in frame:
                    raise ValueError("every keyframe needs an 'at' time")
                joints = dict((k, v) for k, v in frame.items() if k != "at")
                for code, value in joints.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        raise ValueError(
                            '%s: write values in quotes - "%s" for an absolute '
                            'position, "+%s" to nudge. Unquoted, YAML reads it '
                            "as a number and it becomes an absolute position"
                            % (code, value, value))
                keyframes.append((frame["at"], joints))
            specs[str(name)] = GestureSpec(
                keyframes, easing=str(body.get("easing", "ease_in_out")),
                name=str(name))
        except (ValueError, TypeError) as e:
            problems.append("%s: %s" % (name, e))

    return specs, problems


class Layer(object):
    priority = 0
    name = "layer"

    def desired(self, now):
        """What this layer wants, as {joint: value}. Empty means it is not
        claiming anything, which is how a joint is handed back."""
        return {}


class GestureLayer(Layer):
    """Plays one gesture at a time.

    A new gesture replaces the running one from wherever the joints reached, so
    interrupting does not jump.
    """

    priority = 30
    name = "gesture"

    def __init__(self, pose):
        self._pose = pose
        self._lock = threading.Lock()
        self._gesture = None
        self._started_at = 0.0
        self._start_values = {}

    def play(self, gesture, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            # Where the joints are now, not where the previous gesture began.
            # Joints whose position is unknown are left out rather than filled
            # in with the 50 that `pose.get` would return, so `_track` can tell
            # "at 50" from "no idea" and jump instead of easing from a fiction.
            self._start_values = dict(
                (code, self._pose.get(code)) for code in gesture.joints
                if self._pose.known(code))
            self._gesture = gesture
            self._started_at = now

    def stop(self):
        with self._lock:
            self._gesture = None

    def current(self):
        """The running gesture's name, or None if nothing is running.

        Falls back to "unnamed" rather than None for a gesture built without
        one, so `state()` can never report nothing playing while something is.
        """
        with self._lock:
            if self._gesture is None:
                return None
            return self._gesture.name or "unnamed"

    def target_of(self, code):
        """Where a running gesture is taking this joint, or None.

        Nudges resolve against this rather than the live position, so five
        quick arrow presses move five full steps instead of however far the
        easing happened to have reached.
        """
        with self._lock:
            gesture = self._gesture
            if gesture is None or code not in gesture.joints:
                return None
            # The last keyframe naming this joint is where it ends up.
            for _, joints in reversed(gesture.keyframes):
                if code in joints:
                    return joints[code]
            return None

    def desired(self, now):
        with self._lock:
            gesture = self._gesture
            if gesture is None:
                return {}
            elapsed = now - self._started_at
            values = gesture.value_at(elapsed, self._start_values)
            if elapsed >= gesture.duration:
                # Done: hand the joints back after publishing the final frame
                # once, so the last keyframe is actually reached.
                self._gesture = None
            return values


class MotionEngine(object):
    """Ticks, resolves the layers, and sends what changed.

    `send` takes a list of command lines and is injected, so this module knows
    nothing about serial ports.
    """

    def __init__(self, send, tick_seconds=TICK_SECONDS):
        self._send = send
        self._tick_seconds = tick_seconds
        self.pose = Pose()
        self.gesture = GestureLayer(self.pose)
        self._layers = [self.gesture]

        self._suppress_until = 0.0
        self._sent = {}       # joint -> the integer last put on the wire
        # Joint -> a value that was wanted but has not reached the wire yet,
        # because it did not fit in a payload or the send failed. Carried
        # forward independently of the layers: a gesture stops claiming its
        # joints the moment it ends, so without this its final keyframe would
        # be stranded for any joint that was deferred on that last tick.
        self._pending = {}
        self._thread = None
        self._running = False

        self.ticks = 0
        self.sends = 0
        self.overruns = 0
        self.last_error = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="motion")
        self._thread.daemon = True
        self._thread.start()

    def shutdown(self):
        self._running = False

    def _loop(self):
        deadline = time.monotonic()
        while self._running:
            deadline += self._tick_seconds
            try:
                self.tick()
            except Exception as e:
                # A tick that raises must not take the loop with it, or one bad
                # gesture leaves Gary frozen mid-movement.
                self.last_error = "%s" % e
            now = time.monotonic()
            if now > deadline:
                # Overran. Skip ahead rather than catching up, which would fire
                # a burst of sends at a port that is already the bottleneck.
                self.overruns += 1
                deadline = now
            else:
                time.sleep(deadline - now)

    # -- commands ----------------------------------------------------------

    def suppressed(self, now=None):
        """Seconds of stop cooldown remaining, or 0.0."""
        now = time.monotonic() if now is None else now
        return max(0.0, self._suppress_until - now)

    def _refuse_if_stopped(self):
        remaining = self.suppressed()
        if remaining:
            raise Suppressed(remaining)

    def play(self, gesture):
        # Deliberately does not clear the cooldown. Letting the next request
        # cancel it is what made the old stop useless: the brain firing
        # head_recenter a moment later would restart the very movement that
        # had just been stopped.
        self._refuse_if_stopped()
        self.gesture.play(gesture)

    def play_spec(self, spec):
        """Aim a written-down gesture at where the joints are now, and play it.

        Resolving here keeps the random draw and the pose lookup together, so
        the layer only ever handles absolute numbers.
        """
        self._refuse_if_stopped()
        # Unknown joints omitted, as in GestureLayer.play. A relative value on
        # an unknown joint still has to guess at 50, since there is nothing
        # better to measure from - the firmware guesses the same way - but an
        # absolute one ignores the start entirely and lands correctly.
        start = dict((code, self.pose.get(code)) for code in spec.joints
                     if self.pose.known(code))
        self.play(spec.resolve(start))

    def move_to(self, joints, duration=0.6, easing="ease_in_out"):
        """Ease to a pose. A commanded pose is a gesture with one keyframe."""
        self.play(Gesture([(float(duration), dict(joints))],
                          easing=easing, name="pose"))

    def nudge(self, deltas, duration=0.4, easing="ease_in_out"):
        """Move relative to where a joint is heading, and send it absolutely.

        Not a correction for the firmware, which does relative arithmetic
        without drifting. This buys easing, and deltas that accumulate so held
        presses sweep. Returns the absolute targets, since a nudge into the end
        of a joint's travel moves less than was asked for.
        """
        self._refuse_if_stopped()
        targets = {}
        for code, delta in deltas.items():
            # Against where the movement is going, not where it has got to, so
            # repeated nudges accumulate rather than eating each other.
            base = self.gesture.target_of(code)
            if base is None:
                base = self.pose.get(code)
            targets[code] = max(0.0, min(100.0, base + float(delta)))
        self.move_to(targets, duration=duration, easing=easing)
        return targets

    def stop(self, now=None):
        """Abort what is running and hold off new movement for a few seconds.

        Returns the cooldown. The joints keep their last commanded position and
        then power down on their own.
        """
        now = time.monotonic() if now is None else now
        self.gesture.stop()
        self._pending = {}
        self._suppress_until = now + STOP_COOLDOWN
        return STOP_COOLDOWN

    def resume(self):
        """Clear the cooldown early. Never required - it expires on its own."""
        self._suppress_until = 0.0

    def note_external_command(self, lines):
        """Adopt raw commands sent behind the engine's back.

        An absolute value is adopted so a later gesture eases from where the
        joint actually is; a relative one marks it unknown rather than guessing.
        """
        for line in lines:
            for token in str(line).split():
                if len(token) < 2:
                    continue
                code = token[:2]
                if code not in DRIVEN_JOINTS:
                    # The mouth and the disabled left arm come through here
                    # too. Recording them would fill the pose model with joints
                    # the engine can never drive, and then report them in
                    # `state()` as though it were tracking them.
                    continue
                rest = token[2:].lstrip(":")
                if not rest:
                    continue
                if rest[0] in "+-":
                    self.pose.forget(code)
                    self._sent.pop(code, None)
                    continue
                digits = "".join(c for c in rest if c.isdigit())
                if not digits:
                    continue
                value = max(0, min(100, int(digits)))
                self.pose.set(code, value)
                self._sent[code] = value

    # -- the tick ----------------------------------------------------------

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        self.ticks += 1
        if self.suppressed(now):
            return []

        # Anything still unsent starts the tick, so a live layer overrides it
        # but a finished one does not lose it.
        desired = dict(self._pending)
        # Lowest priority first, so a higher layer overwrites it per joint.
        for layer in sorted(self._layers, key=lambda l: l.priority):
            for code, value in layer.desired(now).items():
                if code in DRIVEN_JOINTS:
                    desired[code] = value

        changed = []
        for code, value in desired.items():
            rounded = int(round(max(0.0, min(100.0, value))))
            if self._sent.get(code) != rounded:
                changed.append((code, rounded))

        if not changed:
            # Everything wanted has reached the wire.
            self._pending = {}
            return []

        # Joints that did not fit last tick go first, so one that keeps
        # changing cannot starve one that is waiting.
        waiting = set(self._pending)
        changed.sort(key=lambda cv: 0 if cv[0] in waiting else 1)

        lines, deferred = self._pack(changed)
        deferred = set(deferred)
        self._pending = dict((c, v) for c, v in changed if c in deferred)
        if not lines:
            return []

        try:
            self._send(lines)
        except Exception as e:
            self.last_error = "%s" % e
            # Retry the whole batch rather than dropping it: none of it
            # reached the servos.
            self._pending = dict(changed)
            return []

        self.sends += 1
        for code, value in changed:
            if code not in deferred:
                # The pose model records what was actually commanded, so a
                # deferred joint is not claimed to have moved.
                self._sent[code] = value
                self.pose.set(code, value)
        return lines

    def _pack(self, changed):
        """Fit changed joints into at most two command lines.

        The rest waits for the next tick: 50ms on one joint is invisible, where
        a second serial round trip would double the cost of the tick. All nine
        driven joints currently fit, so deferring is only reachable once the
        left arm is enabled.
        """
        lines = []
        current = []
        deferred = []

        def width(tokens):
            return sum(len(t) for t in tokens) + max(0, len(tokens) - 1)

        def payload(committed, pending):
            total = sum(len(line) + 1 for line in committed)
            return total + (width(pending) + 1 if pending else 0)

        for code, value in changed:
            token = "%s%d" % (code, value)

            if current and width(current + [token]) > MAX_LINE_CHARS:
                # This line is full. Only one more may be opened.
                if lines:
                    deferred.append(code)
                    continue
                lines.append(" ".join(current))
                current = []

            if payload(lines, current + [token]) > MAX_PAYLOAD_BYTES:
                deferred.append(code)
                continue

            current.append(token)

        if current:
            lines.append(" ".join(current))

        return lines, deferred

    # -- introspection -----------------------------------------------------

    def state(self):
        return {
            "running": self._running,
            "stopped": self.suppressed() > 0,
            "stopped_for": round(self.suppressed(), 1),
            "tick_hz": round(1.0 / self._tick_seconds, 1),
            "gesture": self.gesture.current(),
            "pose": self.pose.snapshot(),
            "sent": dict(self._sent),
            "pending": sorted(self._pending),
            "ticks": self.ticks,
            "sends": self.sends,
            "overruns": self.overruns,
            "last_error": self.last_error,
        }
