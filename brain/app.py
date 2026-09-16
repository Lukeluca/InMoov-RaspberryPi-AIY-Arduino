import os
import base64
import logging
import json
import jsonify
import requests
import time
import urllib.request

#import google.generativeai as genai
import cachetools
import threading

from flask import Flask, redirect, render_template, request, url_for
from flask_cors import CORS, cross_origin
from werkzeug.serving import WSGIRequestHandler
from aiy.board import Board, Led


app = Flask(__name__)
#genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# Create a 5 minute cache for the conversations
cache = cachetools.TTLCache(maxsize=10, ttl=300)

VISION_URL = os.environ.get("GARY_VISION_URL", "http://localhost:5002")
FRAME_TIMEOUT = 8
# A stashed frame older than this belongs to an abandoned exchange.
FRAME_MAX_AGE = 60

# Gemini answers in about a second; anything much longer is a stall, not
# thinking. Bounded so one bad connection cannot hold Gary up indefinitely.
GEMINI_TIMEOUT = float(os.environ.get("GARY_GEMINI_TIMEOUT", "12"))
GEMINI_ATTEMPTS = int(os.environ.get("GARY_GEMINI_ATTEMPTS", "2"))

# Speaking blocks until the audio has finished playing, so this has to cover a
# whole spoken reply rather than a round trip - the console uses the same 60
# against the same endpoint.
SPEAK_TIMEOUT = 60
# A backstop for the fire-and-forget posts, not a tuned per-endpoint value.
# The slowest legitimate one is a tracking start that has to open a cold
# camera and wait out its 1.5s exposure warmup; a gesture returns as soon as
# the movement STARTS, so it is quick. Both are far inside this.
#
# It exists at all because `requests` has no default timeout: a servo API or
# vision that accepts the connection and then stalls would pin the thread in
# recv() forever, and every gesture play starts one of these. That is the leak
# that took the ears to 323 threads, and gestures fire more often than
# transcripts do.
ASYNC_TIMEOUT = 20

# The ears report that someone started and stopped speaking. They know nothing
# about cameras - this service decides those moments are worth photographing,
# which is why a robot with no camera runs the same ears unchanged.
FRAME_SLOTS = [
    ("started", "Camera view as the person started speaking:"),
    ("ended", "Camera view as they finished speaking:"),
]

# A poke is something happening with no words attached to it - the first being
# the button held and released in silence. What it is worth saying is decided
# here rather than by whoever noticed it, so the answer can change without
# touching the ears. Fixed wording, not a model call: it is an instruction, and
# it should come back the same way every time and without waiting on Gemini.
POKE_REPLY = ("Did you mean to say something? Hold the button down while "
              "speaking and release it when you're done.")

_frames_lock = threading.Lock()
_frames = {}


def grab_frame(slot):
    """Fetch a frame from the vision service and stash it under `slot`.

    Runs on a background thread so a speech event returns immediately. A
    missing or unreachable vision service is an ordinary condition, not an
    error: plenty of InMoovs have no camera.
    """
    try:
        response = requests.get(VISION_URL + "/frame.jpg", timeout=FRAME_TIMEOUT)
        if response.status_code != 200 or not response.content:
            logging.info("no %s frame: vision returned %d", slot, response.status_code)
            return
    except requests.exceptions.RequestException as e:
        logging.info("no %s frame: %s", slot, e)
        return
    with _frames_lock:
        _frames[slot] = (time.monotonic(), response.content)


def take_frames():
    """Return [(label, jpeg)] for recent frames, emptying the stash."""
    now = time.monotonic()
    images = []
    with _frames_lock:
        for slot, label in FRAME_SLOTS:
            stashed = _frames.get(slot)
            if stashed and now - stashed[0] <= FRAME_MAX_AGE:
                images.append((label, stashed[1]))
        _frames.clear()
    return images


@app.route("/events/speech", methods=["POST"])
@cross_origin(app)
def speech_event():
    """The ears reporting something. What to do about it is our decision -
    taking a photograph when someone speaks, or answering a poke."""
    payload = request.get_json(silent=True) or {}
    event = payload.get("event")

    if event == "poke":
        # A poke starts a session like any other trigger. Nothing was said, but
        # somebody pressed the button, so give them a few seconds to get into
        # frame if they want to be looked at - which is the same five seconds
        # that would let them step back into it.
        start_tracking()
        # On a thread: speaking blocks until the audio has finished, and the
        # publisher gives up after a few seconds, so answering inline would look
        # to the ears like an event that never arrived.
        answer = threading.Thread(target=speak, args=(POKE_REPLY,))
        answer.daemon = True
        answer.start()
        return json.dumps({'error_message': 'success', 'event': event})

    slot = {"speech.started": "started", "speech.ended": "ended"}.get(event)
    if slot is None:
        return json.dumps({'error_message': 'ignored', 'event': event})

    if event == "speech.started":
        # Someone is talking to him, which is the moment to start looking at
        # them. The ears know nothing about this, the same way they know
        # nothing about the camera - deciding what a speech event is worth is
        # this service's job.
        start_tracking()

    thread = threading.Thread(target=grab_frame, args=(slot,))
    thread.daemon = True
    thread.start()
    return json.dumps({'error_message': 'success', 'event': event})


