"""Owns Gary's camera.

Only one process can hold /dev/video0, so exactly one object here owns it and
everything else goes through this module.

The camera is opened lazily on first use and released again once nothing has
asked for a frame in a while. That avoids three problems with holding it open
permanently:

  * V4L2 keeps filling a buffer queue whether anyone reads it or not, so a
    read after a long idle returns a frame from seconds ago,
  * a handle held open for weeks is exposed to the legacy bcm2835-v4l2 driver
    wedging, and a hang is not a crash, so systemd will not restart it,
  * the module's LED stays lit, which in a shared office reads as a camera
    that is always recording.

Callers that genuinely need the camera held open - head tracking, later - take
a lease instead, which suppresses the idle release for as long as it is held.
"""

import os
import threading
import time

import cv2


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class CameraError(Exception):
    """The camera could not be opened or read."""


class Camera:
    """Lazily-opened, idle-released, thread-safe camera."""

    def __init__(self):
        self.index = _env_int("GARY_CAMERA_INDEX", 0)
        self.width = _env_int("GARY_CAMERA_WIDTH", 1280)
        self.height = _env_int("GARY_CAMERA_HEIGHT", 720)
        self.jpeg_quality = _env_int("GARY_CAMERA_JPEG_QUALITY", 85)

        # How long with no frames requested before the camera is released.
        self.idle_timeout = _env_float("GARY_CAMERA_IDLE_TIMEOUT", 30.0)
        # Auto-exposure needs a moment after a cold open or the first frame
        # comes out dark. raspistill uses -t 2000 for the same reason.
        self.warmup = _env_float("GARY_CAMERA_WARMUP", 1.5)
        # A read this long after the previous one is served from a stale
        # buffer, so drain the queue first.
        self.stale_after = _env_float("GARY_CAMERA_STALE_AFTER", 0.5)
        self.flush_frames = _env_int("GARY_CAMERA_FLUSH_FRAMES", 4)

        self._lock = threading.RLock()
        self._cap = None
        self._last_read = 0.0
        self._opened_at = 0.0
        self._leases = 0
        self._frames_served = 0

        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap, name="camera-reaper",
                                        daemon=True)
        self._reaper.start()

    # -- lifecycle ---------------------------------------------------------

    def _open(self):
        """Open the device. Caller must hold the lock."""
        if self._cap is not None:
            return

        cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            cap.release()
            raise CameraError("could not open camera index %d" % self.index)

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

        # Read and discard while auto-exposure settles.
        deadline = time.monotonic() + self.warmup
        while time.monotonic() < deadline:
            cap.grab()

        self._cap = cap
        self._opened_at = time.monotonic()

    def _close(self):
        """Release the device. Caller must hold the lock."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None
            self._opened_at = 0.0

    def _reap(self):
        """Release the camera once it has been idle and nothing holds a lease."""
        while not self._stop.wait(1.0):
            with self._lock:
                if self._cap is None or self._leases > 0:
                    continue
                if time.monotonic() - self._last_read >= self.idle_timeout:
                    self._close()

    def shutdown(self):
        self._stop.set()
        with self._lock:
            self._close()

    # -- leases ------------------------------------------------------------

    def acquire_lease(self):
        """Hold the camera open until the matching release. For tracking."""
        with self._lock:
            self._open()
            self._leases += 1
            self._last_read = time.monotonic()
            return self._leases

    def release_lease(self):
        with self._lock:
            if self._leases > 0:
                self._leases -= 1
            return self._leases

    # -- capture -----------------------------------------------------------

    def _read_frame(self):
        """Read one fresh frame. Caller must hold the lock."""
        self._open()

        # If the last read was a while ago the queued buffers are stale, so
        # throw some away rather than describing a scene from a minute ago.
        if time.monotonic() - self._last_read > self.stale_after:
            for _ in range(self.flush_frames):
                self._cap.grab()

        ok, frame = self._cap.read()
        self._last_read = time.monotonic()
        if not ok or frame is None:
            # A failed read often means the driver has wedged. Drop the handle
            # so the next request starts from a clean open.
            self._close()
            raise CameraError("camera read failed")
        return frame

    def _encode(self, frame):
        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
        if not ok:
            raise CameraError("jpeg encoding failed")
        return buf.tobytes()

    def capture(self):
        """Return one frame as JPEG bytes."""
        with self._lock:
            jpeg = self._encode(self._read_frame())
            self._frames_served += 1
            return jpeg

    def capture_many(self, count, interval):
        """Return `count` JPEG frames, `interval` seconds apart.

        Held under one lock so the burst is not interleaved with other
        requests, and so the camera stays open across the whole sequence.
        """
        frames = []
        with self._lock:
            for i in range(count):
                if i:
                    time.sleep(interval)
                frames.append(self._encode(self._read_frame()))
            self._frames_served += count
        return frames

    # -- introspection -----------------------------------------------------

    def status(self):
        with self._lock:
            is_open = self._cap is not None
            idle = time.monotonic() - self._last_read if self._last_read else None
            return {
                "open": is_open,
                "leases": self._leases,
                "idle_seconds": round(idle, 1) if idle is not None else None,
                "open_seconds": round(time.monotonic() - self._opened_at, 1) if is_open else None,
                "frames_served": self._frames_served,
                "settings": {
                    "index": self.index,
                    "resolution": "%dx%d" % (self.width, self.height),
                    "idle_timeout": self.idle_timeout,
                    "warmup": self.warmup,
                },
            }

    def release_now(self):
        """Drop the camera immediately, if no lease is holding it.

        Useful to free the device for raspistill while aiming it.
        """
        with self._lock:
            if self._leases > 0:
                return False
            self._close()
            return True
