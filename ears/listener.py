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

"""Gary's ears.

Owns the microphone and transcribes speech locally with Vosk. It records only
while a trigger says to - Gary is never listening continuously.

It knows nothing about cameras, servos or language models. It announces two
facts about speech and posts the transcript; what anyone does with those is
their business. That is what lets the brain take a photograph when someone
starts talking without this service knowing a camera exists, and lets a robot
built without a camera run this code unchanged.

    speech.started   someone began speaking
    speech.ended     they stopped
    POST <brain>/    the transcript
"""

import argparse
import os
import time
import threading
import wave
import sys
import json
import requests

from aiy.voice.audio import AudioFormat, play_wav, record_file, Recorder
from vosk import Model, KaldiRecognizer, SetLogLevel #pip3 install vosk

from triggers import build_triggers

BRAIN_URL = os.environ.get("GARY_BRAIN_URL", "http://localhost:5001")
# Whoever wants to know about speech. The brain by default; add more, comma
# separated, and they all get the same events.
SUBSCRIBERS = [u.strip() for u in os.environ.get(
    "GARY_EVENT_SUBSCRIBERS", "http://localhost:5001/events/speech").split(",")
    if u.strip()]
EVENT_TIMEOUT = 3


def publish(event):
    """Announce a speech event. Fire and forget, on background threads.

    Nothing here may delay recording or take the service down: a subscriber
    that is slow, missing or broken must not stop Gary from listening.
    """
    def send(url):
        try:
            requests.post(url, json={"event": event}, timeout=EVENT_TIMEOUT)
        except requests.exceptions.RequestException as e:
            print("event %s not delivered to %s: %s" % (event, url, e))

    for url in SUBSCRIBERS:
        thread = threading.Thread(target=send, args=(url,))
        thread.daemon = True
        thread.start()


def transcribe(model, filename):
    """Run Vosk over the recording and return the text."""
    wf = wave.open(filename, "rb")
    if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getcomptype() != "NONE":
        print("Audio file must be WAV format mono PCM.")
        sys.exit(1)

    rec = KaldiRecognizer(model, wf.getframerate())
    rec.SetWords(True)
    rec.SetPartialWords(True)

    text = ""
    while True:
        data = wf.readframes(4000)
        if len(data) == 0:
            break
        if rec.AcceptWaveform(data):
            text += " " + json.loads(rec.Result()).get("text")
        else:
            text += " " + json.loads(rec.PartialResult()).get("text", "")
    text += " " + json.loads(rec.FinalResult()).get("text")
    return text.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--filename', '-f', default='recording.wav')
    args = parser.parse_args()

    model = Model(lang="en-us")

    started = threading.Event()
    ended = threading.Event()
    triggers = build_triggers(started, ended)
    if not triggers:
        print("no usable trigger, nothing can start a recording - set GARY_TRIGGER")
        return

    print("listening on trigger(s): %s" % ", ".join(t.name for t in triggers))
    for trigger in triggers:
        trigger.start()

    try:
        while True:
            print('Waiting for the trigger to start recording.')
            started.wait()
            started.clear()

            for trigger in triggers:
                trigger.indicate(True)
            publish("speech.started")

            def wait():
                # The sleep matters: a bare spin holds the GIL for the whole
                # recording and starves the threads publishing events.
                while not ended.is_set():
                    time.sleep(0.05)

            record_file(AudioFormat(sample_rate_hz=44100, num_channels=1,
                                    bytes_per_sample=2),
                        filename=args.filename, wait=wait, filetype='wav')

            for trigger in triggers:
                trigger.indicate(False)
            publish("speech.ended")

            print('Analyzing...')
            text = transcribe(model, args.filename)
            print(text)

            if not text:
                print('nothing recognised, not sending')
                continue

            try:
                requests.post(BRAIN_URL + "/", data={'gary_prompt': text})
            except requests.exceptions.ConnectionError:
                print("\n\033[93m Unable to reach the brain on %s \033[0m" % BRAIN_URL)
                continue
    finally:
        for trigger in triggers:
            trigger.close()


if __name__ == '__main__':
    main()
