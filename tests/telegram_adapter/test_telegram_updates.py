import dataclasses

import pytest

import apps.bot.sivka_burka_bot.telegram_updates as telegram_updates
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


@pytest.mark.parametrize("secret", [None, 1, "", "bad secret", "x" * 257])
def test_constructor_rejects_invalid_secrets_without_leaking_them(secret):
    with pytest.raises(ValueError):
        TelegramUpdateAdapter("integration-1", secret)


@pytest.mark.parametrize("integration_id", ["", " leading", "trailing ", "a\x00b", "x" * 129])
def test_constructor_rejects_invalid_integration_ids(integration_id):
    with pytest.raises(ValueError):
        TelegramUpdateAdapter(integration_id, SECRET)


def test_unauthorized_parse_does_not_inspect_body():
    class ExplodingBody(dict):
        def __contains__(self, key):
            raise AssertionError("body was inspected")

        def get(self, key, default=None):
            raise AssertionError("body was inspected")

    with pytest.raises(TelegramUpdateError) as error:
        adapter().parse(None, ExplodingBody())
    assert error.value.code == "UNAUTHORIZED"
    assert "secret_123" not in str(error.value)
    assert "secret_123" not in repr(error.value)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (callback(message=None), "INLINE_CALLBACK"),
        (callback(message={"message_id": 8, "chat": {"id": 42, "type": "group"}}), "NON_PRIVATE_CALLBACK"),
        (callback(message={"message_id": 8, "chat": {"id": 42, "type": "supergroup"}}), "NON_PRIVATE_CALLBACK"),
        (callback(message={"message_id": 8, "chat": {"id": 42, "type": "channel"}}), "NON_PRIVATE_CALLBACK"),
    ],
)
def test_callback_ignores_only_explicit_non_private_or_inline_messages(body, reason):
    assert adapter().parse(SECRET, body) == IgnoredTelegramUpdate(reason)


@pytest.mark.parametrize(
    "message_value",
    [{}, {"message_id": 8}, {"message_id": 8, "chat": {}}, {"message_id": True, "chat": {"id": 42, "type": "private"}}],
)
def test_callback_malformed_message_and_chat_are_errors(message_value):
    with pytest.raises(TelegramUpdateError) as error:
        adapter().parse(SECRET, callback(message=message_value))
    assert error.value.code == "MALFORMED_UPDATE"


@pytest.mark.parametrize(
    "body",
    [
        {"update_id": 1},
        {"update_id": 1, "spoofed_payload": {}},
        {"update_id": 1, "message": {}, "callback_query": {}},
        {"update_id": -1, "message": {}},
        {"update_id": 2_147_483_648, "message": {}},
        {"update_id": True, "message": {}},
    ],
)
def test_no_supported_payloads_and_update_id_bounds_are_malformed(body):
    with pytest.raises(TelegramUpdateError) as error:
        adapter().parse(SECRET, body)
    assert error.value.code == "MALFORMED_UPDATE"


@pytest.mark.parametrize("member", ["edited_message", "channel_post", "edited_channel_post", "inline_query", "my_chat_member"])
def test_recognized_unsupported_update_members_are_ignored(member):
    assert adapter().parse(SECRET, {"update_id": 1, member: {"spoof": True}}) == IgnoredTelegramUpdate("UNSUPPORTED_UPDATE")


def test_acceptance_is_immutable_and_deterministic_without_spoofing_context():
    body = message(platform="VK", integration_id="spoof", event_id="spoof")
    first = adapter().parse(SECRET, body)
    second = adapter().parse(SECRET, dict(body))
    assert first == second
    assert first.context.platform is Platform.TELEGRAM
    assert first.context.integration_id == "integration-1"
    assert first.context.event_id == "telegram-update:9"
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.context = first.context


def test_every_accepted_context_is_sent_through_gateway_validation(monkeypatch):
    calls = []
    original = telegram_updates.validate_context

    def recording_validator(context):
        calls.append(context)
        return original(context)

    monkeypatch.setattr(telegram_updates, "validate_context", recording_validator)
    telegram_updates.TelegramUpdateAdapter("integration-1", SECRET).parse(SECRET, message())
    telegram_updates.TelegramUpdateAdapter("integration-1", SECRET).parse(SECRET, callback())
    assert len(calls) == 2
