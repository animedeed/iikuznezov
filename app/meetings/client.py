"""Client for the meeting service that owns the final transcript."""

from time import sleep
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.contracts import Contract, MeetingReply, MeetingTurn, Ref, ShortText


class MeetingClientError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False):
        self.retryable = retryable
        super().__init__(code)


class _StartReply(Contract):
    reply_text: ShortText


class _FinishReply(Contract):
    status: Literal["queued"]
    meeting_id: Ref


class MeetingClient:
    def __init__(self, http: httpx.Client, url: str, meeting_id: str, token: str):
        address = httpx.URL(url)
        loopback = address.host in {"localhost", "127.0.0.1", "::1"}
        if (
            not address.host or address.userinfo or "?" in url or "#" in url
            or not (address.scheme == "https" or (address.scheme == "http" and loopback))
        ):
            raise ValueError("Адрес сервиса встречи должен использовать HTTPS или локальный HTTP.")
        if (
            not 1 <= len(meeting_id) <= 128 or meeting_id != meeting_id.strip()
            or meeting_id in {".", ".."} or "/" in meeting_id
        ):
            raise ValueError("Укажите допустимый ID встречи.")
        if not token or not token.isascii() or any(not ch.isprintable() or ch.isspace() for ch in token):
            raise ValueError("Укажите действующий токен встречи.")
        self._http = http
        self._url = f"{str(address).rstrip('/')}/v1/meetings/{quote(meeting_id, safe='')}"
        self._meeting_id = meeting_id
        self._headers = {"Authorization": f"Bearer {token}"}
        self._next_sequence = 1

    def start(self) -> str:
        return self._post("start", {"request_id": "voice-start"}, 200, _StartReply).reply_text

    def turn(self, text: str) -> MeetingReply:
        sequence = self._next_sequence
        body = MeetingTurn(request_id=f"voice-turn-{sequence}", sequence=sequence, text=text)
        reply = self._post("turns", body.model_dump(mode="json"), 200, MeetingReply)
        if reply.sequence != sequence:
            raise MeetingClientError("meeting_invalid_response", retryable=True)
        self._next_sequence += 1
        return reply

    def finish(self) -> None:
        reply = self._post("finish", {"request_id": "voice-finish"}, 202, _FinishReply)
        if reply.meeting_id != self._meeting_id:
            raise MeetingClientError("meeting_invalid_response", retryable=True)

    def _post[T: Contract](self, route: str, body: dict, status: int, schema: type[T]) -> T:
        for delay in (0, 0.5, 1):
            if delay:
                sleep(delay)
            try:
                response = self._http.post(
                    f"{self._url}/{route}", headers=self._headers, json=body, follow_redirects=False,
                )
            except httpx.TransportError:
                code = "meeting_transport_error"
                continue
            if response.status_code == status:
                try:
                    return schema.model_validate_json(response.content)
                except ValidationError:
                    raise MeetingClientError("meeting_invalid_response", retryable=True) from None
            code = f"meeting_http_{response.status_code}"
            if response.status_code in {408, 429, 500, 502, 503, 504} or _in_progress(response):
                continue
            raise MeetingClientError(code)
        raise MeetingClientError(code, retryable=True)


def _in_progress(response: httpx.Response) -> bool:
    if response.status_code != 409:
        return False
    try:
        error = response.json()
    except ValueError:
        return False
    if isinstance(error, dict):
        error = error.get("detail", error)
    return isinstance(error, dict) and "request_in_progress" in (error.get("code"), error.get("message"))