@app.route("/", methods=["GET", "POST"])
@cross_origin(app)
def index():
    if request.method == "POST":
        gary_prompt = request.form["gary_prompt"]
        if request.form.get("include_image") in ("1", "true", "on", "yes"):
            # The same slot the ears fill at the end of an utterance: a typed
            # question arriving is the same moment as someone finishing
            # speaking. Synchronous here, where that path is threaded, because
            # the question has already arrived and the frame has to be in hand
            # before the prompt goes out. Anything already stashed belongs to an
            # exchange nobody finished, so it is dropped and exactly one is sent.
            take_frames()
            grab_frame("ended")
        images = take_frames()
        logging.info("prompt received, %d camera frame(s) available", len(images))

        think_gesture()
        result = google_generate_content(gary_prompt, images)

        head_flourish()
        speak(result)
        if not tracking_owns_the_reset():
            # No session running, so nothing else will put his head back.
            head_recenter()
        
        #cache for future discussions, using the prompt as the key, and the result as the value
        cache[gary_prompt] = result
        
        #for POST request, return a JSON result
        return json.dumps({'error_message': 'success', 'text': result }) #redirect(url_for("index", result=result))

    result = request.args.get("result")
    return render_template("index.html", result=result)

def google_generate_chat_content():
    json_data = []
    for key, value in cache.items():
        #print("previous prompt: " + key)
        json_data.append({
            "role": "user",
            "parts": [{ "text": key }]
            })
        json_data.append({
            "role": "model",
            "parts": [{ "text": value }]
            })
    return json_data

def user_parts(prompt, images=None):
    """Build the user turn: the spoken text, then any camera frames.

    Each frame is preceded by a short label so the model can tell them apart
    and place them in time.
    """
    parts = [{"text": prompt}]
    for label, data in (images or []):
        parts.append({"text": label})
        parts.append({"inline_data": {
            "mime_type": "image/jpeg",
            "data": base64.b64encode(data).decode("ascii"),
        }})
    return parts


def google_generate_content(prompt, images=None):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-lite-latest:generateContent"
    url += "?key=" + os.getenv("GOOGLE_API_KEY")
    
    json_chat = google_generate_chat_content()
    
    json_body = {
        "system_instruction": {
            "parts":
            { "text": "Respond as Gary the Robot. No robot sounds. \
Gary is a 3D printed robot in the Bottle Rocket Studios office in Addison, Texas. \
He gets moved around and does not know exactly where he is; work it out from your \
photographs if someone asks. \
Built in 2016 by Luke Wallace at the annual Rocket Science hackathon, when Calvin Carter \
was CEO and President. Matt Smith became President in November 2025. Luke has worked on \
him every hackathon since; he began as one arm and now has a head and waist. \
You may be given photographs from your camera. Use them only when asked about where you \
are or what you can see. Never mention your surroundings otherwise. \
Answer in 15 words or less."
              }
            },
        "contents": [ json_chat, { "role":"user", "parts": user_parts(prompt, images) }] }
    
    #print(json_body)

    json_body = json.dumps(json_body).encode('utf-8')

    # This call used to have no timeout, so a stalled connection hung for as
    # long as the network felt like it. Gemini normally answers in about a
    # second, but occasional ~33s stalls were measured from this Pi - not DNS,
    # not IPv6, and not an unreachable endpoint, all of which were ruled out,
    # so most likely something on the way out of the building. A bounded
    # attempt plus a retry turns a 33s wait into about GEMINI_TIMEOUT + 1s.
    last_error = None
    for attempt in range(GEMINI_ATTEMPTS):
        req = urllib.request.Request(url)
        req.add_header('Content-Type', 'application/json')
        req.add_header('Content-Length', len(json_body))
        try:
            with urllib.request.urlopen(req, json_body, timeout=GEMINI_TIMEOUT) as response:
                json_response = json.loads(response.read())
            first_response = json_response["candidates"][0]["content"]["parts"][0]["text"]
            print(first_response)
            return first_response
        except Exception as e:
            last_error = e
            logging.warning("gemini attempt %d failed: %s", attempt + 1, e)

    # Answer with something rather than raising, which would return a 500 and
    # leave Gary standing there silently. This also covers the 503s Gemini
    # returns under load.
    logging.error("gemini unreachable after %d attempts: %s",
                  GEMINI_ATTEMPTS, last_error)
    return "My brain is fuzzy right now. Ask me again in a moment."


