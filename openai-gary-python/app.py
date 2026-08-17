import os
import logging
import json
import jsonify
import random
import requests
import urllib.request
import re

import openai
#import google.generativeai as genai
import cachetools
import threading

from flask import Flask, redirect, render_template, request, url_for
from flask_cors import CORS, cross_origin
from aiy.board import Board, Led


app = Flask(__name__)
openai.api_key = os.getenv("OPENAI_API_KEY")
#genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# Create a 5 minute cache for the conversations
cache = cachetools.TTLCache(maxsize=10, ttl=300)

@app.route("/", methods=["GET", "POST"])
@cross_origin(app)
def index():
    if request.method == "POST":
        gary_prompt = request.form["gary_prompt"]
        #result = openai_chat_completion(gary_prompt)
        result = google_generate_content(gary_prompt)
        #result = br_insight_completion(gary_prompt)
        
        open_mouth()
        speak(result)
        close_mouth()
        
        #cache for future discussions, using the prompt as the key, and the result as the value
        cache[gary_prompt] = result
        
        #for POST request, return a JSON result
        return json.dumps({'error_message': 'success', 'text': result }) #redirect(url_for("index", result=result))

    result = request.args.get("result")
    return render_template("index.html", result=result)

def br_insight_completion(prompt):
    json_body = {"question": prompt}
    try:
        response = requests.post("https://internal-endpoint.example.com/AskJana", json=json_body)
    except requests.exceptions.ConnectionError as e:
        print("\n\033[93mUnable to reach BR Insight \033[0m") 
        return "Jana says no"
    #return #jsonify(response.json()) #doing nothing with response
    print(response.text)
    return "Jana says yes"

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


#not currently used, this was the original one-shot completion
def openai_completion(prompt):
    response = openai.Completion.create(
        model="text-davinci-002",
        prompt=generate_prompt(prompt),
        temperature=0.6,
        max_tokens=1024
    )
    logging.info(json.dumps(response,indent=4))
    result=response.choices[0].text
    result=re.sub(r'[\x00-\x1f]', '', result).strip()
    
    return result

def openai_chat_completion(prompt):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=generate_messages(cache,prompt),
            temperature=0.6,
            max_tokens=1024
        )
        result=response.choices[0].message.content
        result=re.sub(r'[\x00-\x1f]', '', result).strip()
    except openai.error.APIError as e:
        result = "My brain is fuzzy right now. I'm sorry about that, I still love you."
        pass

            
    return result

def generate_prompt(gary_prompt):
    return """Respond to all queries as Gary the Robot. Gary the Robot is a 3D printed robot who lives in the garage
workshop of Bottle Rocket Studios, based in Addison, Texas. Gary was created in 2016 by Luke Wallace during the
annual Rocket Science hackathon. At the time, the CEO & President of Bottle Rocket Studios was Calvin Carter. The current
CEO of Bottle Rocket Studios is Rajesh Midha, and the President is Andrew Sevin. Luke worked on Gary every year
of Rocket Science, the annual hackathon. He started as only an arm, but has grown to have a head, a waist, and
everything in between. It is currently October 2023, making Gary 7 years old.

Now respond to the following statement, in 30 words or less: {}""".format(
        gary_prompt
    )

#creates openAI messages object from a cache
def generate_messages(cache, prompt):
    json_data = []
    json_data.append({
        "role": "system",
        "content": "Respond to all queries as Gary the Robot. Gary the Robot is a 3D printed robot who lives in the garage \
workshop of Bottle Rocket Studios, based in Addison, Texas. Gary was created in 2016 by Luke Wallace during the \
annual Rocket Science hackathon. At the time, the CEO & President of Bottle Rocket Studios was Calvin Carter. Luke worked on Gary every year \
 of Rocket Science, the annual hackathon. He started as only an arm, but has grown to have a head, a waist, and \
 everything in between."
        })
    json_data.append({"role": "system", "content": "The current CEO of Bottle Rocket Studios is Rajesh Midha, \
and the current President of Bottle Rocket Studios is Andrew Sevin. "})
    json_data.append({"role": "system", "content": "It is currently Novembers 2023, making Gary 7 years old."})
    json_data.append({"role": "system", "content": "Your body allows you to open and close your mouth, and move your head."})
    json_data.append({"role": "system", "content": "You cannot currently move your arms or hands."})
    json_data.append({"role": "system", "content": "Bottle Rocket Studios is also called Bottle Rocket."})
    json_data.append({"role": "system", "content": "Bottle Rocket Studios is a subsidiary of Ogilvy and Mather."})
    json_data.append({"role": "system", "content": "Always respond in 30 words or less."})
    for key, value in cache.items():
        #print("previous prompt: " + key)
        json_data.append({
            "role": "user",
            "content": key
            })
        json_data.append({
            "role": "assistant",
            "content": value
            })
    
    #print("current prompt:" + prompt)
    json_data.append({
        "role": "user",
        "content": prompt
        })
    return json_data

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
    app.run(debug=True, host="0.0.0.0", port=5001)