from dataclasses import FrozenInstanceError

import pytest

from apps.api.sivka_burka_api.channel_gateway import ChannelContext, Platform
from apps.bot.sivka_burka_bot.telegram_interactions import TelegramInteractionError, TelegramInteractionMapper, TelegramIntentKind
from apps.bot.sivka_burka_bot.telegram_updates import AcceptedTelegramUpdate, InputKind


MAPPER = TelegramInteractionMapper()
CONTEXT = ChannelContext(Platform.TELEGRAM, "integration", "sender", "conversation", "event")


def trusted(*, kind=InputKind.TEXT, text=None, data=None, callback_query_id=None, context=CONTEXT):
    return AcceptedTelegramUpdate(context, kind, text=text, data=data, callback_query_id=callback_query_id)


def error(value):
    with pytest.raises(TelegramInteractionError) as raised:
        MAPPER.map(value)
    return raised.value


@pytest.mark.parametrize("value", [None, {}, object(), "update"])
def test_wrong_objects_are_invalid_update(value):
    assert error(value).code == "INVALID_UPDATE"


def test_subclass_is_not_an_accepted_update():
    class Derived(AcceptedTelegramUpdate):
        pass

    assert error(Derived(CONTEXT, InputKind.TEXT, text="hello")).code == "INVALID_UPDATE"


@pytest.mark.parametrize("context", [None, object(), ChannelContext(Platform.VK, "i", "s", "c", "e"), ChannelContext(Platform.TELEGRAM, " ", "s", "c", "e")])
def test_invalid_or_non_telegram_context_is_rejected(context):
    assert error(trusted(text="hello", context=context)).code == "INVALID_CONTEXT"


@pytest.mark.parametrize("update", [
    trusted(kind=InputKind.TEXT), trusted(text=""), trusted(text=" "), trusted(text=" hello"), trusted(text="hello "),
    trusted(text="x" * 4097), trusted(text="hello", data="x"), trusted(text="hello", callback_query_id="qid"), trusted(kind="TEXT", text="hello"),
    trusted(kind=InputKind.CALLBACK, data="v1:o"), trusted(kind=InputKind.CALLBACK, text="x", data="v1:o", callback_query_id="qid"),
    trusted(kind=InputKind.CALLBACK, data="", callback_query_id="qid"), trusted(kind=InputKind.CALLBACK, data="x" * 65, callback_query_id="qid"),
    trusted(kind=InputKind.CALLBACK, data="🙂" * 17, callback_query_id="qid"), trusted(kind=InputKind.CALLBACK, data="v1:o", callback_query_id=None),
    trusted(kind=InputKind.CALLBACK, data="v1:o", callback_query_id=""), trusted(kind=InputKind.CALLBACK, data="v1:o", callback_query_id=" qid"),
    trusted(kind=InputKind.CALLBACK, data="v1:o", callback_query_id="qid "), trusted(kind=InputKind.CALLBACK, data="v1:o", callback_query_id="q" * 257),
    trusted(kind=InputKind.CALLBACK, data=object(), callback_query_id="qid"),
])
def test_every_invalid_text_or_callback_shape_is_malformed(update):
    assert error(update).code == "MALFORMED_UPDATE"


@pytest.mark.parametrize(("command", "kind"), [
    ("/start", TelegramIntentKind.OPEN), ("/START@bot_1", TelegramIntentKind.OPEN),
    ("/help", TelegramIntentKind.HELP), ("/HeLp@Bot", TelegramIntentKind.HELP),
    ("/reset", TelegramIntentKind.RESET), ("/RESET@bot", TelegramIntentKind.RESET),
])
def test_exact_commands_and_username_suffixes(command, kind):
    intent = MAPPER.map(trusted(text=command))
    assert intent.kind is kind and intent.context is CONTEXT and dict(intent.payload) == {}


@pytest.mark.parametrize("command", ["/start now", "/start@", "/start@bot-name", "/ѕtart", "/startx", "/unknown", "/"])
def test_command_variants_and_all_unknown_slash_commands_are_unsupported(command):
    assert error(trusted(text=command)).code == "UNSUPPORTED_COMMAND"


@pytest.mark.parametrize("command", [" /start", "/start "])
def test_outer_whitespace_is_a_malformed_update(command):
    assert error(trusted(text=command)).code == "MALFORMED_UPDATE"


