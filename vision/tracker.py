"""Following a face, for as long as an interaction lasts.

Deliberately not always on. A session starts when someone speaks to Gary and
ends when they leave, which keeps the camera closed except while a person is
actually in front of him, bounds the load on the serial port, and means tracking
never starts itself.

The session owns its own ending, and needs two clocks rather than any states:

    on start            started_at = last_face_at = now
    a face is seen      last_face_at = now, report where it is
    5s with no face     end
    45s since start     end
    end                 stop reporting, drop the lease, play body_reset

"Waiting to see whether anyone appears" and "waiting to see whether they come
back" are the same rule, because `last_face_at` begins at the session's own
start - so a poke gives you five seconds to get into frame by exactly the
mechanism that gives you five seconds to step back into it.

The session budget is reset by a new trigger and never by a detection. That is
what makes it work: talking to him restarts it, so it cannot cut off a real
conversation, while a false positive on a poster produces no triggers at all and
so lets go after 45 seconds.

This reports offsets, not servo positions. `servo_api` owns the pose model and
decides what to do about a face, so the control loop lives next to the thing it
is correcting - and a gesture there simply outranks tracking for its duration.
"""

import os
import threading
import time

import requests

try:
    import cv2
except ImportError:                                        # pragma: no cover
    cv2 = None

SERVO_URL = os.environ.get("GARY_SERVO_URL", "http://localhost:5000")

