#!/bin/bash
# Starts Gary's three services.
#
# Order matters: the servo API owns the serial link to the Arduino and also
# performs text-to-speech, so the brain depends on it being up. Paths resolve
# relative to this script, so the project can live anywhere.

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"

# Unbuffered, so print() output reaches log files instead of sitting in a buffer
export PYTHONUNBUFFERED=1

#source ~/piv4venv/bin/activate
python3 "$ROOT/servo_api/server.py" &
python3 "$ROOT/vision/server.py" &
python3 "$ROOT/ears/listener.py" &
cd "$ROOT/brain" || exit 1
flask run --port 5001 &
