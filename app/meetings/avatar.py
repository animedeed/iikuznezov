"""Loopback-only camera page and the avatar's live animation state."""

from array import array
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import sqrt
from pathlib import Path
import re
import sys
from threading import Lock, Thread
from time import monotonic

from . import SAMPLE_RATE

_WEB = Path(__file__).with_name("avatar_web")
_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/avatar.css": ("avatar.css", "text/css; charset=utf-8"),
    "/avatar.js": ("avatar.js", "text/javascript; charset=utf-8"),
    "/three.module.js": ("node_modules/three/build/three.module.js", "text/javascript; charset=utf-8"),
    "/three.core.js": ("node_modules/three/build/three.core.js", "text/javascript; charset=utf-8"),
}
DEFAULT_CHARACTER = "female"
CHARACTER_VOICES = {"female": "marina", "male": "filipp"}
_FRAMES_PER_SECOND = 30
_EMOTIONS = {
    "concern": r"\b(?:проблема|проблемы|риск|риски|сорвали|задержка|сбой|отменили|не успеем)\b",
    "surprise": r"\b(?:внезапно|неожиданно|сюрприз|вот это да|ничего себе)\b",
    "joy": r"\b(?:отлично|ура|победа|поздравляю|получилось|хорошие новости|замечательно)\b",
}


class CameraError(RuntimeError):
    pass


def reaction(text: str) -> str:
    for emotion, pattern in _EMOTIONS.items():
        if re.search(pattern, text, re.IGNORECASE):
            return emotion
    return "neutral"


class AvatarState:
    def __init__(self, character: str = DEFAULT_CHARACTER):
        self._character = character
        self._lock = Lock()
        self._pose = ("idle", "neutral", (), monotonic())

    def set(self, phase: str, emotion: str = "neutral", pcm: bytes = b"") -> None:
        samples = array("h", pcm)
        if sys.byteorder == "big":
            samples.byteswap()
        frame_size = SAMPLE_RATE // _FRAMES_PER_SECOND
        levels = []
        for offset in range(0, len(samples), frame_size):
            frame = samples[offset:offset + frame_size]
            rms = sqrt(sum(sample * sample for sample in frame) / len(frame))
            levels.append(round(min(rms / 6500, 1), 3))
        with self._lock:
            self._pose = phase, emotion, tuple(levels), monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            phase, emotion, levels, started = self._pose
        start_frame = int((monotonic() - started) * (SAMPLE_RATE / (SAMPLE_RATE // _FRAMES_PER_SECOND)))
        return {
            "character": self._character,
            "phase": phase,
            "emotion": emotion,
            "levels": levels[start_frame:start_frame + 12],
        }


@contextmanager
def camera_server(avatar: AvatarState, port: int):
    if not (_WEB / _FILES["/three.module.js"][0]).is_file():
        raise CameraError("Установите Three.js командой npm ci --prefix app/meetings/avatar_web.")

    class CameraHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.partition("?")[0]
            if path == "/state":
                self._send(json.dumps(avatar.snapshot()).encode(), "application/json")
                return
            entry = _FILES.get(path)
            if entry is None:
                self.send_error(404)
                return
            filename, mimetype = entry
            try:
                body = (_WEB / filename).read_bytes()
            except FileNotFoundError:
                self.send_error(404)
                return
            self._send(body, mimetype)

        def _send(self, body: bytes, mimetype: str):
            self.send_response(200)
            self.send_header("Content-Type", mimetype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if mimetype == "application/json" else "no-cache")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), CameraHandler)
    except OSError:
        raise CameraError("Не удалось открыть локальную камеру. Попробуйте другой --avatar-port.") from None
    with server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            thread.join()
