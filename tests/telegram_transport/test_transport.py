import asyncio
import httpx
from pathlib import Path
import pytest
import tomllib

from apps.bot.sivka_burka_bot.telegram_transport import TelegramBotApiClient, TelegramTransportError


def client(handler):
    return TelegramBotApiClient("123456:synthetic_token", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_fixed_url_and_only_reviewed_payload_are_sent():
    requests = []
    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": True if request.url.path.endswith("answerCallbackQuery") else {"message_id": 1}})
    api = client(handler)
    asyncio.run(api.answer_callback_query("callback"))
    asyncio.run(api.send_message("42", "hello", {"inline_keyboard": []}))
    assert [item.url.host for item in requests] == ["api.telegram.org", "api.telegram.org"]
    assert [item.url.path.rsplit("/", 1)[-1] for item in requests] == ["answerCallbackQuery", "sendMessage"]
    asyncio.run(api.client.aclose())


@pytest.mark.parametrize("status, body, code", [
    (429, {"ok": False, "parameters": {"retry_after": 3}, "description": "private"}, "RATE_LIMITED"),
    (500, {"ok": False, "description": "private"}, "PROVIDER_UNAVAILABLE"),
    (200, {"ok": "yes", "description": "private"}, "PROVIDER_UNAVAILABLE"),
])
def test_provider_failures_are_safe(status, body, code):
    api = client(lambda request: httpx.Response(status, json=body))
    with pytest.raises(TelegramTransportError) as raised:
        asyncio.run(api.answer_callback_query("callback"))
    assert raised.value.code == code
    assert "123456:synthetic_token" not in str(raised.value)
    assert "private" not in repr(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    asyncio.run(api.client.aclose())


def test_runtime_dependency_and_safe_client_repr():
    project = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = project["project"]["dependencies"]
    development = project["project"]["optional-dependencies"]["dev"]
    assert "httpx==0.28.1" in runtime
    assert all(not item.startswith("httpx") for item in development)
    assert "apps/bot" in project["tool"]["setuptools"]["packages"]["find"]["where"]
    api = client(lambda request: httpx.Response(200, json={"ok": True, "result": True}))
    assert "123456:synthetic_token" not in repr(api)
    asyncio.run(api.client.aclose())


@pytest.mark.parametrize("method, body", [
    ("callback", {"ok": True}),
    ("callback", {"ok": True, "result": {}}),
    ("message", {"ok": True}),
    ("message", {"ok": True, "result": {"message_id": True}}),
])
def test_malformed_success_is_rejected(method, body):
    api = client(lambda request: httpx.Response(200, json=body))
    with pytest.raises(TelegramTransportError) as raised:
        if method == "callback":
            asyncio.run(api.answer_callback_query("callback"))
        else:
            asyncio.run(api.send_message("42", "hello", {"inline_keyboard": []}))
    assert raised.value.code == "MALFORMED_PROVIDER_RESPONSE"
    asyncio.run(api.client.aclose())


def test_network_failure_has_no_sensitive_exception_chain():
    def fail(request):
        raise httpx.ConnectError("private provider failure", request=request)
    api = client(fail)
    with pytest.raises(TelegramTransportError) as raised:
        asyncio.run(api.answer_callback_query("callback"))
    assert raised.value.code == "PROVIDER_UNAVAILABLE"
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert "synthetic_token" not in repr(raised.value)
    asyncio.run(api.client.aclose())
