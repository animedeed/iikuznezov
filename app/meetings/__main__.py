"""Run a local, operator-controlled Telemost voice test."""

import argparse
from array import array
from contextlib import nullcontext
from math import pi, sin
from os import environ
import sys
from threading import Event

from . import MAX_RECORD_SECONDS, SAMPLE_RATE
from .audio import AudioError, DEFAULT_SINK, DEFAULT_SOURCE, PulseAudio, list_devices
from .avatar import CHARACTER_VOICES, DEFAULT_CHARACTER, AvatarState, CameraError, camera_server


def _test_tone() -> bytes:
    tone = array("h", (
        int(5_000 * sin(2 * pi * 440 * frame / SAMPLE_RATE))
        for frame in range(SAMPLE_RATE // 2)
    ))
    if sys.byteorder == "big":
        tone.byteswap()
    return tone.tobytes()


def main() -> int:
    parser = argparse.ArgumentParser(description="Голосовой тест в учебной комнате Яндекс Телемоста.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list-devices", action="store_true", help="Показать устройства PulseAudio.")
    modes.add_argument("--audio-check", action="store_true", help="Подать тестовый сигнал в микрофон Телемоста.")
    modes.add_argument("--avatar", action="store_true", help="Запустить голосовой клиент и локальную 3D-камеру.")
    modes.add_argument("--avatar-preview", action="store_true", help="Показать 3D-аватар с демонстрацией мимики без звонка.")
    parser.add_argument(
        "--character", choices=CHARACTER_VOICES, default=DEFAULT_CHARACTER,
        help="Выбрать женский (female) или мужской (male) образ и голос.",
    )
    parser.add_argument("--avatar-port", type=int, default=8765, help="Порт локальной 3D-камеры.")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="Источник звука собеседников.")
    parser.add_argument("--sink", default=DEFAULT_SINK, help="Виртуальный выход для речи ассистента.")
    parser.add_argument("--max-seconds", default=20, type=int, metavar="1–30", help="Лимит одной реплики.")
    args = parser.parse_args()
    if not 1 <= args.max_seconds <= MAX_RECORD_SECONDS:
        parser.error("--max-seconds должен быть от 1 до 30.")

    if not 1 <= args.avatar_port <= 65_535:
        parser.error("--avatar-port должен быть от 1 до 65535.")
    avatar = AvatarState(args.character) if args.avatar or args.avatar_preview else None
    audio = PulseAudio(args.source, args.sink)
    try:
        if args.list_devices:
            for kind in ("sources", "sinks"):
                print(kind)
                for index, name in list_devices(kind):
                    print(f"  {index}  {name}")
            return 0
        if args.audio_check:
            audio.check_output()
            audio.play(_test_tone())
            return 0

        if args.avatar_preview:
            with camera_server(avatar, args.avatar_port) as url:
                print(f"Откройте демонстрацию: {url}?demo=1&character={args.character}", flush=True)
                Event().wait()
            return 0

        if not sys.stdin.isatty():
            parser.error("Для диалога запустите команду в интерактивном терминале.")
        names = ("YANDEX_LLM_API_KEY", "YANDEX_FOLDER_ID", "YANDEX_SPEECH_API_KEY")
        missing = [name for name in names if not environ.get(name, "").strip()]
        if missing:
            parser.error("Задайте переменные окружения " + ", ".join(missing) + ".")

        import httpx

        from app.adapters.model import YandexAIStudioModelPort

        from .session import run_session
        from .speech import YandexSpeech

        audio.check_routes()
        model_name = environ.get("YANDEX_LLM_MODEL", "").strip() or "yandexgpt-5.1"
        camera = camera_server(avatar, args.avatar_port) if avatar else nullcontext()
        with httpx.Client(timeout=20) as client, camera as url:
            if url:
                print(f"3D-камера для OBS: {url}")
            model = YandexAIStudioModelPort(
                environ["YANDEX_LLM_API_KEY"].strip(), environ["YANDEX_FOLDER_ID"].strip(),
                model=model_name, client=client, max_reply_chars=350,
            )
            speech = YandexSpeech(
                client, environ["YANDEX_SPEECH_API_KEY"].strip(), voice=CHARACTER_VOICES[args.character],
            )
            print("Войдите в комнату через браузер и включите назначенный виртуальный микрофон.")
            run_session(audio, speech, model, sys.stdin, args.max_seconds, avatar=avatar)
    except (AudioError, CameraError) as error:
        print(error, file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nКлиент завершён.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
