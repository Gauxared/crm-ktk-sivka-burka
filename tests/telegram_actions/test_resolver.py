from types import MappingProxyType

import pytest

from apps.api.sivka_burka_api.channel_gateway import ChannelContext, Platform
from apps.bot.sivka_burka_bot.telegram_actions import (
    TelegramActionError,
    TelegramActionResolver,
)
from apps.bot.sivka_burka_bot.telegram_interactions import TelegramIntent, TelegramIntentKind


CTX = ChannelContext(Platform.TELEGRAM, "i", "s", "c", "e")
CATALOG = {"version": 7, "services": [{"options": [{"id": "first"}, {"id": "off", "active": False}]}, {"options": [{"id": "second", "active": True}]}]}
RESOLVER = TelegramActionResolver()


def intent(kind, payload=None, context=CTX):
    return TelegramIntent(context, kind, MappingProxyType(payload or {}))


def error_code(call):
    with pytest.raises(TelegramActionError) as caught:
        call()
    return caught.value.code, str(caught.value), repr(caught.value)


@pytest.mark.parametrize(("kind", "expected"), [(TelegramIntentKind.OPEN, "OPEN"), (TelegramIntentKind.HELP, "HELP"), (TelegramIntentKind.RESET, "RESET"), (TelegramIntentKind.SUBMIT, "SUBMIT")])
def test_direct_actions_are_exact(kind, expected):
    action = RESOLVER.resolve(intent(kind), CATALOG)
    assert action.kind == expected and dict(action.payload) == {}


def test_text_is_deferred_and_error_is_safe():
    code, text, representation = error_code(lambda: RESOLVER.resolve(intent(TelegramIntentKind.TEXT_INPUT, {"text": "secret"}), CATALOG))
    assert code == "SCREEN_REQUIRED" and "secret" not in text and "secret" not in representation


def test_order_includes_only_active_options_and_exact_output():
    action = RESOLVER.resolve(intent(TelegramIntentKind.SELECT_SERVICE, {"catalog_version": 7, "option_ordinal": 1}), CATALOG)
    assert action.kind == "SELECT_SERVICE"
    assert dict(action.payload) == {"service_option_id": "second"}
    assert set(action.payload) == {"service_option_id"}


@pytest.mark.parametrize("ordinal", [0, 1])
def test_ordinal_boundaries(ordinal):
    assert RESOLVER.resolve(intent(TelegramIntentKind.SELECT_SERVICE, {"catalog_version": 7, "option_ordinal": ordinal}), CATALOG).payload["service_option_id"] in {"first", "second"}


@pytest.mark.parametrize("ordinal", [2, 2_147_483_647])
def test_unknown_ordinal(ordinal):
    assert error_code(lambda: RESOLVER.resolve(intent(TelegramIntentKind.SELECT_SERVICE, {"catalog_version": 7, "option_ordinal": ordinal}), CATALOG))[0] == "UNKNOWN_SERVICE_SELECTION"


@pytest.mark.parametrize("payload", [{}, {"catalog_version": 7}, {"catalog_version": 7, "option_ordinal": 0, "x": 1}, {"catalog_version": True, "option_ordinal": 0}, {"catalog_version": 7, "option_ordinal": False}, {"catalog_version": 0, "option_ordinal": 0}, {"catalog_version": 7, "option_ordinal": 0.0}])
def test_select_payload_is_strict(payload):
    assert error_code(lambda: RESOLVER.resolve(intent(TelegramIntentKind.SELECT_SERVICE, payload), CATALOG))[0] == "INVALID_INTENT"


@pytest.mark.parametrize("catalog", [None, {}, {"version": True, "services": []}, {"version": 0, "services": []}, {"version": 1, "services": {}}, {"version": 1, "services": [{"options": {}}]}, {"version": 1, "services": [{"options": [None]}]}, {"version": 1, "services": [{"options": [{"id": "x", "active": None}]}]}, {"version": 1, "services": [{"options": [{"id": " x "}]}]}, {"version": 1, "services": [{"options": [{"id": "x"}, {"id": "x"}]}]}])
def test_catalog_failures_are_safe(catalog):
    assert error_code(lambda: RESOLVER.resolve(intent(TelegramIntentKind.SELECT_SERVICE, {"catalog_version": 1, "option_ordinal": 0}), catalog))[0] == "CATALOG_UNAVAILABLE"


def test_stale_catalog_and_version_boundaries():
    assert error_code(lambda: RESOLVER.resolve(intent(TelegramIntentKind.SELECT_SERVICE, {"catalog_version": 6, "option_ordinal": 0}), CATALOG))[0] == "STALE_CATALOG"
    assert RESOLVER.resolve(intent(TelegramIntentKind.SELECT_SERVICE, {"catalog_version": 7, "option_ordinal": 0}), CATALOG).payload["service_option_id"] == "first"


@pytest.mark.parametrize("bad, expected", [(object(), "INVALID_INTENT"), (TelegramIntent(ChannelContext(Platform.VK, "i", "s", "c", "e"), TelegramIntentKind.OPEN, {}), "INVALID_CONTEXT"), (TelegramIntent(CTX, "OPEN", {}), "INVALID_INTENT"), (TelegramIntent(CTX, TelegramIntentKind.OPEN, {"x": 1}), "INVALID_INTENT")])
def test_exact_intent_and_context_validation(bad, expected):
    assert error_code(lambda: RESOLVER.resolve(bad, CATALOG))[0] == expected


def test_inputs_are_not_mutated_or_retained():
    payload = {"catalog_version": 7, "option_ordinal": 0}
    catalog = {"version": 7, "services": [{"options": [{"id": "first"}]}]}
    before = repr((payload, catalog))
    action = RESOLVER.resolve(TelegramIntent(CTX, TelegramIntentKind.SELECT_SERVICE, payload), catalog)
    assert repr((payload, catalog)) == before
    catalog["services"][0]["options"][0]["id"] = "changed"
    assert action.payload["service_option_id"] == "first"
