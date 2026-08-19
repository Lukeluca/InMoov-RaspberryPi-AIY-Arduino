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
    GARY_TTS_VOLUME          playback gain 0.0-1.0, default 0.35
    GARY_SETTINGS_FILE       where a volume set at runtime is remembered

Volume is the one setting that can be changed while Gary is running, because it
is the one you tune by standing in the room and listening. set_volume() writes
it to the settings file and play() reads that file per utterance, so a change
takes effect on the next thing he says with nothing restarted. The environment
variable is the default for a robot that has never had it set.


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

import audioop
import contextlib
import os
import subprocess
import tempfile
import threading
import wave

ENGINE = os.environ.get("GARY_TTS_ENGINE", "auto")
TTS_HOME = os.environ.get("GARY_TTS_HOME", os.path.expanduser("~/gary-tts"))
PIPER_BIN = os.environ.get("GARY_PIPER_BIN", TTS_HOME + "/piper.sh")
PIPER_VOICE = os.environ.get("GARY_PIPER_VOICE",
                             TTS_HOME + "/voices/en_GB-alan-low.onnx")
LENGTH_SCALE = os.environ.get("GARY_PIPER_LENGTH_SCALE", "0.75")
ALSA_DEVICE = os.environ.get("GARY_ALSA_DEVICE", "default")

# Playback gain, 0.0 to 1.0. Applied to the samples, not the system mixer. Only
# the default: what the settings file holds wins, see volume().
DEFAULT_VOLUME = float(os.environ.get("GARY_TTS_VOLUME", "0.35"))

