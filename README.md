# Gary — an InMoov robot brain

Software for [Gary](https://inmoov.fr/), a 3D-printed InMoov humanoid. A Raspberry Pi
handles conversation, speech and vision; an Arduino drives the servos. Press a button,
talk to him, and he answers out loud while moving his mouth and head.

This started as a hackathon project in 2016 and has been rebuilt piecemeal ever since.
It is shared in the hope that it is useful to other InMoov builders — but read
[Known limitations](#known-limitations) first, because parts of it assume specific
hardware.

## How it works

```mermaid
flowchart LR
    B([Trigger]) --> R[ears :5003<br/>Vosk, offline]
    R -->|speech.started / ended| N[brain<br/>:5001]
    R -->|POST transcript| N
    N -->|GET frame.jpg| V[vision :5002]
    V -->|frames| N
    N -->|prompt| G([Gemini API])
    G -->|reply| N
    N -->|POST /api/commands| S[servo_api<br/>:5000]
    N -->|POST /api/voice/text2speech| S
    S -->|serial 115200| A([Arduino → servos])
    S --> SPK([Speaker])
```

Five processes, started together by `scripts/start_all.sh`:

| Service | Port | Responsibility |
|---|---|---|
| `servo_api/server.py` | 5000 | Owns the Arduino serial link; moves servos; text-to-speech |
| `brain/app.py` | 5001 | Coordinates. Prompts Gemini, decides when to look, drives speech |
| `vision/server.py` | 5002 | Owns the camera; serves frames to anything that needs to see |
| `ears/listener.py` | 5003 | Owns the microphone; triggers, recording, offline transcription |
| `console/server.py` | 5004 | Remote control page; proxies to the others |

Each hardware resource has exactly one owning service — serial port, camera,
microphone and button — because each can only be held by one process. Anything
else that needs them asks over HTTP.

A full exchange takes roughly 8 seconds: about 3 for the model, about 5 for speech.

**Start the servo API first.** The brain calls back into it for both speech and
servo movement, and will fail silently if it isn't listening.

## Hardware

- Raspberry Pi 4 (developed on Raspberry Pi OS 10 "buster", Python 3.7)
- Arduino with an Adafruit PCA9685 PWM driver, connected over USB serial at 115200 baud
- Google AIY Voice HAT — supplies the button, the LED and text-to-speech
- Raspberry Pi Camera Module on the CSI ribbon connector (optional; see limitations)

The Arduino firmware is **not** in this repository. It lives in
[ArduinoServoController-UART-InMoov](https://github.com/Lukeluca/ArduinoServoController-UART-InMoov),
and is treated here as a fixed API: the Pi sends it short ASCII commands (`HH:60`,
`HM+15`) over serial, two letters for the servo plus an absolute or relative value.
See `servo_api/pin_and_servos.py` for the servo map.

## Setup

First flash the Arduino with
[ArduinoServoController-UART-InMoov](https://github.com/Lukeluca/ArduinoServoController-UART-InMoov)
— nothing here can move a servo without it. Then:

```bash
git clone <your-fork> gary
cd gary
cp brain/.env.example brain/.env
```

Add your [Google AI Studio key](https://aistudio.google.com/apikey) to `brain/.env`
as `GOOGLE_API_KEY`. Then install dependencies:

```bash
pip3 install -r servo_api/requirements.txt -r brain/requirements.txt \
            -r ears/requirements.txt -r vision/requirements.txt \
            -r console/requirements.txt
```

The AIY packages (`aiy.board`, `aiy.voice`) come from the Voice Kit system image, not
from pip.

## Running

```bash
./scripts/start_all.sh
```

Press and hold the button, speak, then release. Release is what ends the recording.

To verify the pipeline without a button press:

```bash
curl -X POST http://localhost:5001/ --data-urlencode "gary_prompt=say hello"
```

To stop everything:

```bash
pkill -f "[s]erver.py|[f]lask run|[l]istener.py"
```

### Starting automatically on power-on

```bash
./scripts/install-systemd.sh
systemctl --user start gary.target
```

That installs five units plus a `gary.target` that groups them, and enables
lingering so they come up at power-on without anyone logging in.

```bash
systemctl --user status gary-servo     # one service
systemctl --user stop gary.target      # all of them
sudo journalctl --user-unit gary-brain -f
```

These are deliberately **user** units rather than system units. The AIY
voiceHAT is held exclusively by PulseAudio, which runs inside the desktop
user's systemd session, so a system-scope service cannot reach the sound card:
`aplay` fails with "Device or resource busy" and text-to-speech returns a 500
while every other part of the pipeline looks healthy. Gary moves his mouth and
makes no sound. Running in the user session avoids that entirely.

## Vision

`vision/` owns the camera. Only one process can hold `/dev/video0`, so nothing
else opens it — everything that needs to see asks this service.

```
GET  /health           service and camera state
GET  /frame.jpg        one frame, as an image
GET  /frames?count=3   several frames, base64, for a multimodal prompt
POST /camera/release   drop the camera now, e.g. to free it for raspistill
```

The camera is opened on first use and released again after 30 seconds idle
(`GARY_CAMERA_IDLE_TIMEOUT`). Holding it open permanently would mean V4L2
queues buffers nobody reads, so a request after a long gap returns a frame
from seconds earlier; it also leaves a handle exposed to the legacy driver
wedging, and keeps the module's LED lit, which in a shared room reads as a
camera that is always recording. Measured on a Pi 4 with the v1 camera:

| | |
|---|---|
| Cold capture (opens the camera, 1.5s warmup) | ~2.0s |
| Warm capture (already open) | ~0.05s |
| Draining stale buffers after an idle gap | negligible |

That 40x gap is why a burst of frames is served without closing in between.
Components that genuinely need the camera held open — head tracking, when it
arrives — take a lease, which suppresses the idle release while held.

Several images can go into a single Gemini request at roughly 1,100 tokens
each, and labelling them in the prompt works, so `/frames` is useful for a
wider effective view than the camera's field of view allows, or for asking
what changed between two moments.

### What Gary sees when you talk to him

The ears announce two facts — `speech.started` and `speech.ended` — and the
brain decides those moments are worth photographing. It grabs a frame on each,
holds them briefly, and attaches them to the next prompt.

The ears know nothing about cameras. That is deliberate: a robot built without
one runs the same ears unchanged, and the brain simply finds no vision service
and carries on with audio alone.

The frames are given to the model as *optional* context. The system prompt tells
Gary to use them only when they help answer what was actually said, and never to
remark on what he can see otherwise — so "how are you?" gets a normal answer
while "what is on the table?" gets a real one.

## Voice

Gary speaks through Piper, a small neural text-to-speech model, falling back to
the AIY image's Pico voice if Piper is not installed. Pico dates from 2013 and
sounds like it.

```bash
./scripts/install-piper.sh
systemctl --user restart gary-servo
```

Everything lands in `~/gary-tts`; delete that directory to go back to Pico.
`servo_api/tts.py` picks the engine automatically, so a robot without Piper
still talks.

On 32-bit Raspberry Pi OS the install is not straightforward, and the script
explains why at length. In short: current Piper needs Python 3.9+ and only
ships 64-bit ARM wheels, and every standalone armv7 build - back to v1.0.0 -
needs a newer glibc than buster provides. The script fetches a bullseye glibc
into its own directory and runs Piper under that loader, leaving the system
alone.

| | Pico | Piper `alan-low`, `length_scale 0.75` |
|---|---|---|
| Synthesis | 0.12s | ~2.05s |
| Playback | 5.09s | 4.47s |
| Total per reply | ~5.4s | ~6.7s |

Piper is kept resident, because loading the voice costs ~1.5s and spawning per
reply pays it every time. Synthesis writes a file rather than streaming, which
is a deliberate choice measured on this Pi - `servo_api/tts.py` documents the
reasoning and what revisiting it would involve.

Voice and speed are configurable with `GARY_PIPER_VOICE` and
`GARY_PIPER_LENGTH_SCALE`; below 1.0 is faster speech.

## Remote console

`http://<pi>:5004/` — a single page showing what Gary sees, with two boxes:

- **Ask Gary** goes to Gemini; he answers aloud and moves his mouth
- **Gary says** speaks text verbatim, with no model involved

The view polls `/api/frame.jpg` every two seconds rather than streaming, so the
vision service still releases the camera when nobody is watching. Polling stops
when the browser tab is hidden — otherwise a forgotten tab would hold the
camera open and leave the LED lit with nobody actually looking. A live stream
would be a later addition, taking a camera lease for the life of the
connection.

The console proxies rather than letting the browser call each service. That
keeps the brain bound to localhost instead of exposed to the network, avoids
CORS entirely, and keeps the user interface out of the services that own
hardware. The page has no external assets, so it renders on a robot with no
internet.

**There is no authentication.** Anyone who can reach port 5004 can move Gary
and make him speak. Consider where it is bound before exposing it.

### Triggers

Gary is never listening continuously; the ears record only when a trigger says
to. `GARY_TRIGGER` selects which, comma separated:

| | |
|---|---|
| `aiy` | The Voice HAT's button. Hold to talk. The default |
| `http` | `POST :5003/listen/start` and `/listen/stop`, so the brain can ask Gary to listen |
| `keyboard` | Enter to start, Enter to stop. For development, or a robot with no button |

A trigger that cannot be constructed — no AIY board present, say — is skipped
with a warning rather than stopping the service, so a partly equipped robot
still works. This is the seam to extend for a wake word or a GPIO button.

## Configuration

The model is set in `brain/app.py`. It currently uses `gemini-flash-lite-latest` —
an alias rather than a pinned version, deliberately: this project previously broke
when `gemini-1.5-flash` was retired, and pinning is not a safe harbour, since
retired models can be closed to new users entirely.

Gary's personality lives in the `system_instruction` string in
`brain/app.py:google_generate_content`. Change it there to give your robot its own
character.

## Known limitations

These are real and worth understanding before you invest time:

- **Head tracking is not built yet.** The camera works and `vision/` serves frames,
  but nothing follows a face. The old `Sight` class in `servo_api/gary_api.py` is
  still commented out; it contains working Haar-cascade detection and head-tracking
  worth salvaging. It does not need a newer OpenCV, despite the `cv2.data`
  reference that broke it — that attribute only returns the path to the bundled
  cascade files, and the system OpenCV 3.2 loads a cascade fine from an explicit
  path. It does need rewriting to send servo commands over HTTP rather than opening
  a second connection to the serial port.
- **Requires the AIY Voice HAT.** The button, LED and text-to-speech all come from
  `aiy.*`. Without that discontinued board the code fails at import. Making this
  portable means abstracting a trigger source and a TTS backend.
- **The mouth does not articulate.** It opens, holds through the whole utterance,
  then closes — there is no lip-sync.
- **`archive/`** holds superseded code kept for reference. It is not wired up.

## Layout

```
brain/       conversation: Gemini, speech recognition, web UI
servo_api/   motion and speech output; owns the Arduino serial link
scripts/     start_all.sh
archive/     superseded earlier versions, not in use
```

## Licence

MIT — see [LICENSE](LICENSE).
