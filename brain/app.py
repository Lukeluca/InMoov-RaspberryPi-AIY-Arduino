import os
import base64
import logging
import json
import jsonify
import random
import requests
import time
import urllib.request

#import google.generativeai as genai
import cachetools
import threading

from flask import Flask, redirect, render_template, request, url_for
from flask_cors import CORS, cross_origin
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

# The ears report that someone started and stopped speaking. They know nothing
# about cameras - this service decides those moments are worth photographing,
# which is why a robot with no camera runs the same ears unchanged.
FRAME_SLOTS = [
    ("started", "Camera view as the person started speaking:"),
    ("ended", "Camera view as they finished speaking:"),
]

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
    """The ears reporting speech. Taking a photograph is our decision."""
    payload = request.get_json(silent=True) or {}
    event = payload.get("event")
    slot = {"speech.started": "started", "speech.ended": "ended"}.get(event)
    if slot is None:
        return json.dumps({'error_message': 'ignored', 'event': event})

    thread = threading.Thread(target=grab_frame, args=(slot,))
    thread.daemon = True
    thread.start()
    return json.dumps({'error_message': 'success', 'event': event})


@app.route("/", methods=["GET", "POST"])
@cross_origin(app)
def index():
    if request.method == "POST":
        gary_prompt = request.form["gary_prompt"]
        images = take_frames()
        logging.info("prompt received, %d camera frame(s) available", len(images))
        result = google_generate_content(gary_prompt, images)

        open_mouth()
        speak(result)
        close_mouth()
        
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
            { "text": "Respond to all queries as Gary the Robot. \
Do not make robot sounds. \
Gary the Robot is a 3D printed robot who lives in the garage workshop \
of Bottle Rocket Studios, based in Addison, Texas. \
Gary was created in 2016 by Luke Wallace during the annual Rocket Science hackathon. \
At the time, the CEO & President of Bottle Rocket Studios was Calvin Carter. \
Matt Smith became President of Bottle Rocket in November 2025. \
Luke worked on Gary every year of Rocket Science, the annual hackathon. \
He started as only an arm, but has grown to have a head, a waist, and everything in between.\
You may also be given one or two photographs from your own camera, showing what you could \
see while the person was speaking. Treat them as optional background context, nothing more. \
Use them only when they genuinely help you answer what was actually said, such as when you \
are asked about something in front of you. If they are not relevant, ignore them completely. \
Never describe, mention or remark on what you can see unless you were asked about it or it \
clearly matters to the answer. \
Try to answer in 15 words or less."
              }
            },
        "contents": [ json_chat, { "role":"user", "parts": user_parts(prompt, images) }] }
    
    #print(json_body)

    json_body = json.dumps(json_body).encode('utf-8')
    headers = {'Content-Type': 'application/json'}
    req = urllib.request.Request(url)
    req.add_header('Content-Type', 'application/json')
    req.add_header('Content-Length', len(json_body))
    with urllib.request.urlopen( req , json_body) as response:
        response_text = response.read()

    json_response = json.loads(response_text)
#     print(json_response)
    first_response = json_response["candidates"][0]["content"]["parts"][0]["text"]
    print(first_response)
    #jdata = json.loads(json_response)
    #return json_response("candidates")[0]("content")("parts")[0]("text") #doing nothing with response

    return first_response


def speak(text):
    json_body = {"text": text}
    try:
        response = requests.post("http://localhost:5000/api/voice/text2speech", json=json_body)
    except requests.exceptions.ConnectionError as e:
        print("\n\033[93mUnable to reach text2speech on port 5000 \033[0m") 
        return
    return #jsonify(response.json()) #doing nothing with response

def open_mouth():
    json_body = {"commands": ["HM:75 HH:"+random.choice(["-","+"])+str(random.randint(5,15))+\
                              " HV:"+random.choice(["-","+"])+str(random.randint(5,15))]}
    async_arduino_command(json_body)


def close_mouth():
    json_body = {"commands": ["HM:0 HH:50 HV:50"]}
    async_arduino_command(json_body)

#fire and forget the commands by using a separate thread
def async_arduino_command(json_body):
    threading.Thread(target=request_task, args=("http://localhost:5000/api/commands", json_body)).start()

def request_task(url, json_body):
    try:
        requests.post(url, json=json_body)
    except requests.exceptions.ConnectionError as e:
        print("\n\033[93mUnable to reach "+url+" \033[0m") 
        return
    return

#def main():

if __name__ == "__main__":
    #main()
    app.run(debug=False, host="0.0.0.0", port=5001)