from types import MappingProxyType

import pytest

from apps.api.sivka_burka_api.channel_conversation import ChannelAction, ConversationResult
from apps.api.sivka_burka_api.channel_gateway import ChannelContext, Platform
from apps.bot.sivka_burka_bot.telegram_interactions import TelegramIntent, TelegramIntentKind
from apps.bot.sivka_burka_bot.telegram_screen_actions import TelegramScreenActionError, TelegramScreenActionResolver


CONTEXT = ChannelContext(Platform.TELEGRAM, "integration", "sender", "chat", "event")
CATALOG = {"catalog_version": 3, "services": []}
PREFLIGHT = ConversationResult("OPEN", "REQUESTER", {"catalog": CATALOG, "answers": {"service_option_id": "ride"}}, 1)
DETAILS_PREFLIGHT = ConversationResult(
    "OPEN",
    "DETAILS",
    {"catalog": CATALOG, "answers": {"service_option_id": "ride", "requester": {"name": "Alice"}}},
    2,
)


def intent(text="Alice", *, context=CONTEXT, kind=TelegramIntentKind.TEXT_INPUT, payload=None, callback=None):
    return TelegramIntent(context, kind, MappingProxyType({"text": text}) if payload is None else payload, callback)


def test_exact_requester_action_is_immutable_and_name_only():
    action = TelegramScreenActionResolver().resolve(intent(), PREFLIGHT)
    assert type(action) is ChannelAction
    assert action.kind == "SET_REQUESTER"
    assert type(action.payload) is MappingProxyType
    assert dict(action.payload) == {"name": "Alice"}
    with pytest.raises(TypeError):
        action.payload["contact"] = {"kind": "TELEGRAM", "value": "sender"}


@pytest.mark.parametrize("text", ["A", "x" * 200])
def test_name_boundaries_are_accepted(text):
    assert TelegramScreenActionResolver().resolve(intent(text), PREFLIGHT).payload["name"] == text


@pytest.mark.parametrize("text", ["x" * 201, "x\x00y"])
def test_name_boundaries_are_safe_invalid_requester_input(text):
    with pytest.raises(TelegramScreenActionError) as raised:
        TelegramScreenActionResolver().resolve(intent(text), PREFLIGHT)
    assert raised.value.code == "INVALID_REQUESTER_INPUT"
    assert text not in str(raised.value)
    assert text not in repr(raised.value)


@pytest.mark.parametrize("text", ["", " x", "x "])
def test_non_mapper_text_shape_is_invalid_intent(text):
    with pytest.raises(TelegramScreenActionError) as raised:
        TelegramScreenActionResolver().resolve(intent(text), PREFLIGHT)
    assert raised.value.code == "INVALID_INTENT"


@pytest.mark.parametrize("value", [object(), None, TelegramIntent(CONTEXT, TelegramIntentKind.OPEN, MappingProxyType({}))])
def test_wrong_intent_types_are_rejected(value):
    with pytest.raises(TelegramScreenActionError) as raised:
        TelegramScreenActionResolver().resolve(value, PREFLIGHT)
    assert raised.value.code == "INVALID_INTENT"


def test_forged_mutable_or_callback_intents_are_rejected():
    for value in (
        TelegramIntent(CONTEXT, TelegramIntentKind.TEXT_INPUT, {"text": "Alice"}),
        TelegramIntent(CONTEXT, TelegramIntentKind.TEXT_INPUT, MappingProxyType({"text": "Alice"}), "cb"),
        TelegramIntent(ChannelContext(Platform.VK, "integration", "sender", "chat", "event"), TelegramIntentKind.TEXT_INPUT, MappingProxyType({"text": "Alice"})),
    ):
        with pytest.raises(TelegramScreenActionError) as raised:
            TelegramScreenActionResolver().resolve(value, PREFLIGHT)
        assert raised.value.code == "INVALID_INTENT"


