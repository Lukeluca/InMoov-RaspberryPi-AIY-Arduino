from gary_api import GaryAPI
from flask import Flask, request, Response
from flask_cors import CORS, cross_origin
from config import base_conf
import json
import os
import threading

import motion
import tts

app = Flask(__name__)
cors = CORS(app, resources={r"/api/*": {"Access-Control-Allow-Origin": "*"}})
gary = GaryAPI()


def _motion_send(lines):
    """How the engine reaches the Arduino: through the same lock as everything
    else, so a tick and a mouth command cannot interleave on the port."""
    result = gary.send_commands({"commands": lines})
    if str(result.get("response", "")).startswith("ERROR"):
        # Raised rather than returned, so the engine records it and carries on
        # ticking instead of stopping mid-movement.
        raise RuntimeError(result["response"])
    return result


engine = motion.MotionEngine(_motion_send)

# Gestures live in a file next to this one rather than in code, so adding one is
# an edit rather than a deploy. Loaded once at import: they are small, and
# re-reading per request would mean a typo could break a gesture mid-show. A
# gesture that will not parse is skipped and reported through
# GET /api/motion/gestures instead of stopping the service.
GESTURE_FILE = os.environ.get(
    "GARY_GESTURE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "gestures.yaml"))
GESTURES, GESTURE_PROBLEMS = motion.load_library(GESTURE_FILE)
for _problem in GESTURE_PROBLEMS:
    print("gesture library: %s" % _problem)
print("gesture library: %d loaded from %s" % (len(GESTURES), GESTURE_FILE))

@app.route("/", methods=["GET", "POST"])
@cross_origin(app)
def home():
    return "<HTML>"\
           "<head><script type=\"text/javascript\" src=\"http://ajax.googleapis.com/ajax/libs/jquery/1.7.2/jquery.min.js\"></script></head>"\
           "<BODY>" \
           "<h1>GARY API</h1>" \
           "<p>Speak:"\
           "<input type='text' id='text2speech' style=\"width:50%;\" "\
           " onkeydown=\"if (event.keyCode==13) { document.getElementById('submit').click(); this.value=''; }\">" \
           "<input type='submit' id='submit' onclick='speak()'></p>" \
           "<script type='text/javascript'>"\
           "function speak() { "\
           " var text = document.getElementById('text2speech').value;" \
           " $.ajax({ "\
           "   type:\"POST\", "\
           "   url:\"/api/voice/text2speech\","\
           "   data: JSON.stringify({ 'text' : text}) ,"\
           "   dataType:'json', "\
           "   contentType:'application/json', "\
           "   success: function(){}"\
           " });"\
           "}"\
           "</script>" \
           "<p><a href='/api/video/stream'>Video Stream</a></p>" \
           "</BODY></HTML>"


MOUTH_OPEN = "HM:75"
MOUTH_CLOSED = "HM:0"


def close_mouth():
    """Shut the jaw. Runs on a background thread after an utterance."""
    try:
        gary.send_commands({"commands": [MOUTH_CLOSED]})
    except Exception as e:
        print("could not close the mouth: %s" % e)


def say(text, move_mouth=True):
    """Speak, moving the jaw with it. Blocks until the audio has finished.

    Moving the mouth belongs here rather than in whoever asked for the speech.
    This service owns the speaker and the servos, so everything that makes Gary
    talk gets a moving mouth without needing to know that the jaw is HM.
    """
    # Synthesis first, mouth second. Piper takes about two seconds to render a
    # sentence, and opening the jaw before that left Gary sitting open-mouthed
    # and silent while the model worked. With the old Pico voice synthesis took
    # 0.12s, so the gap was invisible.
    audio = tts.synthesize(text)
    try:
        if move_mouth:
            # Synchronous: the jaw has to be open before the sound starts.
            gary.send_commands({"commands": [MOUTH_OPEN]})
        tts.play(audio)
    finally:
        tts.discard(audio)
        # In a finally so a failed utterance does not leave him sitting there
        # with his mouth hanging open. Fire and forget, so the response is not
        # held up by the serial round trip; the lock in Arduino keeps it safe
        # alongside other callers.
        if move_mouth:
            closer = threading.Thread(target=close_mouth)
            closer.daemon = True
            closer.start()


@cross_origin(app)
@app.route("/api/voice/text2speech", methods=["POST"])
def text_to_speech():
    body = request.get_json()
    text = body['text']
    say(text, move_mouth=body.get('move_mouth', True))
    return json.dumps({'error_message': 'success', 'text': text })

@cross_origin(app)
@app.route("/api/voice/volume", methods=["GET", "POST"])
def voice_volume():
    """Read or set how loud Gary speaks, as a gain of 0.0 to 1.0.

    Stored in a file rather than held in memory, so it survives a restart, and
    read again per utterance, so it applies to the next thing he says.

    Setting it makes Gary announce the new level, because the only way to judge
    a volume is to hear it. Pass {"announce": false} to set it silently.
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if "volume" not in body:
            return Response(json.dumps({'error_message': 'no volume given'}),
                            status=400, mimetype='application/json')
        try:
            value = tts.set_volume(body["volume"])
        except (TypeError, ValueError):
            return Response(
                json.dumps({'error_message': 'volume must be a number'}),
                status=400, mimetype='application/json')
        print("volume set to %.2f" % value)

        # After storing it, never before, so what you hear is the new level.
        # Synchronous on purpose: the caller waits out the utterance, which
        # stops a dragged slider from stacking up overlapping announcements.
        if body.get("announce", True):
            try:
                say("%d" % round(value * 100))
            except Exception as e:
                # A silent robot is a bad reason to report the volume as unset,
                # since the value is already stored by this point.
                print("could not announce the volume: %s" % e)
                return json.dumps({'error_message': 'success', 'volume': value,
                                   'announced': False})

        return json.dumps({'error_message': 'success', 'volume': value})

    return json.dumps({'error_message': 'success', 'volume': tts.volume()})


@cross_origin(app)
@app.route("/api/video", methods=["GET"])
def capture():
    if request.method == "GET":
        frame_path = gary.sight.capture()
        return json.dumps({'error_message': 'success', 'file_path': frame_path })

        
@cross_origin(app)
@app.route("/api/video/stream", methods=["GET", "POST"])
def capture_stream():
    print('STREAMING')
    mimetype = 'multipart/x-mixed-replace; boundary=frame'
    
    
    if request.method == "GET":
        return Response(gary.sight.streamVideo(params={"move_to_face" : True, "detect_faces": True}), mimetype=mimetype)
    
    if request.method == "POST":
        return Response(gary.sight.streamVideo(params={"move_to_face" : True, "detect_faces": True, **request.get_json()}), mimetype=mimetype)
    

@cross_origin(app)
@app.route("/api/commands", methods=["GET", "POST"])
def commands():
    if request.method == "POST":
        body = request.get_json()
        res = (
            gary
            #.set_serial(base_conf["usb_port"])
            .send_commands(body)
        )

        # Raw commands stay open for calibration and hand-driving, and the
        # speech path drives the mouth through here. Telling the engine keeps
        # its pose model honest, so a gesture afterwards eases from where the
        # joint actually is rather than jumping.
        try:
            engine.note_external_command((body or {}).get("commands", []))
        except Exception as e:
            print("could not note external command: %s" % e)

        return json.dumps(res)


def _error(message, status=400):
    return Response(json.dumps({'error_message': message}),
                    status=status, mimetype='application/json')


def _stopped(e):
    """409 for a request that arrived during a stop's cooldown.

    Refusing out loud matters: the old stop reported success and then let the
    movement through anyway.
    """
    return Response(json.dumps({'error_message': '%s' % e,
                                'stopped_for': round(e.remaining, 1)}),
                    status=409, mimetype='application/json')


# Movement is a percentage of each joint's safe range, and a gesture that ran
# for hours would be indistinguishable from a wedged engine.
MAX_GESTURE_SECONDS = 30.0


def _joints(raw, where):
    """Validate a {code: percent} mapping. There is no authentication on any of
    this, so nothing from a request reaches the engine unchecked."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("%s must be a non-empty object of servo codes" % where)
    out = {}
    for code, value in raw.items():
        code = str(code).upper()
        if code not in motion.DRIVEN_JOINTS:
            raise ValueError("the engine does not drive %s (it drives %s)"
                             % (code, ", ".join(motion.DRIVEN_JOINTS)))
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("%s: %s must be a number" % (where, code))
        if not 0 <= value <= 100:
            raise ValueError("%s: %s must be between 0 and 100" % (where, code))
        out[code] = value
    return out


@cross_origin(app)
@app.route("/api/motion/state", methods=["GET"])
def motion_state():
    return json.dumps({'error_message': 'success', 'motion': engine.state()})


@cross_origin(app)
@app.route("/api/motion/pose", methods=["GET", "POST"])
def motion_pose():
    """Ease to a pose - which is a gesture with a single keyframe."""
    if request.method == "GET":
        return json.dumps({'error_message': 'success',
                           'pose': engine.pose.snapshot()})

    body = request.get_json(silent=True) or {}
    try:
        joints = _joints(body.get("joints"), "joints")
        duration = float(body.get("duration", 0.6))
    except (ValueError, TypeError) as e:
        return _error("%s" % e)
    if not 0 < duration <= MAX_GESTURE_SECONDS:
        return _error("duration must be between 0 and %.0f seconds"
                      % MAX_GESTURE_SECONDS)

    try:
        engine.move_to(joints, duration=duration,
                       easing=str(body.get("easing", "ease_in_out")))
    except motion.Suppressed as e:
        return _stopped(e)
    return json.dumps({'error_message': 'success', 'joints': joints,
                       'duration': duration})


@cross_origin(app)
@app.route("/api/motion/nudge", methods=["POST"])
def motion_nudge():
    """Move joints relative to where they are, sending absolute positions.

        {"joints": {"HH": 8, "HV": -5}, "duration": 0.4}

    A relative request, never a relative command: the delta is resolved here
    against the pose model and leaves as an absolute position. Doing that
    arithmetic in a browser would let two open consoles both add to the same
    reading.
    """
    body = request.get_json(silent=True) or {}
    raw = body.get("joints")
    if not isinstance(raw, dict) or not raw:
        return _error("joints must be a non-empty object of servo codes")

    deltas = {}
    for code, value in raw.items():
        code = str(code).upper()
        if code not in motion.DRIVEN_JOINTS:
            return _error("the engine does not drive %s (it drives %s)"
                          % (code, ", ".join(motion.DRIVEN_JOINTS)))
        try:
            value = float(value)
        except (TypeError, ValueError):
            return _error("joints: %s must be a number" % code)
        if not -100 <= value <= 100:
            return _error("joints: %s must be between -100 and 100" % code)
        deltas[code] = value

    try:
        duration = float(body.get("duration", 0.4))
    except (TypeError, ValueError):
        return _error("duration must be a number")
    if not 0 < duration <= MAX_GESTURE_SECONDS:
        return _error("duration must be between 0 and %.0f seconds"
                      % MAX_GESTURE_SECONDS)

    try:
        targets = engine.nudge(deltas, duration=duration,
                               easing=str(body.get("easing", "ease_in_out")))
    except motion.Suppressed as e:
        return _stopped(e)
    return json.dumps({'error_message': 'success', 'deltas': deltas,
                       'targets': targets, 'duration': duration})


@cross_origin(app)
@app.route("/api/motion/gestures", methods=["GET"])
def motion_gestures():
    """What Gary can do, so the console and the brain can ask rather than
    hard-code names. Load problems are reported here too - a gesture missing
    from this list because of a typo is otherwise invisible."""
    return json.dumps({
        'error_message': 'success',
        'gestures': dict(
            (name, {'duration': spec.duration,
                    'joints': sorted(spec.joints)})
            for name, spec in GESTURES.items()),
        'problems': GESTURE_PROBLEMS,
        'file': GESTURE_FILE,
    })


@cross_origin(app)
@app.route("/api/motion/gesture", methods=["POST"])
def motion_gesture():
    """Play a gesture, either by name or as keyframes sent inline.

        {"name": "nod"}
        {"keyframes": [[0.0, {"RW": 20}], [0.4, {"RW": 80}]]}

    Inline is how a gesture gets tried out before being written into the file,
    and takes the same value forms. Sending both is inline with a label, not a
    lookup.
    """
    body = request.get_json(silent=True) or {}

    # Keyframes win. With them, `name` is just a label for what is being sent -
    # which is how the inline form has always used it - so it must not be
    # mistaken for a request to play something out of the library.
    if "name" in body and "keyframes" not in body:
        name = str(body["name"])
        spec = GESTURES.get(name)
        if spec is None:
            return _error("no gesture called %r - try GET /api/motion/gestures"
                          % name, 404)
        try:
            engine.play_spec(spec)
        except motion.Suppressed as e:
            return _stopped(e)
        return json.dumps({'error_message': 'success', 'name': name,
                           'duration': spec.duration,
                           'joints': sorted(spec.joints)})

    raw = body.get("keyframes")
    if not isinstance(raw, list) or not raw:
        return _error("pass a gesture name, or keyframes as a non-empty list "
                      "of [seconds, joints]")

    keyframes = []
    try:
        for entry in raw:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                raise ValueError("each keyframe must be [seconds, joints]")
            at = float(entry[0])
            if not 0 <= at <= MAX_GESTURE_SECONDS:
                raise ValueError("keyframe time must be between 0 and %.0f"
                                 % MAX_GESTURE_SECONDS)
            if not isinstance(entry[1], dict) or not entry[1]:
                raise ValueError("the keyframe at %g moves nothing" % at)
            keyframes.append((at, entry[1]))
        spec = motion.GestureSpec(
            keyframes, easing=str(body.get("easing", "ease_in_out")),
            name=str(body.get("name", "inline")))
    except (ValueError, TypeError) as e:
        return _error("%s" % e)

    if spec.duration <= 0:
        return _error("a gesture needs a keyframe later than zero seconds")

    try:
        engine.play_spec(spec)
    except motion.Suppressed as e:
        return _stopped(e)
    return json.dumps({'error_message': 'success', 'name': spec.name,
                       'duration': spec.duration,
                       'joints': sorted(spec.joints)})


@cross_origin(app)
@app.route("/api/motion/track", methods=["POST"])
def motion_track():
    """Report where a face is, as offsets from the centre of frame.

        {"dx": 0.12, "dy": -0.05}    a face this far off centre
        {"face": null}               nothing in view

    Offsets rather than servo positions: `vision` reports what it can see and
    the engine decides what to do about it, so the control loop lives beside the
    pose model it is correcting. A gesture outranks this, so tracking gives the
    head up for the length of a gesture and picks it up again afterwards.
    """
    body = request.get_json(silent=True) or {}

    if "dx" not in body and "dy" not in body:
        # Anything else means "no face" - including an explicit {"face": null}.
        engine.lose_face()
        return json.dumps({'error_message': 'success', 'tracking': False})

    try:
        dx = float(body.get("dx", 0.0))
        dy = float(body.get("dy", 0.0))
    except (TypeError, ValueError):
        return _error("dx and dy must be numbers")
    if not (-1.0 <= dx <= 1.0 and -1.0 <= dy <= 1.0):
        return _error("dx and dy are fractions of the frame, so -1 to 1")

    try:
        targets = engine.look_at(dx, dy)
    except motion.Suppressed as e:
        return _stopped(e)
    return json.dumps({'error_message': 'success', 'tracking': True,
                       'dx': dx, 'dy': dy, 'targets': targets})


@cross_origin(app)
@app.route("/api/motion/stop", methods=["POST"])
def motion_stop():
    """Abort whatever is running and refuse new movement for a few seconds.

    A cooldown rather than a latch, so it can never leave Gary inert with
    nothing to explain why. `systemctl --user stop gary-servo` is the stop that
    holds.
    """
    cooldown = engine.stop()
    return json.dumps({'error_message': 'success', 'cooldown': cooldown,
                       'motion': engine.state()})


@cross_origin(app)
@app.route("/api/motion/resume", methods=["POST"])
def motion_resume():
    engine.resume()
    return json.dumps({'error_message': 'success', 'motion': engine.state()})


if __name__ == "__main__":
    print("\n Gary is listening... \n")
    gary.set_serial(base_conf["usb_port"])
    # Started here rather than at import, so anything that imports this module
    # does not get a thread driving servos as a side effect. It ticks
    # immediately but sends nothing until a layer claims a joint.
    engine.start()
    # debug=False is deliberate: the reloader would run this module twice and
    # open /dev/ttyUSB0 in both processes, which makes reads race and fail with
    # "multiple access on port". It also keeps the Werkzeug debugger console off
    # a socket bound to 0.0.0.0.
    app.run(debug=False, host="0.0.0.0", port=5000)
    