# Bundled rather than taken from OpenCV: cv2 3.2 ships no cascade data, and the
# only copy on this Pi was inside an unrelated virtualenv that nothing maintains.
CASCADE = os.environ.get(
    "GARY_FACE_CASCADE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "haarcascade_frontalface_default.xml"))

# Measured on the robot: at 640 wide a face is found at every distance from 1 to
# 4 m for about 38 ms, and detection needs roughly 40 px of face in the scaled
# image - a cliff, not a gradual decline, and well above OpenCV's nominal 24 px
# window. 480 would save 12 ms and cap usable range at about 3 m.
DETECT_WIDTH = int(os.environ.get("GARY_DETECT_WIDTH", "640"))

# The ordinary pass: no preprocessing, coarse scale step, ~38ms. It found a face
# in four of five captures taken at 1, 2, 3 and 4m.
SCALE_FACTOR = 1.3
MIN_NEIGHBORS = 5

# The careful pass, for the fifth: a real face, in frame at 1m, but lit so badly
# the ordinary pass could not see it at any width. It needs equalisation AND a
# finer scale step AND a wider detect image - all three, measured one at a time.
# ~345ms, so about 3 fps, which the engine's own 20Hz interpolation still
# smooths into continuous movement.
#
# Note equalisation is not a free improvement: applied to a *well-lit* frame it
# broke detection that worked without it. So it belongs here, behind a failure
# to acquire, and never as a default. An equalised second pass at the ordinary
# 640 was tried first and rescued none of the five captures while costing 24ms
# on every empty frame.
CAREFUL_WIDTH = int(os.environ.get("GARY_CAREFUL_WIDTH", "960"))
CAREFUL_SCALE = 1.1
CAREFUL_NEIGHBORS = 4

# How long to fail at the ordinary pass before switching a session to the
# careful one. Once switched it stays switched: the lighting that needed it will
# not have changed, and flapping between two costs would only make the tick rate
# unpredictable.
ESCALATE_AFTER = float(os.environ.get("GARY_ESCALATE_AFTER", "1.5"))

# The camera is not on the head's centre line, so a face Gary is genuinely
# looking at does not land in the middle of the frame. This is where "looking
# at you" actually puts a face, as a fraction of frame from centre, negative
# being left. Offsets are reported relative to it, which also means the
# motion engine's dead zone becomes a box around this point rather than
# around the middle - no change needed on that side, since it never knew
# about cameras.
#
# It is not one constant, though: the offset is a physical displacement
# between the lens and the head's rotation axis, not an angular one, so how
# far off-centre it lands also depends on distance - closer faces need a
# bigger correction than far ones (parallax). So this is a small table of
# (face width as a fraction of frame, dx, dy), each point measured by aiming
# the head at a person standing at a given distance and reading `last_raw`
# from GET /tracking, confirmed against an annotated frame each time rather
# than guessed. Width, not distance, because width is what's actually
# measured every frame and it is scale-invariant across the ordinary and
# careful passes, which detect at different image widths.
#
# Between two measured points the target is interpolated linearly by width.
# Outside the measured range it holds the nearest end rather than
# extrapolating - a width far outside anything measured is more likely a bad
# detection than a real one at an untested distance.
#
# Both measured 2026-08-24 with Luke confirmed in frame each time:
#   far:  width 0.057 (near the detection cliff itself)
#   near: width 0.385
SWEET_SPOTS = (
    (0.057, -0.211, -0.270),
    (0.385, -0.258, -0.074),
)


def _target_for_width(width_frac):
    """(dx, dy) "looking at them" should land at, for a face this wide."""
    points = SWEET_SPOTS
    if width_frac <= points[0][0]:
        return points[0][1], points[0][2]
    if width_frac >= points[-1][0]:
        return points[-1][1], points[-1][2]
    for (w0, dx0, dy0), (w1, dx1, dy1) in zip(points, points[1:]):
        if w0 <= width_frac <= w1:
            t = (width_frac - w0) / (w1 - w0)
            return dx0 + (dx1 - dx0) * t, dy0 + (dy1 - dy0) * t
    return points[-1][1], points[-1][2]  # unreachable; the table is sorted

# Below this the cascade is matching texture rather than people. Also a useful
# speed-up, since it skips the smallest and most numerous search windows.
MIN_FACE = int(os.environ.get("GARY_MIN_FACE", "30"))

# Two consecutive detections have to agree to within this, as a fraction of
# frame, before either is believed. A real face barely moves between frames; a
# false positive on dark texture jumps around. Measured on a failing session:
# error went +0.200, +0.329, -0.062 in six seconds, which no head does, and the
# tracker followed it to the end of the head's travel. A person walking at 2m
# moves well under this between frames, so it costs nothing real.
AGREE_WITHIN = float(os.environ.get("GARY_AGREE_WITHIN", "0.12"))

FACE_LOST_SECONDS = float(os.environ.get("GARY_FACE_LOST_SECONDS", "5"))
SESSION_SECONDS = float(os.environ.get("GARY_SESSION_SECONDS", "45"))

# Detection is ~38 ms and a warm capture ~50 ms, so eight times a second leaves
# the loop idle about a third of the time. The engine interpolates at 20 Hz
# regardless, which is what decouples how smooth the movement looks from how
# often we detect.
LOOP_INTERVAL = float(os.environ.get("GARY_TRACK_INTERVAL", "0.125"))

RESET_GESTURE = "body_reset"
POST_TIMEOUT = 4


class Tracker(object):
    """Runs one tracking session at a time against the shared camera."""

    def __init__(self, camera):
        self._camera = camera
        self._lock = threading.Lock()
        self._cascade = None
        self._cascade_problem = None
        self._thread = None
        self._open = False
        self._started_at = 0.0
        self._last_face_at = 0.0
        self._triggers = 0
        self._faces = 0
        self._misses = 0
        self._last_offset = None
        self._last_raw = None
        self._last_target = None
        self._pending_offset = None
        self._rejected = 0
        self._last_end = None
        self._careful = False
        self._load_cascade()

    # -- setup -------------------------------------------------------------

    def _load_cascade(self):
        if cv2 is None:
            self._cascade_problem = "OpenCV is not available"
            return
        if not os.path.exists(CASCADE):
            self._cascade_problem = "no cascade file at %s" % CASCADE
            return
        cascade = cv2.CascadeClassifier(CASCADE)
        if cascade.empty():
            self._cascade_problem = "cascade at %s would not load" % CASCADE
            return
        self._cascade = cascade

    def available(self):
        return self._cascade is not None

    # -- session -----------------------------------------------------------

    def start(self):
        """Begin a session, or extend the one already running.

        A second trigger during a session resets both clocks rather than
        starting anything new, so a follow-up question buys another 45 seconds
        and another five to be found in.
        """
        with self._lock:
            if not self.available():
                return False, self._cascade_problem
            now = time.monotonic()
            self._started_at = now
            self._last_face_at = now
            self._triggers += 1
            if self._open:
                return True, "session extended"
            self._camera.acquire_lease()
            self._open = True
            self._careful = False
            self._thread = threading.Thread(target=self._run, name="tracker")
            self._thread.daemon = True
            self._thread.start()
            return True, "session started"

    def stop(self, reason="asked to stop"):
        with self._lock:
            if not self._open:
                return False
            self._open = False
        self._finish(reason)
        return True

    def _finish(self, reason):
        """Let go of the head, drop the lease, and return Gary to rest.

        Order matters. The reset is a gesture and outranks tracking, so it would
        reach its pose and then hand the joints straight back to a still-hungry
        tracker, which would pull the head back to the face and silently undo
        it. Stop claiming first, then reset.
        """
        self._post("/api/motion/track", {"face": None})
        try:
            self._camera.release_lease()
        except Exception as e:
            print("tracker: could not release the camera lease: %s" % e)
        self._post("/api/motion/gesture", {"name": RESET_GESTURE})
        self._last_end = reason
        print("tracker: session over - %s" % reason)

    # -- the loop ----------------------------------------------------------

    def _run(self):
        print("tracker: session started, detecting at %dpx" % DETECT_WIDTH)
        while True:
            with self._lock:
                if not self._open:
                    return
                started_at = self._started_at
                last_face_at = self._last_face_at
            now = time.monotonic()

            if now - last_face_at > FACE_LOST_SECONDS:
                self._end_from_loop("no face for %.0fs" % FACE_LOST_SECONDS)
                return
            if now - started_at > SESSION_SECONDS:
                self._end_from_loop("%.0fs since the last trigger"
                                    % SESSION_SECONDS)
                return

            started = time.monotonic()
            try:
                self._look()
            except Exception as e:
                # A camera hiccup or a failed post must not end the session or
                # kill the thread; the clocks above will close it if it really
                # has stopped working.
                print("tracker: %s" % e)
            slack = LOOP_INTERVAL - (time.monotonic() - started)
            if slack > 0:
                time.sleep(slack)

    def _end_from_loop(self, reason):
        with self._lock:
            self._open = False
        self._finish(reason)

    def _look(self):
        with self._lock:
            careful = self._careful
            starving = (not self._careful
                        and time.monotonic() - self._last_face_at > ESCALATE_AFTER)
        if starving:
            # Nothing found for a while. Either there is nobody there, in which
            # case the extra cost buys nothing but the session is about to end
            # anyway, or they are too poorly lit for the ordinary pass.
            print("tracker: no face in %.1fs, switching to the careful pass"
                  % ESCALATE_AFTER)
            with self._lock:
                self._careful = True
            careful = True

        frame = self._camera.read()
        offset = self.find_face(frame, careful=careful)

        if offset is not None:
            previous = self._pending_offset
            self._pending_offset = offset
            if previous is None or (abs(offset[0] - previous[0]) > AGREE_WITHIN or
                                    abs(offset[1] - previous[1]) > AGREE_WITHIN):
                # Either the first sighting or a jump too big to be a person.
                # Hold it as the new candidate and wait for the next frame to
                # agree before moving anything.
                with self._lock:
                    self._rejected += 1
                offset = None

        if offset is None:
            # Deliberately silent. A single missed frame should not drop the
            # target and make the head sag - the layer's own staleness timeout
            # covers a tracker that has really stopped reporting.
            with self._lock:
                self._misses += 1
            return
        with self._lock:
            self._last_face_at = time.monotonic()
            self._faces += 1
            self._last_offset = offset
        self._post("/api/motion/track", {"dx": offset[0], "dy": offset[1]})

    def find_face(self, frame, careful=False):
        """The largest face as (dx, dy) from centre, in fractions of frame.

        Returns None when there is nothing to see. Separate from the loop so it
        can be run against a still image.
        """
        width = CAREFUL_WIDTH if careful else DETECT_WIDTH
        height = frame.shape[0]
        small = cv2.resize(frame, (width, int(height * float(width) / frame.shape[1])))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if careful:
            gray = cv2.equalizeHist(gray)

        faces = self._cascade.detectMultiScale(
            gray,
            CAREFUL_SCALE if careful else SCALE_FACTOR,
            CAREFUL_NEIGHBORS if careful else MIN_NEIGHBORS,
            minSize=(MIN_FACE, MIN_FACE))
        if not len(faces):
            return None

        x, y, w, h = max(faces, key=lambda f: f[2])
        sw, sh = gray.shape[1], gray.shape[0]
        raw = ((x + w / 2.0) / sw - 0.5, (y + h / 2.0) / sh - 0.5)
        self._last_raw = raw
        target_dx, target_dy = _target_for_width(w / float(sw))
        self._last_target = (target_dx, target_dy)
        # Relative to the sweet spot for this width, so zero error means
        # "looking at them" rather than "they are in the middle of the
        # picture".
        return (raw[0] - target_dx, raw[1] - target_dy)

    # -- plumbing ----------------------------------------------------------

    def _post(self, path, body):
        try:
            requests.post(SERVO_URL + path, json=body, timeout=POST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            print("tracker: %s not delivered: %s" % (path, e))

    def state(self):
        with self._lock:
            now = time.monotonic()
            return {
                "available": self.available(),
                "problem": self._cascade_problem,
                "open": self._open,
                "detect_width": CAREFUL_WIDTH if self._careful else DETECT_WIDTH,
                "careful": self._careful,
                "seconds_since_face": (round(now - self._last_face_at, 1)
                                       if self._open else None),
                "seconds_left": (round(SESSION_SECONDS - (now - self._started_at), 1)
                                 if self._open else None),
                "triggers": self._triggers,
                "faces": self._faces,
                "misses": self._misses,
                "last_offset": self._last_offset,
                "rejected": self._rejected,
                "last_raw": self._last_raw,
                "last_target": self._last_target,
                "sweet_spots": SWEET_SPOTS,
                "last_end": self._last_end,
            }
