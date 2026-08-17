import os
import logging
import json
#import jsonify
import requests

import openai #pip3 install openai==1.39.0
from flask import Flask, redirect, render_template, request, url_for
from aiy.board import Board, Led
#from aiy.cloudspeech import CloudSpeechClient

#app = Flask(__name__)
openai.api_key = os.getenv("OPENAI_API_KEY")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def main():
    print ('LED on when button pressed for GarySpeech')
    #client = CloudSpeechClient()
    with Board() as board:
        while True:
            board.button.wait_for_press()
            print('ON')
            board.led.state = Led.ON
            
            text = "Hi"
            #text = client.recognize(language_code="en_us")
            if text is None:
                text = '' #'Please say I did not hear anything'
                
            text = text.lower()
            print(text)
            #board.button.wait_for_release()
            print('OFF')
            board.led.state = Led.OFF
            
            #Send data to garyGPT
            try:
                form_data = { 'gary_prompt': text }
                response = requests.post("http://localhost:5001/", data=form_data)
            except requests.exceptions.ConnectionError as e:
                print("\n\033[93m Unable to reach myself on port 5001 \033[0m") 
                continue
             

if __name__ == "__main__":
    main()
   # app.run(debug=True, host="0.0.0.0", port=5002)
