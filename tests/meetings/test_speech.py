from urllib.parse import parse_qs

import httpx
import pytest

from app.meetings.speech import SAMPLE_RATE, SpeechError, YandexSpeech


def test_recognition_sends_russian_pcm_and_returns_clean_text():
    pcm = b"\x00\x00\x01\x00"

    def recognize(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.scheme == "https"
        assert request.url.host == "stt.api.cloud.yandex.net"
        assert request.url.path == "/speech/v1/stt:recognize"
        assert request.headers["Authorization"] == "Api-Key test-speech-key"
        assert request.url.params["lang"] == "ru-RU"
        assert request.url.params["format"] == "lpcm"
        assert request.url.params["sampleRateHertz"] == str(SAMPLE_RATE) == "16000"
        assert "folderId" not in request.url.params
        assert request.content == pcm
        return httpx.Response(200, json={"result": "  Привет, коллеги.  "})

    with httpx.Client(transport=httpx.MockTransport(recognize)) as client:
        assert YandexSpeech(client, "test-speech-key").recognize(pcm) == "Привет, коллеги."


@pytest.mark.parametrize(("voice_args", "voice"), [({}, "marina"), ({"voice": "filipp"}, "filipp")])
def test_synthesis_posts_russian_voice_and_returns_raw_pcm(voice_args, voice):
    pcm = b"\x01\x00\x02\x00"
    reply = "Что будем делать в четверг?"

    def synthesize(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.scheme == "https"
        assert request.url.host == "tts.api.cloud.yandex.net"
        assert request.url.path == "/speech/v1/tts:synthesize"
        assert request.headers["Authorization"] == "Api-Key test-speech-key"
        assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        fields = parse_qs(request.content.decode("ascii"))
        assert fields["text"] == [reply]
        assert fields["voice"] == [voice]
        assert fields["format"] == ["lpcm"]
        assert fields["sampleRateHertz"] == [str(SAMPLE_RATE)]
        assert "folderId" not in fields
        return httpx.Response(200, content=pcm)

    with httpx.Client(transport=httpx.MockTransport(synthesize)) as client:
        assert YandexSpeech(client, "test-speech-key", **voice_args).synthesize(reply) == pcm


def test_recognition_returns_empty_text_when_provider_detects_no_speech():
    def recognize(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": ""})

    with httpx.Client(transport=httpx.MockTransport(recognize)) as client:
        assert YandexSpeech(client, "test-speech-key").recognize(b"\x00\x00") == ""


def test_recognition_http_error_does_not_expose_key_or_response_body():
    api_key = "secret-speech-key"
    provider_body = "private provider body"

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=provider_body)

    with httpx.Client(transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(SpeechError) as error:
            YandexSpeech(client, api_key).recognize(b"\x00\x00")

    assert error.value.code
    assert api_key not in str(error.value)
    assert provider_body not in str(error.value)


def test_synthesis_timeout_does_not_expose_transport_error_or_reply():
    reply = "Приватная реплика встречи"
    provider_detail = "sensitive transport failure"

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(provider_detail, request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(SpeechError) as error:
            YandexSpeech(client, "test-speech-key").synthesize(reply)

    assert error.value.code
    assert reply not in str(error.value)
    assert provider_detail not in str(error.value)
