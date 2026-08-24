"""Gary's vision service.

The single access point for anything that needs to see. Other services call
this over HTTP rather than opening the camera themselves, because only one
process can hold the device.

    GET  /health              service and camera state
    GET  /frame.jpg           one frame, as an image
    GET  /frames?count=3      several frames, base64, for a multimodal prompt
    POST /camera/release      drop the camera now, e.g. to free it for raspistill
    POST /tracking/start      begin, or extend, a face-tracking session
    POST /tracking/stop       end it now
    GET  /tracking            session and detector state

Tracking runs in here because it needs raw frames: going out through
/frame.jpg would cost a JPEG encode and decode, about 59ms, which is more than
the detection itself. It takes a camera lease rather than opening the device
separately, and it sends offsets to the servo API over HTTP - never by opening
the serial port, which servo_api owns.

Nothing starts a session by itself. Something has to ask.
"""

import base64
import json

from flask import Flask, Response, request
from flask_cors import CORS, cross_origin

from camera import Camera, CameraError
from tracker import Tracker

app = Flask(__name__)
CORS(app, resources={r"/*": {"Access-Control-Allow-Origin": "*"}})

camera = Camera()
tracker = Tracker(camera)
if not tracker.available():
    print("tracking unavailable: %s" % tracker.state()["problem"])

MAX_FRAMES = 10


def _error(message, status=500):
    return Response(json.dumps({"error_message": message}),
                    status=status, mimetype="application/json")


@app.route("/health", methods=["GET"])
@cross_origin(app)
def health():
    return Response(json.dumps({"error_message": "success",
                                "camera": camera.status()}),
                    mimetype="application/json")


@app.route("/frame.jpg", methods=["GET"])
@cross_origin(app)
def frame():
    try:
        jpeg = camera.capture()
    except CameraError as e:
        return _error(str(e))
    return Response(jpeg, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.route("/frames", methods=["GET"])
@cross_origin(app)
def frames():
    """Several frames in one response, base64 encoded.

    Shaped for a multimodal prompt: Gemini accepts several images in one
    request at roughly 1100 tokens each, so a short burst gives a wider or
    time-separated view without a second round trip.
    """
    try:
        count = int(request.args.get("count", 3))
        interval = float(request.args.get("interval", 250)) / 1000.0
    except ValueError:
        return _error("count must be an integer and interval a number", 400)

    if not 1 <= count <= MAX_FRAMES:
        return _error("count must be between 1 and %d" % MAX_FRAMES, 400)
    if not 0 <= interval <= 5:
        return _error("interval must be between 0 and 5000 ms", 400)

    try:
        jpegs = camera.capture_many(count, interval)
    except CameraError as e:
        return _error(str(e))

    return Response(json.dumps({
        "error_message": "success",
        "count": len(jpegs),
        "mime_type": "image/jpeg",
        "frames": [base64.b64encode(j).decode("ascii") for j in jpegs],
    }), mimetype="application/json")


@app.route("/camera/release", methods=["POST"])
@cross_origin(app)
def release():
    released = camera.release_now()
    return Response(json.dumps({
        "error_message": "success",
        "released": released,
        "camera": camera.status(),
    }), mimetype="application/json")


@app.route("/tracking/start", methods=["POST"])
@cross_origin(app)
def tracking_start():
    """Begin a session, or extend the one already running.

    Extending rather than restarting is what makes a follow-up question buy
    another 45 seconds without interrupting the tracking already in progress.
    """
    started, note = tracker.start()
    if not started:
        return _error("tracking unavailable: %s" % note, 503)
    return Response(json.dumps({"error_message": "success", "note": note,
                                "tracking": tracker.state()}),
                    mimetype="application/json")


@app.route("/tracking/stop", methods=["POST"])
@cross_origin(app)
def tracking_stop():
    stopped = tracker.stop("asked to stop")
    return Response(json.dumps({"error_message": "success",
                                "was_running": stopped,
                                "tracking": tracker.state()}),
                    mimetype="application/json")


@app.route("/tracking", methods=["GET"])
@cross_origin(app)
def tracking_state():
    return Response(json.dumps({"error_message": "success",
                                "tracking": tracker.state(),
                                "camera": camera.status()}),
                    mimetype="application/json")


if __name__ == "__main__":
    print("\n Gary is watching... \n")
    # debug=False for the same reason as the servo API: the reloader would run
    # this module twice and both copies would fight over the camera.
    app.run(debug=False, host="0.0.0.0", port=5002)
