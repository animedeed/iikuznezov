from array import array
from io import StringIO
from unittest.mock import Mock, call

import pytest

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
    avatar = AvatarState()
    samples = array("h", [0] * 533 + [6500] * 533).tobytes()
    avatar.set("speaking", "joy", samples)
    assert avatar.snapshot() == {"phase": "speaking", "emotion": "joy", "levels": (0.0, 1.0)}
    clock.return_value = 100.04
    assert avatar.snapshot()["levels"] == (1.0,)
    avatar.set("idle")
    assert avatar.snapshot() == {"phase": "idle", "emotion": "neutral", "levels": ()}


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
