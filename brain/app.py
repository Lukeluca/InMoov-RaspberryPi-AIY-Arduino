import os
import logging
import json
import jsonify
import random
import requests
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

@app.route("/", methods=["GET", "POST"])
@cross_origin(app)
def index():
    if request.method == "POST":
        gary_prompt = request.form["gary_prompt"]
        result = google_generate_content(gary_prompt)

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

def google_generate_content(prompt):
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
Try to answer in 15 words or less."
              }
            },
        "contents": [ json_chat, { "role":"user", "parts":[{"text": prompt}] }] }
    
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