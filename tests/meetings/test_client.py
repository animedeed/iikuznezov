from io import StringIO
from json import loads
from unittest.mock import Mock, call

import httpx
import pytest

from app.meetings.__main__ import main
from app.meetings.audio import PulseAudio
from app.meetings.client import MeetingClient, MeetingClientError
from app.meetings.session import run_session
from app.meetings.speech import SpeechError, YandexSpeech


def test_voice_session_retries_same_request_and_finishes_after_tts_failure(monkeypatch, capsys):
    monkeypatch.setattr("app.meetings.client.sleep", Mock())
    received = []

    def serve(request):
        assert request.headers["authorization"] == "Bearer demo-token"
        route = request.url.path.removeprefix("/api/v1/meetings/demo-meeting/")
        body = loads(request.content)
        received.append((route, body))
        if route == "start":
            return httpx.Response(200, json={"reply_text": "Начнём?"})
        if route == "finish":
            return httpx.Response(202, json={"meeting_id": "demo-meeting", "status": "queued"})
        if len(received) == 2:
            raise httpx.ReadTimeout("response lost", request=request)
        if len(received) == 3:
            return httpx.Response(409, json={"detail": {"code": "request_in_progress"}})
        if len(received) == 4:
            return httpx.Response(503)
        return httpx.Response(200, json={
            "sequence": body["sequence"], "reply_text": "Зафиксировано.", "approval_required": True,
        })

    audio = Mock(spec=PulseAudio)
    audio.record.return_value = b"pcm"
    speech = Mock(spec=YandexSpeech)
    speech.recognize.side_effect = ["Нужен бюджет.", "Пока не согласован."]
    speech.synthesize.side_effect = [b"greeting", SpeechError("tts_timeout"), b"last"]
    with httpx.Client(transport=httpx.MockTransport(serve)) as http:
        meeting = MeetingClient(http, "https://example.test/api", "demo-meeting", "demo-token")
        run_session(audio, speech, None, StringIO("\n\n\n\nq\n"), 20, meeting=meeting)

    request = {
        "request_id": "voice-turn-1", "sequence": 1, "text": "Нужен бюджет.", "role": "participant",
    }
    assert received == [
        ("start", {"request_id": "voice-start"}),
        *[("turns", request)] * 4,
        ("turns", {"request_id": "voice-turn-2", "sequence": 2, "text": "Пока не согласован.",
                   "role": "participant"}),
        ("finish", {"request_id": "voice-finish"}),
    ]
    assert speech.recognize.call_count == 2
    assert audio.play.call_args_list == [call(b"greeting"), call(b"last")]
    captured = capsys.readouterr()
    assert "Расшифровка поставлена в очередь" in captured.out
    assert "требует согласования руководителя" in captured.out
    assert "tts_timeout" in captured.err


@pytest.mark.parametrize("status", [302, 401, 409, 422])
def test_terminal_service_errors_stop_without_resending_or_following_redirects(status):
    handler = Mock(return_value=httpx.Response(status, headers={"Location": "https://other.example.test/"}))
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        session = MeetingClient(http, "https://example.test", "demo-meeting", "demo-token")
        with pytest.raises(MeetingClientError, match=f"meeting_http_{status}") as error:
            session.start()
    assert not error.value.retryable
    assert handler.call_count == 1


@pytest.mark.parametrize("url", ["http://example.test", "https://name@example.test", "https://example.test/?"])
def test_meeting_url_validation(url):
    with pytest.raises(ValueError):
        MeetingClient(Mock(spec=httpx.Client), url, "demo-meeting", "demo-token")


def test_cli_connects_to_meeting_with_speech_key_only(monkeypatch):
    monkeypatch.setattr("sys.argv", [
        "app.meetings", "--meeting-url", "http://127.0.0.1:8000", "--meeting-id", "demo-meeting",
    ])
    monkeypatch.setattr("sys.stdin", Mock(isatty=lambda: True))
    monkeypatch.setenv("IIKUZNEZOV_MEETING_TOKEN", "demo-token")
    monkeypatch.setenv("YANDEX_SPEECH_API_KEY", "speech-key")
    monkeypatch.delenv("YANDEX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("YANDEX_FOLDER_ID", raising=False)
    monkeypatch.setattr(PulseAudio, "check_routes", Mock())
    run = Mock()
    monkeypatch.setattr("app.meetings.session.run_session", run)

    assert main() == 0
    assert run.call_args.args[2] is None
    assert isinstance(run.call_args.kwargs["meeting"], MeetingClient)
