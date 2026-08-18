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
    GET  /api/status       which services are up, and the camera state

There is no authentication. Anyone who can reach this port can move Gary and
make him speak, so think about where it is bound before exposing it.
"""

import json
import os

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

app = Flask(__name__)


def _json(payload, status=200):
    return Response(json.dumps(payload), status=status,
                    mimetype="application/json")


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
    """Put a question to the brain. Gary answers aloud and moves his mouth."""
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return _json({"error_message": "nothing to ask"}, 400)
    try:
        response = requests.post(BRAIN_URL + "/", data={"gary_prompt": text},
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
