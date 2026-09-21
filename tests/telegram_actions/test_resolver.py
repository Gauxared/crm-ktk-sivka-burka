from collections.abc import Mapping
from types import MappingProxyType

import pytest

from apps.api.sivka_burka_api.channel_gateway import ChannelContext, Platform
from apps.bot.sivka_burka_bot.telegram_actions import TelegramActionError, TelegramActionResolver
from apps.bot.sivka_burka_bot.telegram_interactions import TelegramIntent, TelegramIntentKind


CTX = ChannelContext(Platform.TELEGRAM, "i", "s", "c", "e")
CATALOG = {
    "catalog_version": 7, "club_timezone": "Europe/Moscow", "contact_info": "club@example.test",
    "location_link": "https://example.test/location", "visit_rules": "Synthetic rules",
    "services": [
        {"id": "service-1", "title": "One", "description": None, "information": None,
         "options": [{"id": "first"}, {"id": "off", "active": False}]},
        {"id": "service-2", "title": "Two", "description": None, "information": None,
         "options": [{"id": "second", "active": True}]},
    ],
}
RESOLVER = TelegramActionResolver()


def intent(kind, payload=None, context=CTX, callback_query_id=None):
    return TelegramIntent(context, kind, MappingProxyType(payload or {}), callback_query_id)


def selection(payload=None, catalog=CATALOG, callback_query_id="callback-1"):
    if payload is None:
        payload = {"catalog_version": 7, "option_ordinal": 0}
    return RESOLVER.resolve(intent(TelegramIntentKind.SELECT_SERVICE, payload, callback_query_id=callback_query_id), catalog)


def error_code(call):
    with pytest.raises(TelegramActionError) as caught:
        call()
    return caught.value.code, str(caught.value), repr(caught.value)


@pytest.mark.parametrize(("kind", "expected"), [(TelegramIntentKind.OPEN, "OPEN"), (TelegramIntentKind.HELP, "HELP"), (TelegramIntentKind.RESET, "RESET"), (TelegramIntentKind.SUBMIT, "SUBMIT")])
def test_direct_actions_are_exact(kind, expected):
    action = RESOLVER.resolve(intent(kind), CATALOG)
    assert action.kind == expected and dict(action.payload) == {}


def test_realistic_public_catalog_runtime_projection_resolves_in_order():
    action = selection({"catalog_version": 7, "option_ordinal": 1})
    assert action.kind == "SELECT_SERVICE"
    assert dict(action.payload) == {"service_option_id": "second"}
    assert set(action.payload) == {"service_option_id"}


def test_legacy_version_key_is_not_a_catalog_version():
    legacy = {"version": 7, "services": CATALOG["services"]}
    assert error_code(lambda: selection(catalog=legacy))[0] == "CATALOG_UNAVAILABLE"


@pytest.mark.parametrize("ordinal", [0, 1])
def test_ordinal_boundaries(ordinal):
    assert selection({"catalog_version": 7, "option_ordinal": ordinal}).payload["service_option_id"] in {"first", "second"}


@pytest.mark.parametrize("ordinal", [2, 2_147_483_647])
def test_unknown_ordinal(ordinal):
    assert error_code(lambda: selection({"catalog_version": 7, "option_ordinal": ordinal}))[0] == "UNKNOWN_SERVICE_SELECTION"


def test_text_is_deferred_only_for_valid_mapper_shape_and_error_is_safe():
    code, text, representation = error_code(lambda: RESOLVER.resolve(intent(TelegramIntentKind.TEXT_INPUT, {"text": "secret"}), CATALOG))
    assert code == "SCREEN_REQUIRED" and "secret" not in text and "secret" not in representation


@pytest.mark.parametrize("payload, callback", [({}, None), ({"text": ""}, None), ({"text": " text"}, None), ({"text": "x" * 4097}, None), ({"text": "ok", "x": 1}, None), ({"text": "ok"}, "callback")])
def test_malformed_text_input_is_invalid_intent(payload, callback):
    assert error_code(lambda: RESOLVER.resolve(intent(TelegramIntentKind.TEXT_INPUT, payload, callback_query_id=callback), CATALOG))[0] == "INVALID_INTENT"


