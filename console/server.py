"""Gary's remote console.

Serves one page and proxies everything it needs to the services behind it.
Proxying rather than calling them from the browser buys three things: the brain
can stay bound to localhost instead of being exposed to the network, the page
has a single origin so CORS never comes into it, and the services that own
hardware do not have to serve a user interface.

    GET  /                 the console
    GET  /api/frame.jpg    latest camera frame, via vision
    POST /api/ask          ask Gemini - Gary answers out loud and moves
    POST /api/say          make Gary say something verbatim
    GET  /api/gestures     what Gary can do
    POST /api/gesture      play one named gesture
    POST /api/head         nudge the head, or recenter it
    POST /api/command      raw servo commands, for testing by hand
    GET  /api/volume       how loud Gary speaks
    POST /api/volume       set how loud Gary speaks
    GET  /api/status       which services are up, and the camera state

There is no authentication. Anyone who can reach this port can move Gary and
make him speak, so think about where it is bound before exposing it.
"""

import json
import os
import re

import requests
from flask import Flask, Response, render_template, request

SERVO_URL = os.environ.get("GARY_SERVO_URL", "http://localhost:5000")
BRAIN_URL = os.environ.get("GARY_BRAIN_URL", "http://localhost:5001")
VISION_URL = os.environ.get("GARY_VISION_URL", "http://localhost:5002")
EARS_URL = os.environ.get("GARY_EARS_URL", "http://localhost:5003")

# Speaking blocks until the audio finishes, and a Gemini round trip plus speech
# runs to about ten seconds, so these are generous on purpose.
SPEAK_TIMEOUT = 60
FRAME_TIMEOUT = 15
STATUS_TIMEOUT = 3
# A servo command waits on the Arduino replying, which takes a second or two.
MOVE_TIMEOUT = 20
# Reading or writing the volume is a small file operation, nothing more.
VOLUME_TIMEOUT = 5

# Absolute positions the centre pad returns to. Pan really is the middle of its
# travel; tilt is not - 50 points him at the floor, below a standing person's
# face, so rest is 75. These match the head_recenter and body_reset gestures.
HEAD_CENTER_PAN = 50
HEAD_CENTER_TILT = 75
# Refuse an implausible nudge. The Arduino clamps to its own servo limits, so
# this is only here to catch a mistake in the page.
HEAD_STEP_LIMIT = 50

# The shape of one raw command: a two-letter servo code followed straight by a
# value, with nothing in between, or the bare "DP".
#
#   HH50    absolute, as a percentage of that servo's travel
#   HH+5    relative, from -100 to +100
#   DP      every enabled servo to its default position
#
# A colon between the two is accepted and then dropped. It is not the documented
# form, but the firmware tolerates it by accident - the value is found with
# strpbrk over "+-1234567890", which skips anything that is not a sign or a
# digit - and other callers here have always sent one, so pasting "HM:75" out of
# servo_api still works. Only the documented form reaches the Arduino.
#
# Any two-letter code is allowed through, rather than a list kept in step with
# the firmware, so a servo added to the sketch works here with no change on
# this side - the Arduino answers E100 for a code it does not know, which is
# the more useful thing to see while testing anyway. Values are not range
# checked for the same reason: the firmware clamps both absolute and relative
# moves to 0-100%, so an out of range value is safe, and being able to send one
# is how you check that clamping still works.
COMMAND_RE = re.compile(r"^(?:DP|([A-Z]{2}):?([+-]?\d{1,3}))$")

# Commands may be typed with spaces, commas or newlines between them.
COMMAND_SEPARATORS = re.compile(r"[\s,;]+")

# Room for a pose across every servo an InMoov has, while still refusing a
# whole file pasted in by accident.
MAX_COMMANDS = 40

app = Flask(__name__)


def _json(payload, status=200):
    return Response(json.dumps(payload), status=status,
                    mimetype="application/json")


