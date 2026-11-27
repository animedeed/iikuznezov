"""Loopback-only camera page and the avatar's live animation state."""

from array import array
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import sqrt
from os import fstat
from pathlib import Path
import re
from shutil import copyfileobj
import sys
from threading import Lock, Thread
from time import monotonic

from . import SAMPLE_RATE

_WEB = Path(__file__).with_name("avatar_web")
_JAVASCRIPT = "text/javascript; charset=utf-8"
_GLTF = "model/gltf-binary"
# The script hash authorizes the import map in index.html.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'sha256-p9ruqrrvUoHk8B1W8x6/urtDxl5eLM2f53RCknrG03c='; "
    "img-src 'self' blob:; connect-src 'self' blob:; frame-ancestors 'none'"
)
_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/avatar.css": ("avatar.css", "text/css; charset=utf-8"),
    "/avatar.js": ("avatar.js", _JAVASCRIPT),
    "/rocketbox.js": ("rocketbox.js", _JAVASCRIPT),
    "/three/addons/loaders/GLTFLoader.js": (
        "node_modules/three/examples/jsm/loaders/GLTFLoader.js", _JAVASCRIPT,
    ),
    "/three/addons/utils/BufferGeometryUtils.js": (
        "node_modules/three/examples/jsm/utils/BufferGeometryUtils.js", _JAVASCRIPT,
    ),
    "/models/sophia_formal.glb": ("models/sophia_formal.glb", _GLTF),
    "/models/daniel_formal.glb": ("models/daniel_formal.glb", _GLTF),
    "/three.module.js": ("node_modules/three/build/three.module.js", _JAVASCRIPT),
    "/three.core.js": ("node_modules/three/build/three.core.js", _JAVASCRIPT),
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
                with (_WEB / filename).open("rb") as asset:
                    self._headers(fstat(asset.fileno()).st_size, mimetype)
                    copyfileobj(asset, self.wfile, 128 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except FileNotFoundError:
                self.send_error(404)

        def _headers(self, size: int, mimetype: str):
            self.send_response(200)
            self.send_header("Content-Type", mimetype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store" if mimetype == "application/json" else "no-cache")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

        def _send(self, body: bytes, mimetype: str):
            try:
                self._headers(len(body), mimetype)
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
