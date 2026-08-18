"""What tells Gary to start listening.

The listener owns the microphone; a trigger only decides *when* it should be
recording. Keeping that behind an interface is what makes this portable: the
AIY Voice HAT's button is one implementation, and a robot without that
discontinued board can use a keyboard, a GPIO pin, or an HTTP call instead.

A trigger sets `started` when listening should begin and `ended` when it should
stop. Several triggers share the same pair of events, so a physical button and
a remote call can both be live at once - which is how the brain can ask Gary to
listen without the button being in its path.

Select them with GARY_TRIGGER, comma separated:

    GARY_TRIGGER=aiy          the Voice HAT button (default)
    GARY_TRIGGER=aiy,http     button, plus POST /listen/start and /listen/stop
    GARY_TRIGGER=keyboard     press Enter to start, Enter again to stop
"""

import os
import sys
import threading


class Trigger(object):
    """Base class. Subclasses set `started` and `ended`."""

    name = "trigger"

    def __init__(self, started, ended):
        self.started = started
        self.ended = ended

    def start(self):
        """Begin watching. Must not block."""

    def indicate(self, listening):
        """Optional feedback that recording is or is not in progress."""

    def close(self):
        """Release any hardware."""


class AiyButtonTrigger(Trigger):
    """The arcade button on a Google AIY Voice HAT. Hold to talk."""

    name = "aiy"

    def __init__(self, started, ended):
        Trigger.__init__(self, started, ended)
        from aiy.board import Board, Led
        self._led_cls = Led
        self._board = Board()
        self._closed = False

    def start(self):
        thread = threading.Thread(target=self._watch, name="aiy-button")
        thread.daemon = True
        thread.start()

    def _watch(self):
        while not self._closed:
            # A timeout so close() is noticed rather than blocking forever.
            if not self._board.button.wait_for_press(timeout=0.5):
                continue
            self.ended.clear()
            self.started.set()
            self._board.button.wait_for_release()
            self.ended.set()

    def indicate(self, listening):
        self._board.led.state = self._led_cls.ON if listening else self._led_cls.OFF

    def close(self):
        self._closed = True
        try:
            self._board.close()
        except Exception:
            pass


class KeyboardTrigger(Trigger):
    """Enter to start, Enter again to stop.

    For development, and for robots with no button. Needs a terminal, so it is
    not useful under systemd.
    """

    name = "keyboard"

    def __init__(self, started, ended):
        Trigger.__init__(self, started, ended)
        self._closed = False

    def start(self):
        thread = threading.Thread(target=self._watch, name="keyboard")
        thread.daemon = True
        thread.start()

    def _watch(self):
        while not self._closed:
            print("Press Enter to start recording.")
            if sys.stdin.readline() == "":
                return          # stdin closed
            self.ended.clear()
            self.started.set()
            print("Recording. Press Enter again to stop.")
            if sys.stdin.readline() == "":
                return
            self.ended.set()


class HttpTrigger(Trigger):
    """POST /listen/start and /listen/stop.

    Lets the brain, or anything else, ask Gary to listen without owning the
    button. Runs its own small server so a robot that does not want a remote
    trigger simply does not enable it, and no port is opened.
    """

    name = "http"

    def __init__(self, started, ended):
        Trigger.__init__(self, started, ended)
        self.port = int(os.environ.get("GARY_EARS_PORT", 5003))

    def start(self):
        from flask import Flask, Response

        app = Flask("gary-ears")

        @app.route("/listen/start", methods=["POST"])
        def listen_start():
            self.ended.clear()
            self.started.set()
            return Response('{"error_message": "success"}',
                            mimetype="application/json")

        @app.route("/listen/stop", methods=["POST"])
        def listen_stop():
            self.ended.set()
            return Response('{"error_message": "success"}',
                            mimetype="application/json")

        @app.route("/health", methods=["GET"])
        def health():
            return Response('{"error_message": "success"}',
                            mimetype="application/json")

        def serve():
            app.run(host="0.0.0.0", port=self.port, debug=False,
                    use_reloader=False, threaded=True)

        thread = threading.Thread(target=serve, name="ears-http")
        thread.daemon = True
        thread.start()


AVAILABLE = {
    AiyButtonTrigger.name: AiyButtonTrigger,
    KeyboardTrigger.name: KeyboardTrigger,
    HttpTrigger.name: HttpTrigger,
}


def build_triggers(started, ended, spec=None):
    """Construct the triggers named in GARY_TRIGGER.

    A trigger that cannot be constructed - no AIY board present, say - is
    skipped with a warning rather than taking the service down, so a partly
    equipped robot still works.
    """
    spec = spec or os.environ.get("GARY_TRIGGER", "aiy")
    triggers = []
    for name in [n.strip() for n in spec.split(",") if n.strip()]:
        factory = AVAILABLE.get(name)
        if factory is None:
            print("unknown trigger %r, expected one of %s"
                  % (name, ", ".join(sorted(AVAILABLE))))
            continue
        try:
            triggers.append(factory(started, ended))
        except Exception as e:
            print("trigger %r unavailable: %s" % (name, e))
    return triggers
