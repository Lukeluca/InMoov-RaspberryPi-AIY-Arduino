#!/bin/bash
# Installs Piper, a small neural text-to-speech model, so Gary stops sounding
# like a 2005 satnav. Everything lands in ~/gary-tts and nothing system-wide is
# touched; delete that directory to undo it completely.
#
#   ./scripts/install-piper.sh
#
# Then restart the servo API. servo_api/tts.py finds it automatically and falls
# back to the old Pico voice if this was never run.
#
#
# The awkward part, if you are wondering why this is not four lines
# ------------------------------------------------------------------
# Current Piper needs Python 3.9+ and publishes only 64-bit ARM wheels. This Pi
# runs 32-bit Raspberry Pi OS with Python 3.7, so the pip package is out.
#
# The older rhasspy releases ship a standalone binary for armv7l, which needs
# no Python at all. But every one of them - checked back to v1.0.0 - is linked
# against GLIBC 2.29/2.30 and GLIBCXX 3.4.26, while buster provides 2.28 and
# 3.4.25. One version short on both.
#
# So this fetches a bullseye glibc into a private directory and runs Piper
# under that loader. The rest of the system carries on with its own glibc; only
# Piper sees the newer one. It is a hack, and the honest alternative is
# upgrading the Pi to a 64-bit OS - which would very likely break the AIY Voice
# Kit stack that provides Gary's button, LED and audio, so it is not a small
# decision.

set -e

TTS_HOME="${GARY_TTS_HOME:-$HOME/gary-tts}"
PIPER_RELEASE=2023.11.14-2
VOICE="${1:-en_GB-alan-low}"
VOICE_PATH="en/en_GB/alan/low/en_GB-alan-low"     # matches the default voice
GLIBC_DEB=libc6_2.31-13+deb11u11_armhf.deb
STDCXX_DEB=libstdc++6_10.2.1-6_armhf.deb

echo "Installing Piper into $TTS_HOME"
mkdir -p "$TTS_HOME/voices"
cd "$TTS_HOME"

if [ ! -x piper/piper ]; then
    echo "  fetching the piper binary ($PIPER_RELEASE, armv7l)"
    curl -sL -o piper.tar.gz \
        "https://github.com/rhasspy/piper/releases/download/$PIPER_RELEASE/piper_linux_armv7l.tar.gz"
    tar xzf piper.tar.gz && rm piper.tar.gz
else
    echo "  piper binary already present"
fi

if [ ! -f "voices/$VOICE.onnx" ]; then
    echo "  fetching voice $VOICE"
    base=https://huggingface.co/rhasspy/piper-voices/resolve/main
    curl -sL -o "voices/$VOICE.onnx"      "$base/$VOICE_PATH.onnx"
    curl -sL -o "voices/$VOICE.onnx.json" "$base/$VOICE_PATH.onnx.json"
else
    echo "  voice $VOICE already present"
fi

# Only bother with the shim if the system glibc is too old to run piper.
NEED_SHIM=no
if ! ldd --version | head -1 | grep -qE '2\.(29|3[0-9]|[4-9][0-9])'; then
    NEED_SHIM=yes
fi

if [ "$NEED_SHIM" = yes ] && [ ! -f newlibs/lib/arm-linux-gnueabihf/ld-linux-armhf.so.3 ]; then
    echo "  system glibc is older than piper needs; fetching a private one"
    mkdir -p newlibs
    curl -sL -o "newlibs/$GLIBC_DEB"  "http://deb.debian.org/debian/pool/main/g/glibc/$GLIBC_DEB"
    curl -sL -o "newlibs/$STDCXX_DEB" "http://deb.debian.org/debian/pool/main/g/gcc-10/$STDCXX_DEB"
    (cd newlibs && for d in *.deb; do dpkg-deb -x "$d" . ; done && rm -f *.deb)
fi

# A wrapper so nothing else has to know about any of the above. tts.py just
# runs this as if it were piper.
cat > piper.sh <<WRAPPER
#!/bin/bash
# Written by install-piper.sh. Runs piper, under a private glibc if the system
# one is too old. Takes the same arguments as piper itself.
HERE="\$(cd "\$(dirname "\$0")" && pwd)"
if [ -f "\$HERE/newlibs/lib/arm-linux-gnueabihf/ld-linux-armhf.so.3" ]; then
    exec "\$HERE/newlibs/lib/arm-linux-gnueabihf/ld-linux-armhf.so.3" \\
        --library-path "\$HERE/newlibs/lib/arm-linux-gnueabihf:\$HERE/newlibs/usr/lib/arm-linux-gnueabihf:\$HERE/piper" \\
        "\$HERE/piper/piper" --espeak_data "\$HERE/piper/espeak-ng-data" "\$@"
else
    exec "\$HERE/piper/piper" --espeak_data "\$HERE/piper/espeak-ng-data" "\$@"
fi
WRAPPER
chmod +x piper.sh

echo "  checking it actually speaks"
echo "Piper is installed." | ./piper.sh --model "voices/$VOICE.onnx" \
    --length_scale 0.75 --output_file /tmp/piper_check.wav 2>/dev/null

if [ -s /tmp/piper_check.wav ]; then
    echo
    echo "Done. Restart the servo API and Gary will use $VOICE."
    echo "Test without restarting anything:  aplay /tmp/piper_check.wav"
else
    echo "FAILED: piper produced no audio" >&2
    exit 1
fi
