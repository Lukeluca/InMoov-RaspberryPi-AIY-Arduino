#!/usr/bin/env python3
# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import time
import threading
import wave
import sys
import json
import requests

from aiy.board import Board, Led
from aiy.voice.audio import AudioFormat, play_wav, record_file, Recorder
from vosk import Model, KaldiRecognizer, SetLogLevel #pip3 install vosk

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--filename', '-f', default='recording.wav')
    args = parser.parse_args()

    model = Model(lang="en-us")

    with Board() as board:
        while True:
            print('Press button to start recording.')
            board.button.wait_for_press()
            board.led.state = Led.ON

            done = threading.Event()
            board.button.when_released = done.set

            def wait():
                start = time.monotonic()
                while not done.is_set():
                    board.led.state = Led.ON
                    #duration = time.monotonic() - start
                    #print('Recording: %.02f seconds [Release button to stop]' % duration)
                    #time.sleep(0.5)

            #record_file(AudioFormat.CD, filename=args.filename, wait=wait, filetype='wav')
            record_file(AudioFormat(sample_rate_hz=44100, num_channels=1, bytes_per_sample=2), filename=args.filename, wait=wait, filetype='wav')
            board.led.state = Led.OFF
            
            #print('Press button to play recorded sound.')
            #board.button.wait_for_press()

            #print('Playing...')
            #play_wav(args.filename)
            #print('Done.')
            
            text_output = ""
            print('Analyzing...')
            wf = wave.open(args.filename, "rb")
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getcomptype() != "NONE":
                print("Audio file must be WAV format mono PCM.")
                sys.exit(1)
            
            rec = KaldiRecognizer(model, wf.getframerate())
            rec.SetWords(True)
            rec.SetPartialWords(True)
            
            json_data = []
            text = ""

            while True:
                data = wf.readframes(4000)
                if len(data) == 0:
                    break
                if rec.AcceptWaveform(data):
                    #print('rec result:')
                    #print(rec.Result())
                    text += " " + json.loads(rec.Result()).get("text")
                else:
                #    print(rec.PartialResult())
                    text += " " + json.loads(rec.PartialResult()).get("text", "")
            #print('Final result:')
            #print(rec.FinalResult())
            text += " " + json.loads(rec.FinalResult()).get("text")
            print('formatted result')
            text = text.strip()
            print(text)

            #Send data to garyGPT
            try:
                form_data = { 'gary_prompt': text }
                response = requests.post("http://localhost:5001/", data=form_data)
            except requests.exceptions.ConnectionError as e:
                print("\n\033[93m Unable to reach myself on port 5001 \033[0m") 
                continue
             

if __name__ == '__main__':
    main()
