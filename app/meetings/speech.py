"""Synchronous Yandex SpeechKit adapter for short, explicitly captured turns."""

import httpx

from . import MAX_RECORD_SECONDS, SAMPLE_BYTES, SAMPLE_RATE

_STT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
_TTS = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
_FORMAT = {"format": "lpcm", "sampleRateHertz": str(SAMPLE_RATE), "lang": "ru-RU"}


class SpeechError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class YandexSpeech:
    def __init__(self, client: httpx.Client, api_key: str):
        self._client = client
        self._headers = {"Authorization": f"Api-Key {api_key}"}

    def recognize(self, pcm: bytes) -> str:
        if not pcm:
            return ""
        if len(pcm) % SAMPLE_BYTES or len(pcm) > MAX_RECORD_SECONDS * SAMPLE_RATE * SAMPLE_BYTES:
            raise SpeechError("stt_invalid_pcm")
        response = self._post("stt", _STT, params=_FORMAT, content=pcm)
        try:
            result = response.json()["result"]
        except (ValueError, KeyError, TypeError):
            raise SpeechError("stt_invalid_response") from None
        if not isinstance(result, str):
            raise SpeechError("stt_invalid_response")
        return result.strip()

    def synthesize(self, text: str) -> bytes:
        response = self._post("tts", _TTS, data={**_FORMAT, "text": text, "voice": "marina"})
        pcm = response.content
        if not pcm or len(pcm) % SAMPLE_BYTES:
            raise SpeechError("tts_invalid_pcm")
        return pcm

    def _post(self, operation: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.post(url, headers=self._headers, **kwargs)
        except httpx.TimeoutException:
            raise SpeechError(f"{operation}_timeout") from None
        except httpx.HTTPError:
            raise SpeechError(f"{operation}_transport_error") from None
        if not response.is_success:
            raise SpeechError(f"{operation}_http_{response.status_code}")
        return response