@pytest.mark.parametrize("result", [
    object(),
    ConversationResult("SAVED", "REQUESTER", {"catalog": CATALOG, "answers": {"service_option_id": "ride"}}, 1),
    ConversationResult("OPEN", "REQUESTER", {"catalog": CATALOG, "answers": {"service_option_id": "ride"}}, None),
    ConversationResult("OPEN", "REQUESTER", {"catalog": CATALOG, "answers": {"service_option_id": "ride"}}, 1, {"receipt": "hidden"}),
    ConversationResult("OPEN", "REQUESTER", {"catalog": CATALOG}, 1),
    ConversationResult("OPEN", "REQUESTER", {"catalog": CATALOG, "answers": {"service_option_id": "ride", "requester": {"name": "hidden"}}}, 1),
])
def test_malformed_or_unsupported_preflight_is_rejected(result):
    with pytest.raises(TelegramScreenActionError) as raised:
        TelegramScreenActionResolver().resolve(intent(), result)
    assert raised.value.code in {"INVALID_SCREEN_RESULT", "SCREEN_INPUT_UNSUPPORTED"}


def test_4096_text_is_valid_intent_but_invalid_requester_input():
    with pytest.raises(TelegramScreenActionError) as raised:
        TelegramScreenActionResolver().resolve(intent("x" * 4096), PREFLIGHT)
    assert raised.value.code == "INVALID_REQUESTER_INPUT"


@pytest.mark.parametrize(
    "experience, expected",
    [("Новичок", "BEGINNER"), ("опытный", "EXPERIENCED"), ("НЕ УКАЗАНО", "UNKNOWN")],
)
def test_details_input_maps_exact_immutable_action(experience, expected):
    text = (
        "Участники: 2\n"
        "Дата: 2026-10-01\n"
        "Время: -\n"
        f"Опыт: {experience}\n"
        "Комментарий: -"
    )
    action = TelegramScreenActionResolver().resolve(intent(text), DETAILS_PREFLIGHT)
    assert type(action) is ChannelAction
    assert action.kind == "SET_DETAILS"
    assert type(action.payload) is MappingProxyType
    assert dict(action.payload) == {
        "participants_count": 2,
        "requested_time": {"date": "2026-10-01", "time_text": None},
        "experience": expected,
        "comment": "",
    }


@pytest.mark.parametrize(
    "text",
    [
        "Дата: 2026-10-01",
        "Участники: 01\nДата: 2026-10-01\nВремя: -\nОпыт: Новичок\nКомментарий: -",
        "Участники: 101\nДата: 2026-10-01\nВремя: -\nОпыт: Новичок\nКомментарий: -",
        "Участники: 2\nДата: 2026-02-30\nВремя: -\nОпыт: Новичок\nКомментарий: -",
        "Участники: 2\nДата: 2026-10-01\nВремя: \nОпыт: Новичок\nКомментарий: -",
        "Участники: 2\nДата: 2026-10-01\nВремя: -\nОпыт: Неопытный\nКомментарий: -",
        "Участники: 2\nДата: 2026-10-01\nВремя: -\nОпыт: Новичок\nКомментарий: x\x00y",
    ],
)
def test_malformed_details_are_safe_and_typed(text):
    with pytest.raises(TelegramScreenActionError) as raised:
        TelegramScreenActionResolver().resolve(intent(text), DETAILS_PREFLIGHT)
    assert raised.value.code == "INVALID_DETAILS_INPUT"
    assert text not in str(raised.value)
    assert text not in repr(raised.value)


@pytest.mark.parametrize(
    "preflight",
    [
        ConversationResult("SAVED", "DETAILS", DETAILS_PREFLIGHT.data, 2),
        ConversationResult("OPEN", "DETAILS", {"catalog": CATALOG, "answers": {"service_option_id": "ride"}}, 2),
        ConversationResult("OPEN", "DETAILS", DETAILS_PREFLIGHT.data, None),
        ConversationResult("OPEN", "DETAILS", DETAILS_PREFLIGHT.data, 2, {"trusted": "hidden"}),
    ],
)
def test_malformed_details_preflight_is_rejected(preflight):
    text = "Участники: 2\nДата: 2026-10-01\nВремя: -\nОпыт: Новичок\nКомментарий: -"
    with pytest.raises(TelegramScreenActionError) as raised:
        TelegramScreenActionResolver().resolve(intent(text), preflight)
    assert raised.value.code == "INVALID_SCREEN_RESULT"
