import dataclasses

import pytest

from apps.api.sivka_burka_api.channel_gateway import Platform
from apps.bot.sivka_burka_bot.telegram_updates import (
    AcceptedTelegramUpdate,
    IgnoredTelegramUpdate,
    InputKind,
    TelegramUpdateAdapter,
    TelegramUpdateError,
)


SECRET = "secret_123"


def adapter() -> TelegramUpdateAdapter:
    return TelegramUpdateAdapter("integration-1", SECRET)


def message(**overrides):
    value = {"message_id": 7, "from": {"id": 42, "is_bot": False}, "chat": {"id": 42, "type": "private"}, "text": "  Привет  "}
    value.update(overrides)
    return {"update_id": 9, "message": value}


def callback(**overrides):
    value = {"id": "cq-1", "from": {"id": 42, "is_bot": False}, "data": "BOOK", "message": {"message_id": 8, "chat": {"id": 42, "type": "private"}}}
    value.update(overrides)
    return {"update_id": 10, "callback_query": value}


def test_secret_is_checked_before_body_and_result_is_normalized():
    with pytest.raises(TelegramUpdateError) as error:
        adapter().parse("wrong", {"secret": SECRET})
    assert error.value.code == "UNAUTHORIZED"
    assert SECRET not in repr(error.value)

    result = adapter().parse(SECRET, message())
    assert isinstance(result, AcceptedTelegramUpdate)
    assert result.context == result.context.__class__(Platform.TELEGRAM, "integration-1", "42", "42", "telegram-update:9")
    assert result.text == "Привет"
    assert result.input_kind is InputKind.TEXT


def test_callback_uses_utf8_limit_and_is_immutable():
    result = adapter().parse(SECRET, callback())
    assert isinstance(result, AcceptedTelegramUpdate)
    assert result.data == "BOOK"
    assert result.callback_query_id == "cq-1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.data = "x"
    with pytest.raises(TelegramUpdateError):
        adapter().parse(SECRET, callback(data="я" * 33))


def test_bool_ids_and_contradictory_supported_payload_are_malformed():
    with pytest.raises(TelegramUpdateError) as error:
        adapter().parse(SECRET, message(message_id=True))
    assert error.value.code == "MALFORMED_UPDATE"
    with pytest.raises(TelegramUpdateError):
        adapter().parse(SECRET, {**message(), "callback_query": callback()["callback_query"]})


def test_valid_unsupported_updates_are_ignored_without_context():
    assert adapter().parse(SECRET, {"update_id": 1, "edited_message": {}}) == IgnoredTelegramUpdate("UNSUPPORTED_UPDATE")
    assert adapter().parse(SECRET, message(chat={"id": 42, "type": "group"})) == IgnoredTelegramUpdate("NON_PRIVATE_MESSAGE")
    assert adapter().parse(SECRET, callback(message=None)) == IgnoredTelegramUpdate("INLINE_CALLBACK")