def speak(text):
    json_body = {"text": text}
    try:
        response = requests.post("http://localhost:5000/api/voice/text2speech",
                                 json=json_body, timeout=SPEAK_TIMEOUT)
    except requests.exceptions.RequestException as e:
        # RequestException rather than ConnectionError: with a timeout set, a
        # servo API that accepts and then stalls raises Timeout, which is not a
        # ConnectionError and would otherwise escape as a traceback.
        print("\n\033[93mUnable to reach text2speech on port 5000: %s\033[0m" % e)
        return
    return #jsonify(response.json()) #doing nothing with response

# The mouth is not driven from here - the servo API opens and closes it around
# the speech itself, which keeps the timing tight and means anything that makes
# Gary talk gets a moving mouth. What is left is asking for movement at the
# right moments.
#
# The brain no longer knows a servo code, a percentage or a command format. It
# names a gesture and the servo API decides what that means, easing it rather
# than snapping. Everything these functions used to have to get right - the
# space that made two commands out of one, the `+` that made a value relative,
# the direction each axis travels - is now either the motion engine's problem
# or written down in `servo_api/gestures.yaml`. The comments warning about
# those traps went with them.
#
# What each gesture actually does is in that file, and can change without
# touching this service at all.
GESTURE_URL = os.environ.get("GARY_SERVO_URL",
                             "http://localhost:5000") + "/api/motion/gesture"
TRACKING_URL = VISION_URL + "/tracking"


def start_tracking():
    """Ask vision to begin, or extend, a tracking session.

    Every trigger calls this. Extending rather than restarting is what makes a
    follow-up question buy another session budget without interrupting the
    tracking already under way.

    Fire and forget, and a missing vision service is an ordinary condition
    rather than an error - plenty of InMoovs have no camera, which is the same
    reason grab_frame shrugs one off.
    """
    async_request(TRACKING_URL + "/start", {})


def tracking_owns_the_reset():
    """Whether a tracking session is running, and so will reset him itself.

    Asked rather than remembered, because the session's start and its end
    arrive as separate requests here. If one is running, this service must not
    also recenter: the session ends with its own body_reset when the person
    leaves, and a recenter on top would be a gesture outranking the tracker,
    reaching centre, and then handing the head straight back to a tracker still
    aimed at a face - undoing itself.
    """
    try:
        response = requests.get(TRACKING_URL, timeout=2)
        if response.status_code != 200:
            return False
        return bool(response.json()["tracking"]["open"])
    except (requests.exceptions.RequestException, ValueError, KeyError,
            TypeError):
        # No vision service, or it cannot answer. Then nothing is tracking and
        # the reset is ours, exactly as it was before tracking existed.
        return False


def play_gesture(name):
    """Ask for a named gesture. Fire and forget: a movement that fails to start
    is not a reason to hold up an answer, and the servo API logs it."""
    async_request(GESTURE_URL, {"name": name})


def head_flourish():
    play_gesture("head_flourish")


def think_gesture():
    play_gesture("think")


def head_recenter():
    play_gesture("head_recenter")


#fire and forget the request by using a separate thread
def async_request(url, json_body):
    threading.Thread(target=request_task, args=(url, json_body)).start()

def request_task(url, json_body):
    try:
        requests.post(url, json=json_body, timeout=ASYNC_TIMEOUT)
    except requests.exceptions.RequestException as e:
        # RequestException rather than ConnectionError: with a timeout set, a
        # service that accepts and then stalls raises Timeout, which is not a
        # ConnectionError. Uncaught, it would kill this thread with a traceback
        # instead of the one-line warning this was always meant to print.
        print("\n\033[93mUnable to reach "+url+": %s\033[0m" % e)
        return
    return

#def main():

# Every connection gets its own thread, and by default no socket timeout, so a
# client that vanishes without closing it (a dropped VPN, a sleeping laptop)
# leaves that thread blocked in recv() for good - the ears reached 323 threads
# that way. This reaps them. Slow requests are unaffected: the timeout is on
# socket reads, not on how long a handler may take, which matters here because
# this service holds a request open for a whole exchange.
#
# NOTE this is currently inert: the systemd unit starts this service with
# `flask run`, which never executes the __main__ block below, so the handler
# is never installed. It is kept for anyone running app.py directly, and
# because the leak it guards against cannot reach this service anyway - the
# brain listens on 127.0.0.1 only, so its clients are the other services on
# this Pi rather than browsers on a network that can drop. The four services
# that DO run their .py directly are the ones actually protected.
class IdleTimeoutHandler(WSGIRequestHandler):
    timeout = 30


if __name__ == "__main__":
    #main()
    app.run(debug=False, host="0.0.0.0", port=5001,
            request_handler=IdleTimeoutHandler)