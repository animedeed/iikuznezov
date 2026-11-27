import httpx
import pytest
from pydantic import BaseModel

from app.adapters.model import ModelAdapterError, YandexAIStudioModelPort


class Answer(BaseModel):
    answer: str


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_structured_request_uses_yandex_auth_model_and_validates_schema():
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ok"}'}}]})

    port = YandexAIStudioModelPort("secret-key", "folder-123", client=client_for(handler))
    result = port.generate_json(Answer, [{"role": "user", "content": "question"}])
    request = seen["request"]
    import json
    payload = json.loads(request.content)
    assert result == Answer(answer="ok")
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == "Api-Key secret-key"
    assert request.headers["OpenAI-Project"] == "folder-123"
    assert payload["model"] == "gpt://folder-123/yandexgpt-5.1"
    assert payload["response_format"]["type"] == "json_schema"


def test_invalid_structured_content_is_rejected_locally():
    port = YandexAIStudioModelPort(
        "k", "f", client=client_for(lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": '{"wrong":3}'}}]}
        )),
    )
    with pytest.raises(ModelAdapterError) as error:
        port.generate_json(Answer, [{"role": "user", "content": "x"}])
    assert error.value.code == "invalid_model_json"


def test_error_exposes_no_response_body_or_key():
    secret = "never-print-this"
    port = YandexAIStudioModelPort(
        secret, "f", client=client_for(lambda request: httpx.Response(401, text=secret))
    )
    with pytest.raises(ModelAdapterError) as error:
        port.reply([{ "role": "user", "content": "x" }])
    assert error.value.http_status == 401
    assert secret not in str(error.value)


def test_response_limit_and_short_reply_are_enforced():
    long_text = "word " * 100
    port = YandexAIStudioModelPort(
        "k", "f", client=client_for(lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": long_text}}]}
        )), max_reply_chars=20,
    )
    assert len(port.reply([{ "role": "user", "content": "x" }])) <= 20

    limited = YandexAIStudioModelPort(
        "k", "f", client=client_for(lambda request: httpx.Response(200, content=b"x" * 100)),
        max_response_bytes=10,
    )
    with pytest.raises(ModelAdapterError) as error:
        limited.reply([{ "role": "user", "content": "x" }])
    assert error.value.code == "response_too_large"


def test_timeout_is_a_safe_typed_error():
    def timeout(request):
        raise httpx.ReadTimeout("key and request must not escape")

    port = YandexAIStudioModelPort("k", "f", client=client_for(timeout))
    with pytest.raises(ModelAdapterError) as error:
        port.reply([{ "role": "user", "content": "private prompt" }])
    assert error.value.code == "timeout"
    assert "private" not in str(error.value)
