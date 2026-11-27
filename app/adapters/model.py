"""Yandex AI Studio Completions API adapter (OpenAI-compatible HTTP endpoint)."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from time import monotonic
from typing import TypeVar
from uuid import uuid4

import httpx
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
_LOGGER = logging.getLogger(__name__)
_ENDPOINT = "https://ai.api.cloud.yandex.net/v1/chat/completions"


class ModelAdapterError(RuntimeError):
    """Safe adapter failure; carries only a stable code and optional HTTP status."""

    def __init__(self, code: str, *, http_status: int | None = None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


class YandexAIStudioModelPort:
    """Synchronous model port. Inject an httpx.Client to control transport and lifecycle."""

    def __init__(
        self,
        api_key: str,
        folder_id: str,
        *,
        model: str = "yandexgpt-5.1",
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = 1_000_000,
        max_reply_chars: int = 2_000,
    ):
        if not api_key or not folder_id or not model:
            raise ValueError("api_key, folder_id and model are required")
        if timeout <= 0 or max_response_bytes <= 0 or max_reply_chars <= 0:
            raise ValueError("limits must be positive")
        self._api_key = api_key
        self._folder_id = folder_id
        self._model = model
        self._client = client or httpx.Client()
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._max_reply_chars = max_reply_chars

    def generate_json(self, schema: type[T], messages: Sequence[dict[str, str]]) -> T:
        response = self._logged_complete(
            messages,
            max_tokens=1_000,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                },
            },
        )
        try:
            content = self._content(response)
            return schema.model_validate_json(content)
        except ModelAdapterError:
            raise
        except Exception:
            raise ModelAdapterError("invalid_model_json") from None

    def reply(self, messages: Sequence[dict[str, str]]) -> str:
        response = self._logged_complete(messages, max_tokens=300)
        try:
            content = self._content(response).strip()
        except ModelAdapterError:
            raise
        if not content:
            raise ModelAdapterError("empty_model_reply")
        if len(content) > self._max_reply_chars:
            content = content[: self._max_reply_chars - 1].rstrip() + "…"
        return content

    def _logged_complete(
        self, messages: Sequence[dict[str, str]], *, max_tokens: int,
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]:
        request_id = uuid4().hex
        started = monotonic()
        code = "ok"
        try:
            return self._complete(messages, max_tokens=max_tokens, response_format=response_format)
        except ModelAdapterError as error:
            code = error.code
            raise
        except ValueError:
            code = "invalid_request"
            raise
        finally:
            _LOGGER.info("model_request id=%s outcome=%s duration_ms=%.0f",
                         request_id, code, (monotonic() - started) * 1000)

    def _complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized = self._messages(messages)
        payload: dict[str, object] = {
            "model": f"gpt://{self._folder_id}/{self._model}",
            "messages": normalized,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        headers = {
            "Authorization": f"Api-Key {self._api_key}",
            "OpenAI-Project": self._folder_id,
            "Content-Type": "application/json",
        }
        try:
            with self._client.stream(
                "POST", _ENDPOINT, headers=headers, json=payload, timeout=self._timeout
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > self._max_response_bytes:
                        raise ModelAdapterError("response_too_large")
                    body.extend(chunk)
                status = response.status_code
        except ModelAdapterError:
            raise
        except httpx.TimeoutException:
            raise ModelAdapterError("timeout") from None
        except httpx.HTTPError:
            raise ModelAdapterError("transport_error") from None
        if status < 200 or status >= 300:
            raise ModelAdapterError("http_error", http_status=status)
        try:
            decoded = json.loads(body)
            if not isinstance(decoded, dict):
                raise ValueError
            return decoded
        except (ValueError, UnicodeDecodeError):
            raise ModelAdapterError("invalid_response_json") from None

    @staticmethod
    def _messages(messages: Sequence[dict[str, str]]) -> list[dict[str, str]]:
        if not messages:
            raise ValueError("messages must not be empty")
        normalized = []
        for message in messages:
            if set(message) != {"role", "content"} or message["role"] not in {"system", "user", "assistant"}:
                raise ValueError("messages must contain role and content")
            if not isinstance(message["content"], str):
                raise ValueError("message content must be text")
            normalized.append({"role": message["role"], "content": message["content"]})
        return normalized

    @staticmethod
    def _content(response: dict[str, object]) -> str:
        try:
            choices = response["choices"]
            first = choices[0]  # type: ignore[index]
            message = first["message"]  # type: ignore[index]
            content = message["content"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            raise ModelAdapterError("invalid_response_shape") from None
        if not isinstance(content, str):
            raise ModelAdapterError("invalid_response_shape")
        return content
