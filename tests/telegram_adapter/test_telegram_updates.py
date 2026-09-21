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


def adapter():
    return TelegramUpdateAdapter("integration-1", SECRET)


def message(**overrides):
    value = {"message_id": 7, "from": {"id": 42, "is_bot": False}, "chat": {"id": 42, "type": "private"}, "text": "Привет"}
    value.update(overrides)
    return {"update_id": 9, "message": value}


def callback(**overrides):
    value = {"id": "cq-1", "from": {"id": 42, "is_bot": False}, "data": "BOOK", "message": {"message_id": 8, "chat": {"id": 42, "type": "private"}}}
    value.update(overrides)
    return {"update_id": 10, "callback_query": value}


def error_code(body, header=SECRET):
    with pytest.raises(TelegramUpdateError) as error:
        adapter().parse(header, body)
    return error.value.code


@pytest.mark.parametrize("secret", [None, 1, "", "bad secret", "секрет", "x" * 257])
def test_constructor_rejects_secret_classes_without_leaking_values(secret):
    with pytest.raises(ValueError) as error:
        TelegramUpdateAdapter("integration-1", secret)
    assert not secret or str(secret) not in str(error.value)
    assert "секрет" not in repr(error.value)


@pytest.mark.parametrize("integration_id", ["", " leading", "trailing ", "a\x00b", "x" * 129])
def test_constructor_rejects_invalid_integration_ids(integration_id):
    with pytest.raises(ValueError):
        TelegramUpdateAdapter(integration_id, SECRET)


@pytest.mark.parametrize("header", [None, 1, "wrong"])
def test_unauthorized_headers_have_identical_safe_error(header):
    with pytest.raises(TelegramUpdateError) as error:
        adapter().parse(header, {"update_id": 1, "message": "body"})
    assert error.value.code == "UNAUTHORIZED"
    assert str(error.value) == "Unauthorized Telegram update"
    assert repr(error.value) == "TelegramUpdateError('Unauthorized Telegram update')"
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value)


def test_unauthorized_parse_does_not_inspect_body():
    class ExplodingBody(dict):
        def __contains__(self, key):
            raise AssertionError("body was inspected")

        def get(self, key, default=None):
            raise AssertionError("body was inspected")

    assert error_code(ExplodingBody(), header=None) == "UNAUTHORIZED"


def test_secret_is_absent_from_accepted_and_ignored_serialization():
    accepted = adapter().parse(SECRET, message())
    ignored = adapter().parse(SECRET, {"update_id": 1, "poll": {"id": "secret_123"}})
    for result in (accepted, ignored):
        assert SECRET not in str(result)
        assert SECRET not in repr(result)


@pytest.mark.parametrize("update_id", [0, 2_147_483_647])
def test_message_accepts_update_id_bounds_and_validates_context(update_id):
    body = message()
    body["update_id"] = update_id
    result = adapter().parse(SECRET, body)
    assert isinstance(result, AcceptedTelegramUpdate)
    assert result.input_kind is InputKind.TEXT
    assert result.text == "Привет"
    assert result.context == result.context.__class__(Platform.TELEGRAM, "integration-1", "42", "42", f"telegram-update:{update_id}")


def test_message_accepts_4096_characters_and_is_deterministic():
    body = message(text="x" * 4096)
    first = adapter().parse(SECRET, body)
    second = adapter().parse(SECRET, dict(body))
    assert first == second
    assert first.text == "x" * 4096


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from", None), ("from", {}), ("from", {"id": True, "is_bot": False}),
        ("from", {"id": -1, "is_bot": False}), ("from", {"id": 42}),
        ("from", {"id": 42, "is_bot": None}), ("from", {"id": 42, "is_bot": 1}),
        ("chat", None), ("chat", "private"), ("chat", {}), ("chat", {"id": True, "type": "private"}),
        ("chat", {"id": 42}), ("chat", {"id": 42, "type": "unknown"}),
        ("message_id", None), ("message_id", True), ("message_id", -1), ("message_id", "7"),
        ("text", 1), ("text", ""), ("text", "   "), ("text", "x" * 4097),
    ],
)
def test_message_malformed_sender_chat_message_id_and_text_classes(field, value):
    assert error_code(message(**{field: value})) == "MALFORMED_UPDATE"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (message(**{"from": {"id": 42, "is_bot": True}}), "BOT_MESSAGE"),
        (message(chat={"id": 42, "type": "group"}), "NON_PRIVATE_MESSAGE"),
        (message(chat={"id": 42, "type": "supergroup"}), "NON_PRIVATE_MESSAGE"),
        (message(chat={"id": 42, "type": "channel"}), "NON_PRIVATE_MESSAGE"),
        (message(text=None), "UNSUPPORTED_MESSAGE"),
    ],
)
def test_message_ignores_are_exact(body, reason):
    assert adapter().parse(SECRET, body) == IgnoredTelegramUpdate(reason)