# Settings changed at runtime live here, in KEY=VALUE lines. Beside this module
# rather than in the working directory, so it does not matter who started the
# service or from where. It is a .env, which .gitignore already excludes.
SETTINGS_FILE = os.environ.get(
    "GARY_SETTINGS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
VOLUME_KEY = "GARY_TTS_VOLUME"

# Two callers can set the volume at once - Flask is threaded - and the file is
# read-modify-written, so the write needs to be serialised.
_settings_lock = threading.Lock()

# tmpfs if it exists, so a wav per utterance does not chew at the SD card.
_RUN_DIR = "/run/user/%d" % os.getuid()
TMP_DIR = _RUN_DIR if os.path.isdir(_RUN_DIR) else tempfile.gettempdir()


def _clamp_volume(value):
    """A gain in 0.0-1.0. Raises ValueError on something that is not a number."""
    return max(0.0, min(1.0, float(value)))


def _read_setting(key):
    """One KEY=VALUE from the settings file, or None if it is not in there.

    Parsed by hand rather than with python-dotenv, to keep a dependency out of
    the speaking path that the servo API needs nowhere else. Blank lines and
    comments are ignored; the last assignment wins, as a shell would have it.
    """
    try:
        with open(SETTINGS_FILE) as handle:
            lines = handle.readlines()
    except IOError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            return value.strip().strip("'\"")
    return None


def volume():
    """The gain to play at: what was last set, else the environment default.

    Read per utterance rather than cached, so setting it takes effect on the
    next thing Gary says, and so editing the file by hand works too.
    """
    stored = _read_setting(VOLUME_KEY)
    if stored is None:
        return DEFAULT_VOLUME
    try:
        return _clamp_volume(stored)
    except ValueError:
        # A hand-edited file should not silence him, so fall back rather than
        # raise out of the middle of an utterance.
        print("ignoring unreadable %s in %s: %r"
              % (VOLUME_KEY, SETTINGS_FILE, stored))
        return DEFAULT_VOLUME


def set_volume(value):
    """Store the playback gain and return what was stored, clamped to 0.0-1.0.

    Only the one assignment is rewritten, so anything else in the file survives
    - which matters because this is a .env, and a .env is where keys live. The
    file is replaced by renaming a complete copy over it, so an interrupted
    write cannot leave a truncated file with the rest of the settings lost.
    """
    value = _clamp_volume(value)
    assignment = "%s=%.2f\n" % (VOLUME_KEY, value)

    with _settings_lock:
        try:
            with open(SETTINGS_FILE) as handle:
                lines = handle.readlines()
        except IOError:
            lines = []

        kept = []
        replaced = False
        for line in lines:
            name, separator, _ = line.partition("=")
            if separator and name.strip() == VOLUME_KEY:
                # Substitute the first, drop any duplicates after it.
                if not replaced:
                    kept.append(assignment)
                    replaced = True
                continue
            kept.append(line)
        if not replaced:
            if kept and not kept[-1].endswith("\n"):
                kept[-1] += "\n"
            kept.append(assignment)

        handle = tempfile.NamedTemporaryFile(
            "w", dir=os.path.dirname(SETTINGS_FILE) or ".", delete=False)
        try:
            handle.writelines(kept)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            # NamedTemporaryFile is 0600; keep whatever the file had, so a
            # settings file shared with something else stays readable to it.
            if lines:
                os.chmod(handle.name, os.stat(SETTINGS_FILE).st_mode & 0o7777)
            os.rename(handle.name, SETTINGS_FILE)
        except Exception:
            discard(handle.name)
            raise

    return value


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


def _synthesize_piper_once(text):
    """Spawn piper for a single utterance. The fallback path."""
    handle = tempfile.NamedTemporaryFile(suffix=".wav", dir=TMP_DIR, delete=False)
    handle.close()
    subprocess.run(
        [PIPER_BIN, "--model", PIPER_VOICE,
         "--length_scale", LENGTH_SCALE, "--output_file", handle.name],
        input=text.encode("utf-8"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return handle.name


def _synthesize_pico(text):
    """The 2013 AIY voice, used when Piper is not installed.

    The volume, pitch and speed markup matches what aiy.voice.tts.say applied,
    so falling back sounds like Gary always used to.
    """
    handle = tempfile.NamedTemporaryFile(suffix=".wav", dir=TMP_DIR, delete=False)
    handle.close()
    markup = ("<volume level='50'><pitch level='69'><speed level='125'>"
              "%s</speed></pitch></volume>" % text)
    subprocess.check_call(
        ["pico2wave", "--wave", handle.name, "--lang", "en-GB", markup],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return handle.name


def synthesize(text):
    """Render speech to a wav and return its path. Caller must discard() it.

    Kept separate from play() so the servo API can open Gary's mouth between
    the two. Synthesis takes about two seconds with Piper, and opening the jaw
    before that leaves him sitting open-mouthed and silent while the model
    works - which with Pico's 0.12s nobody ever noticed.
    """
    if ENGINE == "pico" or (ENGINE == "auto" and not piper_available()):
        return _synthesize_pico(text)
    try:
        return _piper.synthesize(text)
    except Exception as e:
        # A one-shot run is slower but self-contained, so a wedged resident
        # process degrades the voice rather than silencing Gary.
        print("resident piper failed (%s), falling back to one-shot" % e)
        return _synthesize_piper_once(text)


def play(path):
    """Play a wav, attenuated to the current volume. Blocks until it finishes.

    The gain is applied to the samples rather than to the mixer, so Gary's
    loudness is his own setting and does not change system audio for anything
    else on the Pi. Piper output is louder in practice than the old voice was:
    Pico was played through aiy.voice.tts at volume level 50, which halved it,
    and Piper has no equivalent.
    """
    with contextlib.closing(wave.open(path, "rb")) as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.readframes(source.getnframes())

    gain = volume()
    if gain != 1.0:
        frames = audioop.mul(frames, width, gain)

    # Piped rather than given a filename, so the attenuated audio never needs
    # a second temporary file.
    player = subprocess.Popen(
        ["aplay", "-q", "-D", ALSA_DEVICE, "-f", "S16_LE",
         "-r", str(rate), "-c", str(channels), "-"],
        stdin=subprocess.PIPE)
    player.communicate(frames)
    if player.returncode != 0:
        raise RuntimeError("aplay exited %d" % player.returncode)


def discard(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def speak(text):
    """Synthesize and play. Blocks until the audio has finished."""
    path = synthesize(text)
    try:
        play(path)
    finally:
        discard(path)
