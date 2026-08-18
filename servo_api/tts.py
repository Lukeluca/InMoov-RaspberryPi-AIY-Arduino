"""Turning Gary's words into sound.

Two engines. Piper is a small neural model and sounds like a person; Pico is
the 2013 formant synthesiser that ships with the AIY image and sounds like a
satnav. Piper is used when it is installed, and Pico otherwise, so a robot
without Piper still talks.

Install Piper with scripts/install-piper.sh. On 32-bit Raspberry Pi OS that
involves a glibc shim - see the script, it is not optional and it is not
obvious.

Configuration, all optional:

    GARY_TTS_ENGINE          auto (default), piper, or pico
    GARY_PIPER_BIN           wrapper written by the installer
    GARY_PIPER_VOICE         path to a .onnx voice
    GARY_PIPER_LENGTH_SCALE  phoneme length; below 1.0 is faster speech
    GARY_ALSA_DEVICE         aplay device, default "default"


Why this synthesizes to a file rather than streaming
----------------------------------------------------
Piper can stream raw audio with --output_raw, which would let playback begin
before synthesis finishes. It is deliberately not used, because measurement on
this Pi showed it would not help:

    one sentence     4.44s of audio, first bytes at 2.10s, all at once
    three sentences  5.72s of audio, first bytes at 0.47s, progressively

Piper synthesizes a whole sentence before emitting any of it. Gary's system
prompt asks for answers of fifteen words or less, which is nearly always one
sentence, so streaming and file mode finish at the same moment. Streaming would
only add complexity.

Piper is already kept resident here, which is a separate concern: loading the
voice costs ~1.5s and spawning per reply pays it every time (3.51s per
utterance one-shot versus 2.05s resident). File mode needs that just as much as
streaming would.

If you want to revisit streaming, the pieces are:

  * pipe `--output_raw` into `aplay -r <rate> -f S16_LE -c 1`, where rate comes
    from the voice's .onnx.json (16000 for alan-low, 22050 for medium voices),
  * work out when speech has actually ended. Today check_call returns when
    aplay exits, and servo_api closes the mouth on that. With a stream you must
    wait on aplay rather than on Piper, or the jaw shuts while he is still
    talking.

And note the real lever: because chunking is per sentence, a prompt that asks
for two or three short sentences instead of one long one gets audio started in
about half a second rather than two. That is a prompt change, not a code
change, and it is worth more than the streaming machinery.
"""

import os
import subprocess
import tempfile
import threading

ENGINE = os.environ.get("GARY_TTS_ENGINE", "auto")
TTS_HOME = os.environ.get("GARY_TTS_HOME", os.path.expanduser("~/gary-tts"))
PIPER_BIN = os.environ.get("GARY_PIPER_BIN", TTS_HOME + "/piper.sh")
PIPER_VOICE = os.environ.get("GARY_PIPER_VOICE",
                             TTS_HOME + "/voices/en_GB-alan-low.onnx")
LENGTH_SCALE = os.environ.get("GARY_PIPER_LENGTH_SCALE", "0.75")
ALSA_DEVICE = os.environ.get("GARY_ALSA_DEVICE", "default")

# tmpfs if it exists, so a wav per utterance does not chew at the SD card.
_RUN_DIR = "/run/user/%d" % os.getuid()
TMP_DIR = _RUN_DIR if os.path.isdir(_RUN_DIR) else tempfile.gettempdir()


def piper_available():
    return os.path.exists(PIPER_BIN) and os.path.exists(PIPER_VOICE)


def engine_name():
    """Which engine speak() will actually use."""
    if ENGINE == "pico":
        return "pico"
    if ENGINE == "piper":
        return "piper" if piper_available() else "piper (MISSING - will fail)"
    return "piper" if piper_available() else "pico"


class _ResidentPiper(object):
    """One Piper process kept alive across utterances.

    Loading the voice takes about 1.5s. Spawning Piper per reply pays that
    every time - measured at 3.51s per utterance one-shot versus 2.05s
    resident, on the same sentence. This is unrelated to streaming: file mode
    benefits from it just as much.

    Piper reads a line of text on stdin, writes a wav into --output_dir, and
    logs a line to stderr when it has finished. That stderr line is the only
    signal that an utterance is complete, so it is what we wait on.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._dir = os.path.join(TMP_DIR, "gary-piper")

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self):
        if not os.path.isdir(self._dir):
            os.makedirs(self._dir)
        self._proc = subprocess.Popen(
            [PIPER_BIN, "--model", PIPER_VOICE,
             "--length_scale", LENGTH_SCALE, "--output_dir", self._dir],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
        while True:
            line = self._proc.stderr.readline().decode("utf-8", "replace")
            if not line:
                raise RuntimeError("piper exited while starting up")
            if "Initialized piper" in line:
                return

    def synthesize(self, text):
        """Return the path of a wav holding `text`, spoken."""
        with self._lock:
            if not self._alive():
                self._spawn()
            self._proc.stdin.write((text.replace("\n", " ") + "\n").encode("utf-8"))
            self._proc.stdin.flush()
            while True:
                line = self._proc.stderr.readline().decode("utf-8", "replace")
                if not line:
                    self._proc = None
                    raise RuntimeError("piper exited mid-utterance")
                if "Real-time factor" in line:
                    break
            names = [os.path.join(self._dir, n) for n in os.listdir(self._dir)
                     if n.endswith(".wav")]
            if not names:
                raise RuntimeError("piper produced no audio")
            return max(names, key=os.path.getmtime)


_piper = _ResidentPiper()


def _speak_piper(text):
    try:
        path = _piper.synthesize(text)
    except Exception as e:
        # A one-shot run is slower but self-contained, so a wedged resident
        # process degrades the voice rather than silencing Gary.
        print("resident piper failed (%s), falling back to one-shot" % e)
        return _speak_piper_once(text)
    try:
        subprocess.check_call(["aplay", "-q", "-D", ALSA_DEVICE, path])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _speak_piper_once(text):
    """Spawn piper for a single utterance. The fallback path."""
    handle = tempfile.NamedTemporaryFile(suffix=".wav", dir=TMP_DIR, delete=False)
    handle.close()
    try:
        subprocess.run(
            [PIPER_BIN, "--model", PIPER_VOICE,
             "--length_scale", LENGTH_SCALE, "--output_file", handle.name],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.check_call(["aplay", "-q", "-D", ALSA_DEVICE, handle.name])
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _speak_pico(text):
    # The AIY helper, which is what Gary used before Piper. Its own defaults
    # lower the pitch a little, which suited the old voice.
    import aiy.voice.tts
    aiy.voice.tts.say(text, lang="en-GB", volume=50, pitch=69, speed=125)


def speak(text):
    """Say something out loud. Blocks until the audio has finished playing."""
    if ENGINE == "pico" or (ENGINE == "auto" and not piper_available()):
        return _speak_pico(text)
    return _speak_piper(text)
