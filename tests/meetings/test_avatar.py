from array import array
from contextlib import nullcontext
from io import StringIO
from unittest.mock import Mock, call

import pytest

from app.meetings.__main__ import main
from app.meetings.audio import PulseAudio
from app.meetings.avatar import AvatarState, reaction
from app.meetings.session import run_session
from app.meetings.speech import YandexSpeech
from app.ports import ModelPort


@pytest.mark.parametrize(("phrase", "emotion"), [
    ("Отлично, получилось!", "joy"),
    ("Неожиданно перенесли релиз", "surprise"),
    ("Есть риск, мы не успеем", "concern"),
    ("Встречаемся в десять", "neutral"),
])
def test_reaction_to_transcribed_phrase(phrase, emotion):
    assert reaction(phrase) == emotion


def test_camera_gets_only_recent_playback_levels(monkeypatch):
    clock = Mock(return_value=100.0)
    monkeypatch.setattr("app.meetings.avatar.monotonic", clock)
    avatar = AvatarState("male")
    samples = array("h", [0] * 533 + [6500] * 533).tobytes()
    avatar.set("speaking", "joy", samples)
    assert avatar.snapshot() == {
        "character": "male", "phase": "speaking", "emotion": "joy", "levels": (0.0, 1.0),
    }
    clock.return_value = 100.04
    assert avatar.snapshot()["levels"] == (1.0,)
    avatar.set("idle")
    assert avatar.snapshot() == {
        "character": "male", "phase": "idle", "emotion": "neutral", "levels": (),
    }


def test_voice_session_drives_avatar_including_actual_tts_audio():
    audio = Mock(spec=PulseAudio)
    speech = Mock(spec=YandexSpeech)
    model = Mock(spec=ModelPort)
    avatar = Mock(spec=AvatarState)
    audio.record.return_value = b"voice"
    speech.recognize.return_value = "Вот это да, какой сюрприз!"
    speech.synthesize.return_value = b"\x00\x00"
    model.reply.return_value = "Расскажите, что изменилось."

    run_session(audio, speech, model, StringIO("\n\nq\n"), 20, avatar=avatar)

    assert avatar.set.call_args_list == [
        call("speaking", "joy", b"\x00\x00"), call("idle"),
        call("listening"), call("thinking"), call("thinking", "surprise"),
        call("speaking", "surprise", b"\x00\x00"), call("idle"),
    ]
    assert audio.play.call_count == 2


@pytest.mark.parametrize(("character_args", "character", "voice"), [
    ([], "female", "marina"),
    (["--character", "male"], "male", "filipp"),
])
def test_cli_selects_matching_character_and_speechkit_voice(monkeypatch, character_args, character, voice):
    monkeypatch.setattr("sys.argv", ["app.meetings", "--avatar", *character_args])
    monkeypatch.setattr("sys.stdin", Mock(isatty=lambda: True))
    for name in ("YANDEX_LLM_API_KEY", "YANDEX_FOLDER_ID", "YANDEX_SPEECH_API_KEY"):
        monkeypatch.setenv(name, "test")
    monkeypatch.setattr(PulseAudio, "check_routes", Mock())
    monkeypatch.setattr("app.meetings.__main__.camera_server", Mock(return_value=nullcontext("http://127.0.0.1/")))
    speech = Mock()
    session = Mock()
    monkeypatch.setattr("app.meetings.speech.YandexSpeech", speech)
    monkeypatch.setattr("app.meetings.session.run_session", session)

    assert main() == 0

    assert speech.call_args.kwargs["voice"] == voice
    assert session.call_args.kwargs["avatar"].snapshot()["character"] == character
