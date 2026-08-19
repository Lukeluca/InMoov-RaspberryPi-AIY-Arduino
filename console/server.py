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
    POST /api/head         nudge the head, or recentre it
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

# Absolute position both head servos treat as straight ahead.
HEAD_CENTER = 50
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


@app.route("/api/head", methods=["POST"])
def head():
    """Move the head, or recentre it.

        {"pan": 8}      turn, relative, as HH+8
        {"tilt": -8}    tilt, relative, as HV-8
        {"center": true}  absolute HH50 HV50

    Recentring is absolute on purpose. There is no position feedback from the
    Arduino, so relative nudges accumulate error with nothing to correct it;
    an absolute command is the only way back to a known pose.
    """
    payload = request.get_json(silent=True) or {}
    commands = []

    if payload.get("center"):
        commands.append("HH%d HV%d" % (HEAD_CENTER, HEAD_CENTER))
    else:
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
                commands.append("%s%+d" % (axis, value))

    if not commands:
        return _json({"error_message": "nothing to move"}, 400)

    try:
        response = requests.post(SERVO_URL + "/api/commands",
                                 json={"commands": commands}, timeout=MOVE_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return _json({"error_message": "servo api unreachable: %s" % e}, 503)
    if response.status_code != 200:
        return _json({"error_message": "move failed (%d)" % response.status_code}, 502)
    return _json({"error_message": "success", "commands": commands})


@app.route("/api/command", methods=["POST"])
def command():
    """Send raw commands to the Arduino, for testing servos by hand.

        {"text": "HH50 HV+5"}

    Everything in one request goes down the serial link in a single write, which
    is also the only way to make two servos move together.
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
