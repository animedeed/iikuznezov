import httpx
import pytest

from app.contracts import DeliveryStatus
from app.telegram.bot import TelegramAPI, deliver_pages


@pytest.mark.parametrize(("pages", "responses", "expected_status"), [
    (["page"], [(429, None)], DeliveryStatus.RETRYABLE),
    (["page"], [(200, {"ok": True, "result": {}})], DeliveryStatus.UNKNOWN),
    (["page"], [(403, None)], DeliveryStatus.FAILED),
    (["first", "second"], [(200, {"ok": True, "result": {"message_id": 1}}), (429, None)],
     DeliveryStatus.UNKNOWN),
])
def test_delivery_classifies_provider_responses(pages, responses, expected_status):
    remaining = iter(responses)

    def respond(request):
        status, body = next(remaining)
        if body is None:
            return httpx.Response(status, text="Provider response without JSON")
        return httpx.Response(status, json=body)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = deliver_pages(TelegramAPI("test", client), 10001, pages)

    assert result.status == expected_status
    assert result.external_id is None
