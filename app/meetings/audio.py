"""PipeWire-Pulse and PulseAudio access for the operator's Linux session."""

import os
import subprocess
from selectors import EVENT_READ, DefaultSelector
from time import monotonic
from typing import TextIO

from . import SAMPLE_BYTES, SAMPLE_RATE

DEFAULT_SOURCE = "telemost_in.monitor"
DEFAULT_SINK = "telemost_out"
_PCM_FLAGS = ["--raw", "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1"]


class AudioError(RuntimeError):
    pass


def list_devices(kind: str) -> list[tuple[str, str]]:
    try:
        result = subprocess.run(
            ["pactl", "list", "short", kind],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise AudioError("Не удалось получить список устройств через pactl.") from None
    rows = (row.split("\t") for row in result.stdout.splitlines())
    return [(fields[0], fields[1]) for fields in rows if len(fields) >= 2]


class PulseAudio:
    def __init__(self, source: str, sink: str):
        self.source = source
        self.sink = sink

    def check_output(self) -> None:
        if self.sink not in dict(list_devices("sinks")).values():
            raise AudioError("Выход для ответа отсутствует в списке pactl.")

    def check_routes(self) -> None:
        if self.source == f"{self.sink}.monitor":
            raise AudioError("Звук встречи и ответ должны идти через разные аудиоканалы.")
        self.check_output()
        if self.source not in dict(list_devices("sources")).values():
            raise AudioError("Источник звука встречи отсутствует в списке pactl.")

    def record(self, terminal: TextIO, seconds: int) -> bytes:
        try:
            process = subprocess.Popen(
                ["parec", *_PCM_FLAGS, f"--device={self.source}", "--latency-msec=60"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise AudioError("Не удалось запустить parec.") from None
        with process:
            try:
                return self._read_turn(process, terminal, seconds)
            finally:
                if process.poll() is None:
                    process.kill()

    @staticmethod
    def _read_turn(process: subprocess.Popen, terminal: TextIO, seconds: int) -> bytes:
        audio = bytearray()
        max_bytes = seconds * SAMPLE_RATE * SAMPLE_BYTES
        deadline = monotonic() + 5
        with DefaultSelector() as selector:
            selector.register(process.stdout, EVENT_READ, "audio")
            selector.register(terminal, EVENT_READ, "terminal")
            while len(audio) < max_bytes:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                for key, _ in selector.select(remaining):
                    if key.data == "terminal":
                        if not terminal.readline():
                            raise EOFError
                        return bytes(audio[:len(audio) // SAMPLE_BYTES * SAMPLE_BYTES])
                    chunk = os.read(key.fd, min(4096, max_bytes - len(audio)))
                    if not chunk:
                        raise AudioError("Захват звука прерван. Проверьте источник в pactl.")
                    if not audio:
                        deadline = monotonic() + seconds
                        print("Говорите. Enter завершает реплику.", flush=True)
                    audio.extend(chunk)
        if not audio:
            raise AudioError("Источник звука не передал данные. Проверьте настройки браузера.")
        return bytes(audio[:len(audio) // SAMPLE_BYTES * SAMPLE_BYTES])

    def play(self, pcm: bytes) -> None:
        try:
            subprocess.run(
                ["pacat", "--playback", *_PCM_FLAGS, f"--device={self.sink}", "--latency-msec=60"],
                input=pcm, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=True, timeout=len(pcm) / (SAMPLE_BYTES * SAMPLE_RATE) + 5,
            )
        except (OSError, subprocess.SubprocessError):
            raise AudioError("Не удалось воспроизвести ответ через pacat.") from None
