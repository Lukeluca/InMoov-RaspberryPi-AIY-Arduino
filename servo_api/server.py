from gary_api import GaryAPI
from flask import Flask, request, Response
from flask_cors import CORS, cross_origin
from config import base_conf
import json
import threading

import tts

app = Flask(__name__)
cors = CORS(app, resources={r"/api/*": {"Access-Control-Allow-Origin": "*"}})
gary = GaryAPI()

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
        res = (
            gary
            #.set_serial(base_conf["usb_port"])
            .send_commands(request.get_json())
        )

        return json.dumps(res)

@cross_origin(app)
@app.route("/api/info", methods=["GET"])
def info():
    info = gary.arduino.available_machinations()
    return json.dumps({'error_message' : 'success', 'info': info})


if __name__ == "__main__":
    print("\n Gary is listening... \n")
    gary.set_serial(base_conf["usb_port"])    
    # debug=False is deliberate: the reloader would run this module twice and
    # open /dev/ttyUSB0 in both processes, which makes reads race and fail with
    # "multiple access on port". It also keeps the Werkzeug debugger console off
    # a socket bound to 0.0.0.0.
    app.run(debug=False, host="0.0.0.0", port=5000)
    
