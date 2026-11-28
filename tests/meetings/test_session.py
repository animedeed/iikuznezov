from io import StringIO
from unittest.mock import Mock, call

import pytest

from app.adapters.model import ModelAdapterError
from app.meetings.audio import AudioError, PulseAudio
from app.meetings.session import run_session
from app.meetings.speech import SpeechError, YandexSpeech
from app.ports import ModelPort


def test_guide_through_two_turns_without_recording_during_playback(capsys):
    events = Mock()
    audio = Mock(spec=PulseAudio)
    speech = Mock(spec=YandexSpeech)
    model = Mock(spec=ModelPort)
    events.attach_mock(audio, "audio")
    events.attach_mock(speech, "speech")
    events.attach_mock(model, "model")
    audio.record.side_effect = [b"first", b"second"]
    speech.recognize.side_effect = ["Встречаемся во вторник.", "В какой день встречаемся?"]
    model.reply.side_effect = ["Хорошо, во вторник.", "Во вторник."]
    speech.synthesize.return_value = b"pcm"
    terminal = StringIO("\n\n\nq\n")

    run_session(audio, speech, model, terminal, 20)

    assert [entry[0] for entry in events.mock_calls] == [
        "speech.synthesize", "audio.play",
        "audio.record", "speech.recognize", "model.reply", "speech.synthesize", "audio.play",
        "audio.record", "speech.recognize", "model.reply", "speech.synthesize", "audio.play",
    ]
    assert model.reply.call_args.args[0][1:] == [
        {"role": "user", "content": "Встречаемся во вторник."},
        {"role": "assistant", "content": "Хорошо, во вторник."},
        {"role": "user", "content": "В какой день встречаемся?"},
    ]
    assert capsys.readouterr().out.count("От конца захвата") == 2


def test_silence_and_provider_failures_leave_session_ready_for_next_turn(capsys):
    audio = Mock(spec=PulseAudio)
    speech = Mock(spec=YandexSpeech)
    model = Mock(spec=ModelPort)
    audio.record.return_value = b"pcm"
    speech.recognize.side_effect = ["", SpeechError("stt_timeout"), "Один", "Два"]
    speech.synthesize.side_effect = [b"hello", b"repeat", SpeechError("tts_timeout"), b"reply"]
    model.reply.side_effect = [ModelAdapterError("timeout"), "Есть ответ."]
    terminal = StringIO("\n\n\n\n\nq\n")

    run_session(audio, speech, model, terminal, 20)

    assert model.reply.call_count == 2
    assert audio.play.call_args_list == [call(b"hello"), call(b"repeat"), call(b"reply")]
    assert model.reply.call_args.args[0][1]["content"] == "Один"
    captured = capsys.readouterr()
    assert "stt_timeout" in captured.err and "tts_timeout" in captured.err
    assert captured.out.count("От конца захвата") == 2


def test_capture_from_own_playback_monitor_is_rejected():
    with pytest.raises(AudioError, match="разные аудиоканалы"):
        PulseAudio("output.monitor", "output").check_routes()