def _canonical(command):
    """The command as the firmware documents it, or None if it is not one.

    "HH50" and "HH:50" both come back as "HH50".
    """
    match = COMMAND_RE.match(command)
    if match is None:
        return None
    if match.group(1) is None:
        return command  # DP, which carries no value.
    return match.group(1) + match.group(2)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/api/frame.jpg", methods=["GET"])
def frame():
    """Proxy a single frame. The page polls this rather than streaming, so the
    camera is released by the vision service once nobody is looking."""
    try:
        response = requests.get(VISION_URL + "/frame.jpg", timeout=FRAME_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _json({"error_message": "vision unreachable: %s" % e}, 503)
    if response.status_code != 200:
        return _json({"error_message": "vision returned %d" % response.status_code},
                     response.status_code)
    return Response(response.content, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/ask", methods=["POST"])
def ask():
    """Put a question to the brain. Gary answers aloud and moves his mouth.

    With {"image": true} the brain photographs what he can see as the question
    arrives and sends that to Gemini alongside it.
    """
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "").strip()
    if not text:
        return _json({"error_message": "nothing to ask"}, 400)
    form = {"gary_prompt": text}
    if payload.get("image"):
        form["include_image"] = "1"
    try:
        response = requests.post(BRAIN_URL + "/", data=form,
                                 timeout=SPEAK_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _json({"error_message": "brain unreachable: %s" % e}, 503)
    if response.status_code != 200:
        return _json({"error_message": "brain returned %d" % response.status_code},
                     502)
    try:
        return _json(response.json())
    except ValueError:
        return _json({"error_message": "brain sent a non-JSON reply"}, 502)


@app.route("/api/say", methods=["POST"])
def say():
    """Speak as Gary, verbatim. No model involved."""
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return _json({"error_message": "nothing to say"}, 400)
    try:
        response = requests.post(SERVO_URL + "/api/voice/text2speech",
                                 json={"text": text}, timeout=SPEAK_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _json({"error_message": "servo api unreachable: %s" % e}, 503)
    if response.status_code != 200:
        return _json({"error_message": "text to speech failed (%d)"
                      % response.status_code}, 502)
    return _json({"error_message": "success", "text": text})


@app.route("/api/gestures", methods=["GET"])
def gestures():
    """What Gary can do, straight from the servo API's gesture file.

    Proxied rather than listed in the page, so a new gesture in `gestures.yaml`
    gets a button with no edit here. Load problems come through too.
    """
    try:
        response = requests.get(SERVO_URL + "/api/motion/gestures",
                                timeout=STATUS_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _json({"error_message": "servo api unreachable: %s" % e}, 503)
    if response.status_code != 200:
        return _json({"error_message": "gesture list failed (%d)"
                      % response.status_code}, 502)
    try:
        return _json(response.json())
    except ValueError:
        return _json({"error_message": "servo api sent a non-JSON reply"}, 502)


@app.route("/api/gesture", methods=["POST"])
def gesture():
    """Play one gesture by name.

        {"name": "nod"}

    Not checked against a list here: the servo API owns the file and answers
    404 for a name it does not know, which is passed straight back. A second
    copy of the names on this side would only go stale.
    """
    name = str((request.get_json(silent=True) or {}).get("name", "")).strip()
    if not name:
        return _json({"error_message": "no gesture named"}, 400)
    try:
        response = requests.post(SERVO_URL + "/api/motion/gesture",
                                 json={"name": name}, timeout=MOVE_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _json({"error_message": "servo api unreachable: %s" % e}, 503)
    if response.status_code != 200:
        try:
            message = response.json().get("error_message", "")
        except ValueError:
            message = ""
        # An unknown gesture is the caller's mistake and a stop is a real
        # answer, so both keep their own status rather than being flattened
        # into a bad gateway.
        status = (response.status_code
                  if response.status_code in (404, 409) else 502)
        return _json({"error_message": message or ("gesture failed (%d)"
                                                   % response.status_code)},
                     status)
    try:
        return _json(response.json())
    except ValueError:
        return _json({"error_message": "servo api sent a non-JSON reply"}, 502)


# How long an eased head move takes. The engine interpolates rather than
# jumping, so these are also how long the movement lasts, which is what the
# page waits out before fetching a new frame. Recentring is slower because it
# is usually a longer way to travel.
HEAD_NUDGE_SECONDS = 0.4
HEAD_CENTER_SECONDS = 0.8


@app.route("/api/head", methods=["POST"])
def head():
    """Move the head, or recenter it.

        {"pan": 8}        turn, relative, eased
        {"tilt": -8}      tilt, relative, eased
        {"center": true}  back to the middle

    These go through the motion engine, so the movement eases instead of
    snapping and held presses sweep. Raw `HH+8` would land in the same place -
    the firmware does relative arithmetic without drifting - but in one jump,
    and leaves the pose model guessing.

    This is the *arrow pad* path. Raw servo commands keep their own route,
    deliberately unmediated - see `command()`.
    """
    payload = request.get_json(silent=True) or {}

    if payload.get("center"):
        path = "/api/motion/pose"
        duration = HEAD_CENTER_SECONDS
        body = {"joints": {"HH": HEAD_CENTER_PAN, "HV": HEAD_CENTER_TILT},
                "duration": duration}
    else:
        joints = {}
        for axis, key in (("HH", "pan"), ("HV", "tilt")):
            value = payload.get(key)
            if value is None:
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                return _json({"error_message": "%s must be a whole number" % key}, 400)
            if abs(value) > HEAD_STEP_LIMIT:
                return _json({"error_message": "%s beyond +/-%d" % (key, HEAD_STEP_LIMIT)}, 400)
            if value:
                joints[axis] = value

        if not joints:
            return _json({"error_message": "nothing to move"}, 400)
        path = "/api/motion/nudge"
        duration = HEAD_NUDGE_SECONDS
        body = {"joints": joints, "duration": duration}

    try:
        response = requests.post(SERVO_URL + path, json=body,
                                 timeout=MOVE_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _json({"error_message": "servo api unreachable: %s" % e}, 503)
    if response.status_code != 200:
        try:
            message = response.json().get("error_message", "")
        except ValueError:
            message = ""
        # A stop keeps its 409, so the page can say he is stopped rather than
        # reporting a vague failure.
        status = 409 if response.status_code == 409 else 502
        return _json({"error_message": message or ("move failed (%d)"
                                                   % response.status_code)},
                     status)

    try:
        upstream = response.json()
    except ValueError:
        upstream = {}

    # The duration goes back so the page can wait out the movement rather than
    # guessing at it, and the targets so a nudge that ran into the end of a
    # joint's travel can say where it actually stopped.
    return _json({"error_message": "success", "duration": duration,
                  "targets": upstream.get("targets") or upstream.get("joints")})


@app.route("/api/command", methods=["POST"])
def command():
    """Send raw commands to the Arduino, for testing servos by hand.

        {"text": "HH50 HV+5"}

    Everything in one request goes down the serial link in a single write, which
    is also the only way to make two servos move together.

    THIS STAYS RAW. It goes to `/api/commands` and reaches the firmware
    unmediated - no easing, no pose model, no engine - because that is the
    entire point of it: calibrating a joint, or checking a servo the engine
    refuses to drive, means seeing exactly what the firmware does with exactly
    what you typed. Routing it through the motion engine would make it a
    friendlier control and a useless instrument. The arrow pads are the
    mediated path; this one is deliberately not. Do not "unify" them.

    Sending a relative command here does leave the engine's pose model unsure
    where that joint is, and it marks it unknown. That is caution rather than
    ignorance: the firmware would land somewhere exact, but predicting it from
    here means replicating its per-servo degree table, which this side
    deliberately has no copy of, and allowing for a step under a degree of
    travel being silently dropped. The engine recovers on the next absolute
    position it sends.
    """
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return _json({"error_message": "nothing to send"}, 400)

    typed = [c.upper() for c in COMMAND_SEPARATORS.split(text) if c]
    if len(typed) > MAX_COMMANDS:
        return _json({"error_message": "at most %d commands at a time"
                      % MAX_COMMANDS}, 400)

    commands = [_canonical(c) for c in typed]
    malformed = [t for t, c in zip(typed, commands) if c is None]
    if malformed:
        return _json({"error_message": "not a command: %s"
                      % ", ".join(malformed)}, 400)

    try:
        response = requests.post(SERVO_URL + "/api/commands",
                                 json={"commands": commands},
                                 timeout=MOVE_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _json({"error_message": "servo api unreachable: %s" % e}, 503)
    if response.status_code != 200:
        return _json({"error_message": "servo api returned %d"
                      % response.status_code}, 502)

    # The servo API answers 200 even when the Arduino rejected the command - an
    # unsupported servo comes back as E100 and turns into an "ERROR: ..." string
    # in the body, and nowhere else. So the body decides whether this worked.
    try:
        body = response.json()
    except ValueError:
        return _json({"error_message": "servo api sent a non-JSON reply"}, 502)

    reply = str(body.get("response", ""))
    if reply.startswith("ERROR"):
        return _json({"error_message": reply, "commands": commands}, 502)

    return _json({"error_message": "success", "commands": commands,
                  "response": reply or "SUCCESS"})


@app.route("/api/volume", methods=["GET", "POST"])
def volume():
    """Read or set how loud Gary speaks, as a gain of 0.0 to 1.0.

    The servo API owns this, because it owns the speaker; here it is only
    forwarded so the page has a single origin to talk to.
    """
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        try:
            value = float(payload.get("volume"))
        except (TypeError, ValueError):
            return _json({"error_message": "volume must be a number"}, 400)
        # The servo API clamps as well; refusing here means an obviously wrong
        # value never reaches it, and says so in terms the page can show.
        if not 0.0 <= value <= 1.0:
            return _json({"error_message": "volume must be between 0 and 1"}, 400)
        try:
            # Setting the volume makes Gary announce the new level and only
            # answers once he has finished saying it, so this waits as long as
            # speaking does, not as long as a file write does.
            response = requests.post(SERVO_URL + "/api/voice/volume",
                                     json={"volume": value},
                                     timeout=SPEAK_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return _json({"error_message": "servo api unreachable: %s" % e}, 503)
    else:
        try:
            response = requests.get(SERVO_URL + "/api/voice/volume",
                                    timeout=VOLUME_TIMEOUT)
        except requests.exceptions.RequestException as e:
            return _json({"error_message": "servo api unreachable: %s" % e}, 503)

    if response.status_code != 200:
        return _json({"error_message": "volume failed (%d)"
                      % response.status_code}, 502)
    try:
        body = response.json()
    except ValueError:
        return _json({"error_message": "servo api sent a non-JSON reply"}, 502)
    return _json({"error_message": "success", "volume": body.get("volume")})


@app.route("/api/status", methods=["GET"])
def status():
    services = {}

    def probe(name, url):
        try:
            r = requests.get(url, timeout=STATUS_TIMEOUT)
            services[name] = r.status_code < 500
            return r
        except requests.exceptions.RequestException:
            services[name] = False
            return None

    probe("servo", SERVO_URL + "/")
    probe("brain", BRAIN_URL + "/")
    probe("ears", EARS_URL + "/health")
    vision_response = probe("vision", VISION_URL + "/health")

    camera = None
    if vision_response is not None:
        try:
            camera = vision_response.json().get("camera")
        except ValueError:
            camera = None

    return _json({"error_message": "success", "services": services,
                  "camera": camera})


if __name__ == "__main__":
    print("\n Gary's console is up on :5004 \n")
    app.run(debug=False, host="0.0.0.0", port=5004, threaded=True)