@pytest.mark.parametrize("body", [{"update_id": 1}, {"update_id": 1, "spoofed_payload": {}}, {"update_id": True, "message": {}}, {"update_id": -1, "message": {}}, {"update_id": 2_147_483_648, "message": {}}])
def test_no_member_spoof_only_and_update_id_bounds_are_malformed(body):
    assert error_code(body) == "MALFORMED_UPDATE"


def test_message_and_callback_together_are_malformed():
    assert error_code({**message(), "callback_query": callback()["callback_query"]}) == "MALFORMED_UPDATE"


@pytest.mark.parametrize("member", [
    "message", "edited_message", "channel_post", "edited_channel_post", "business_connection", "business_message", "edited_business_message", "deleted_business_messages", "guest_message", "message_reaction", "message_reaction_count", "inline_query", "chosen_inline_result", "callback_query", "shipping_query", "pre_checkout_query", "purchased_paid_media", "poll", "poll_answer", "my_chat_member", "chat_member", "chat_join_request", "chat_boost", "removed_chat_boost", "managed_bot", "subscription", "stopped_message_generation",
])
def test_every_bot_api_10_3_update_member_has_explicit_behavior(member):
    if member in {"message", "callback_query"}:
        assert error_code({"update_id": 1, member: {}}) == "MALFORMED_UPDATE"
    else:
        assert adapter().parse(SECRET, {"update_id": 1, member: {}}) == IgnoredTelegramUpdate("UNSUPPORTED_UPDATE")


@pytest.mark.parametrize("callback_id", [None, "", "   ", 1, "x" * 257])
def test_callback_id_malformed_classes(callback_id):
    assert error_code(callback(id=callback_id)) == "MALFORMED_UPDATE"


@pytest.mark.parametrize("sender", [None, {}, {"id": True, "is_bot": False}, {"id": -1, "is_bot": False}, {"id": 42}, {"id": 42, "is_bot": None}])
def test_callback_sender_malformed_classes(sender):
    assert error_code(callback(**{"from": sender})) == "MALFORMED_UPDATE"


def test_callback_bot_is_ignored():
    assert adapter().parse(SECRET, callback(**{"from": {"id": 42, "is_bot": True}})) == IgnoredTelegramUpdate("BOT_CALLBACK")


@pytest.mark.parametrize("body", [
    callback(message=None), callback(message="bad"), callback(message={}),
    callback(message={"message_id": 8}),
    callback(message={"message_id": True, "chat": {"id": 42, "type": "private"}}),
    callback(message={"message_id": -1, "chat": {"id": 42, "type": "private"}}),
    callback(message={"message_id": "8", "chat": {"id": 42, "type": "private"}}),
    callback(message={"message_id": 8, "chat": None}),
    callback(message={"message_id": 8, "chat": "private"}),
    callback(message={"message_id": 8, "chat": {"id": True, "type": "private"}}),
    callback(message={"message_id": 8, "chat": {"id": 42}}),
])
def test_callback_message_and_chat_boundaries(body):
    if body["callback_query"].get("message") is None:
        assert adapter().parse(SECRET, body) == IgnoredTelegramUpdate("INLINE_CALLBACK")
    else:
        assert error_code(body) == "MALFORMED_UPDATE"


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
def test_callback_explicit_non_private_chats_are_ignored(chat_type):
    body = callback(message={"message_id": 8, "chat": {"id": 42, "type": chat_type}})
    assert adapter().parse(SECRET, body) == IgnoredTelegramUpdate("NON_PRIVATE_CALLBACK")


@pytest.mark.parametrize("data", [None, "", 1, "x" * 65, "я" * 33])
def test_callback_data_malformed_classes(data):
    assert error_code(callback(data=data)) == "MALFORMED_UPDATE"


@pytest.mark.parametrize("data", ["x" * 64, "я" * 32])
def test_callback_accepts_exact_64_utf8_bytes_and_preserves_fields(data):
    result = adapter().parse(SECRET, callback(data=data))
    assert isinstance(result, AcceptedTelegramUpdate)
    assert result.input_kind is InputKind.CALLBACK
    assert result.data == data
    assert result.callback_query_id == "cq-1"
    assert result.context.platform is Platform.TELEGRAM
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.data = "x"


def test_callback_context_is_deterministic_and_immutable():
    result = adapter().parse(SECRET, callback())
    assert result.context.event_id == "telegram-update:10"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.context = result.context


def test_every_accepted_context_is_sent_through_gateway_validation(monkeypatch):
    calls = []
    original = telegram_updates.validate_context

    def recording_validator(context):
        calls.append(context)
        return original(context)

    monkeypatch.setattr(telegram_updates, "validate_context", recording_validator)
    adapter().parse(SECRET, message())
    adapter().parse(SECRET, callback())
    assert len(calls) == 2