@pytest.mark.parametrize("text", ["hello", "Привет", "你好", "🙂 /start", "a\nb"])
def test_arbitrary_non_command_unicode_text_is_preserved_exactly(text):
    intent = MAPPER.map(trusted(text=text))
    assert intent.kind is TelegramIntentKind.TEXT_INPUT
    assert intent.context is CONTEXT and intent.payload["text"] is text


@pytest.mark.parametrize(("data", "kind"), [("v1:o", TelegramIntentKind.OPEN), ("v1:h", TelegramIntentKind.HELP), ("v1:r", TelegramIntentKind.RESET), ("v1:u", TelegramIntentKind.SUBMIT)])
def test_all_fixed_callbacks_preserve_callback_id(data, kind):
    callback_id = "callback-id"
    intent = MAPPER.map(trusted(kind=InputKind.CALLBACK, data=data, callback_query_id=callback_id))
    assert intent.kind is kind and intent.callback_query_id is callback_id and dict(intent.payload) == {}


@pytest.mark.parametrize(("data", "code"), [
    ("v1:s", "MALFORMED_CALLBACK"), ("v1:s:", "MALFORMED_CALLBACK"), ("v1:s:1", "MALFORMED_CALLBACK"), ("v1:s:1:0:0", "MALFORMED_CALLBACK"),
    ("v1:o:1", "MALFORMED_CALLBACK"), ("v1:h:", "MALFORMED_CALLBACK"), ("v1:r:x", "MALFORMED_CALLBACK"), ("v1:u:0", "MALFORMED_CALLBACK"), ("v1:", "MALFORMED_CALLBACK"),
    ("v1:x", "UNSUPPORTED_CALLBACK"), ("v1:x:1:0", "UNSUPPORTED_CALLBACK"), ("v2:o", "UNSUPPORTED_CALLBACK"),
    ("service-id", "UNSUPPORTED_CALLBACK"), ('{"catalog_version":1}', "UNSUPPORTED_CALLBACK"), ("v1:s:service-id:0", "MALFORMED_CALLBACK"),
])
def test_callback_shape_classification_is_exact_and_never_raises_implementation_errors(data, code):
    assert error(trusted(kind=InputKind.CALLBACK, data=data, callback_query_id="qid")).code == code


@pytest.mark.parametrize(("data", "payload"), [("v1:s:1:0", {"catalog_version": 1, "option_ordinal": 0}), ("v1:s:2147483647:2147483647", {"catalog_version": 2147483647, "option_ordinal": 2147483647})])
def test_service_boundaries_are_canonical_and_compact(data, payload):
    intent = MAPPER.map(trusted(kind=InputKind.CALLBACK, data=data, callback_query_id="qid"))
    assert intent.kind is TelegramIntentKind.SELECT_SERVICE and dict(intent.payload) == payload


@pytest.mark.parametrize("data", ["v1:s:0:0", "v1:s:01:0", "v1:s:1:00", "v1:s:+1:0", "v1:s:1:+0", "v1:s: 1:0", "v1:s:1:0 ", "v1:s:١:0", "v1:s:1:０", "v1:s:1::0", "v1:s:1:abc", "v1:s:2147483648:0", "v1:s:1:2147483648"])
def test_noncanonical_or_out_of_range_service_fields_are_malformed(data):
    assert error(trusted(kind=InputKind.CALLBACK, data=data, callback_query_id="qid")).code == "MALFORMED_CALLBACK"


def test_intent_and_payload_are_immutable_and_errors_do_not_leak_input_or_identifiers():
    intent = MAPPER.map(trusted(text="hello"))
    with pytest.raises(TypeError):
        intent.payload["text"] = "other"
    with pytest.raises(FrozenInstanceError):
        intent.kind = TelegramIntentKind.HELP
    raised = error(trusted(kind=InputKind.CALLBACK, data="v1:s:private-id:0", callback_query_id="trusted-id"))
    assert str(raised) == "Invalid Telegram interaction"
    assert repr(raised) == "TelegramInteractionError('Invalid Telegram interaction')"
    assert "private-id" not in str(raised) + repr(raised) and "trusted-id" not in str(raised) + repr(raised)