@pytest.mark.parametrize("payload", [{}, {"catalog_version": 7}, {"catalog_version": 7, "option_ordinal": 0, "x": 1}, {"catalog_version": True, "option_ordinal": 0}, {"catalog_version": 7, "option_ordinal": False}, {"catalog_version": 0, "option_ordinal": 0}, {"catalog_version": 7, "option_ordinal": 0.0}])
def test_select_payload_is_strict(payload):
    assert error_code(lambda: selection(payload))[0] == "INVALID_INTENT"


@pytest.mark.parametrize("catalog", [None, {}, {"version": True, "services": []}, {"catalog_version": True, "services": []}, {"catalog_version": 0, "services": []}, {"catalog_version": 1, "services": {}}, {"catalog_version": 1, "services": [{"options": {}}]}, {"catalog_version": 1, "services": [{"options": [None]}]}, {"catalog_version": 1, "services": [{"options": [{"id": "x", "active": None}]}]}, {"catalog_version": 1, "services": [{"options": [{"id": " x "}]}]}, {"catalog_version": 1, "services": [{"options": [{"id": "x"}, {"id": "x"}]}]}])
def test_catalog_failures_are_safe(catalog):
    assert error_code(lambda: selection({"catalog_version": 1, "option_ordinal": 0}, catalog))[0] == "CATALOG_UNAVAILABLE"


def test_stale_catalog_and_immutability():
    assert error_code(lambda: selection({"catalog_version": 6, "option_ordinal": 0}))[0] == "STALE_CATALOG"
    payload = MappingProxyType({"catalog_version": 7, "option_ordinal": 0})
    catalog = {"catalog_version": 7, "services": [{"options": [{"id": "first"}]}]}
    before = repr((payload, catalog))
    action = RESOLVER.resolve(TelegramIntent(CTX, TelegramIntentKind.SELECT_SERVICE, payload, "callback-1"), catalog)
    assert repr((payload, catalog)) == before
    catalog["services"][0]["options"][0]["id"] = "changed"
    assert action.payload["service_option_id"] == "first"


@pytest.mark.parametrize("bad, expected", [(object(), "INVALID_INTENT"), (TelegramIntent(ChannelContext(Platform.VK, "i", "s", "c", "e"), TelegramIntentKind.OPEN, MappingProxyType({})), "INVALID_CONTEXT"), (TelegramIntent(CTX, "OPEN", MappingProxyType({})), "INVALID_INTENT"), (TelegramIntent(CTX, TelegramIntentKind.OPEN, MappingProxyType({"x": 1})), "INVALID_INTENT"), (TelegramIntent(CTX, TelegramIntentKind.OPEN, {}), "INVALID_INTENT"), (TelegramIntent(CTX, TelegramIntentKind.SELECT_SERVICE, {"catalog_version": 7, "option_ordinal": 0}, "callback"), "INVALID_INTENT")])
def test_exact_intent_context_and_mapper_payload_validation(bad, expected):
    assert error_code(lambda: RESOLVER.resolve(bad, CATALOG))[0] == expected


def test_select_requires_callback_id_before_catalog_access():
    class ExplodingCatalog(Mapping):
        def __getitem__(self, key):
            raise AssertionError("catalog must not be read")

        def __iter__(self):
            raise AssertionError("catalog must not be read")

        def __len__(self):
            raise AssertionError("catalog must not be read")

        def get(self, key, default=None):
            raise AssertionError("catalog must not be read")

    bad = intent(TelegramIntentKind.SELECT_SERVICE, {"catalog_version": 7, "option_ordinal": 0})
    assert error_code(lambda: RESOLVER.resolve(bad, ExplodingCatalog()))[0] == "INVALID_INTENT"
