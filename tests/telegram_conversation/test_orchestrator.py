from dataclasses import dataclass
from types import MappingProxyType

import pytest

from apps.api.sivka_burka_api.channel_conversation import ChannelConversation, ConversationResult
from apps.api.sivka_burka_api.channel_gateway import ChannelContext, Platform
from apps.bot.sivka_burka_bot.telegram_actions import TelegramActionError, TelegramActionResolver
from apps.bot.sivka_burka_bot.telegram_conversation import (
    TelegramConversationError,
    TelegramConversationOrchestrator,
    TelegramConversationOutcome,
)
from apps.bot.sivka_burka_bot.telegram_interactions import TelegramInteractionMapper
from apps.bot.sivka_burka_bot.telegram_updates import AcceptedTelegramUpdate, InputKind


CONTEXT = ChannelContext(Platform.TELEGRAM, "integration", "sender", "chat", "event")
CATALOG = {"catalog_version": 3, "services": [{"options": [{"id": "ride", "active": True}]}]}


class EmptyGateway:
    def read_catalog(self, context): return None
    def read_draft(self, context): return None
    def save_draft(self, *args, **kwargs): return None
    def submit_inquiry(self, *args, **kwargs): return None
    def reset_draft(self, *args, **kwargs): return None


def update(text=None, data=None, callback=None):
    return AcceptedTelegramUpdate(CONTEXT, InputKind.TEXT if text is not None else InputKind.CALLBACK, text=text, data=data, callback_query_id=callback)


def harness(results):
    conversation = ChannelConversation(EmptyGateway())
    calls = []

    def handle(context, action):
        calls.append((context, action))
        return results[len(calls) - 1]

    conversation.handle = handle
    return TelegramConversationOrchestrator(TelegramInteractionMapper(), TelegramActionResolver(), conversation), calls


def test_strict_dependencies_and_immutable_outcome():
    with pytest.raises(TypeError):
        TelegramConversationOrchestrator(object(), TelegramActionResolver(), ChannelConversation(EmptyGateway()))
    with pytest.raises(TypeError):
        TelegramConversationOrchestrator(TelegramInteractionMapper(), object(), ChannelConversation(EmptyGateway()))
    with pytest.raises(TypeError):
        TelegramConversationOrchestrator(TelegramInteractionMapper(), TelegramActionResolver(), object())
    outcome = TelegramConversationOutcome(ConversationResult("OPEN", "START", {}), "cb")
    assert outcome.callback_query_id == "cb"
    with pytest.raises(AttributeError):
        outcome.callback_query_id = "other"


@pytest.mark.parametrize("command", ["/start", "/help", "/reset"])
def test_direct_command_is_one_call_without_catalog(command):
    expected = ConversationResult("OPEN", "START", {})
    orchestrator, calls = harness([expected])
    outcome = orchestrator.handle(update(text=command))
    assert outcome.result is expected
    assert len(calls) == 1
    assert calls[0][0] is CONTEXT
    assert calls[0][1].kind in {"OPEN", "HELP", "RESET"}


def test_submit_is_one_call_and_preserves_callback_id():
    expected = ConversationResult("ACCEPTED_UNCONFIRMED", "ACCEPTED_UNCONFIRMED", {})
    orchestrator, calls = harness([expected])
    outcome = orchestrator.handle(update(data="v1:u", callback="callback-1"))
    assert outcome.result is expected
    assert outcome.callback_query_id == "callback-1"
    assert len(calls) == 1 and calls[0][0] is CONTEXT


def test_select_service_preflights_then_resolves_with_same_context_and_callback():
    preflight = ConversationResult("OPEN", "SELECT_SERVICE", {"catalog": CATALOG})
    selected = ConversationResult("SAVED", "REQUESTER", {})
    orchestrator, calls = harness([preflight, selected])
    outcome = orchestrator.handle(update(data="v1:s:3:0", callback="callback-2"))
    assert outcome.result is selected
    assert outcome.callback_query_id == "callback-2"
    assert len(calls) == 2
    assert calls[0][0] is calls[1][0] is CONTEXT
    assert calls[0][1].kind == "OPEN"
    assert calls[1][1].kind == "SELECT_SERVICE"
    assert calls[1][1].payload["service_option_id"] == "ride"


@pytest.mark.parametrize("preflight", [ConversationResult("OPEN", "SELECT_SERVICE", {}), object()])
def test_missing_or_malformed_catalog_is_typed_and_does_not_mutate(preflight):
    orchestrator, calls = harness([preflight])
    with pytest.raises(TelegramConversationError) as raised:
        orchestrator.handle(update(data="v1:s:3:0", callback="callback-3"))
    assert raised.value.code == "CATALOG_UNAVAILABLE"
    assert len(calls) == 1


def test_text_input_preserves_screen_required_error_and_never_calls_conversation():
    orchestrator, calls = harness([])
    with pytest.raises(TelegramActionError) as raised:
        orchestrator.handle(update(text="some free text"))
    assert raised.value.code == "SCREEN_REQUIRED"
    assert len(calls) == 0


def test_mapper_error_identity_is_preserved_and_never_calls_conversation():
    orchestrator, calls = harness([])
    with pytest.raises(Exception) as raised:
        orchestrator.handle(update(text="/unsupported"))
    assert raised.value.code == "UNSUPPORTED_COMMAND"
    assert len(calls) == 0


def test_conversation_error_identity_is_preserved():
    conversation = ChannelConversation(EmptyGateway())
    error = TelegramActionError("SENTINEL")

    def handle(context, action):
        raise error

    conversation.handle = handle
    orchestrator = TelegramConversationOrchestrator(TelegramInteractionMapper(), TelegramActionResolver(), conversation)
    with pytest.raises(TelegramActionError) as raised:
        orchestrator.handle(update(text="/start"))
    assert raised.value is error
