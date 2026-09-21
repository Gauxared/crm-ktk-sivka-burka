"""Pure resolution of Telegram text against the current conversation screen."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any, Mapping

from apps.api.sivka_burka_api.channel_conversation import ChannelAction, ConversationResult
from apps.api.sivka_burka_api.channel_gateway import ChannelContext, GatewayError, Platform, validate_context
from .telegram_interactions import TelegramIntent, TelegramIntentKind


class TelegramScreenActionError(ValueError):
    """Safe screen resolver error that never exposes caller-controlled values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Invalid Telegram screen action")

    def __str__(self) -> str:
        return f"Telegram screen action error: {self.code}"

    def __repr__(self) -> str:
        return f"TelegramScreenActionError({self.code!r})"


def _error(code: str) -> TelegramScreenActionError:
    return TelegramScreenActionError(code)


def _context(value: Any) -> ChannelContext:
    if type(value) is not ChannelContext:
        raise _error("INVALID_INTENT")
    try:
        checked = validate_context(value)
    except GatewayError:
        raise _error("INVALID_INTENT") from None
    if checked.platform is not Platform.TELEGRAM:
        raise _error("INVALID_INTENT")
    return checked


def _intent(value: Any) -> TelegramIntent:
    if type(value) is not TelegramIntent:
        raise _error("INVALID_INTENT")
    _context(value.context)
    if value.kind is not TelegramIntentKind.TEXT_INPUT:
        raise _error("INVALID_INTENT")
    if type(value.payload) is not MappingProxyType or set(value.payload) != {"text"}:
        raise _error("INVALID_INTENT")
    if value.callback_query_id is not None:
        raise _error("INVALID_INTENT")
    text = value.payload.get("text")
    if not isinstance(text, str) or not text or text.strip() != text or len(text) > 4096:
        raise _error("INVALID_INTENT")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or len(value) > 200 or "\x00" in value:
        raise _error("INVALID_REQUESTER_INPUT")
    return value


def _details_text(value: Any) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or "\x00" in value:
        raise _error("INVALID_DETAILS_INPUT")
    lines = value.split("\n")
    labels = ("Участники", "Дата", "Время", "Опыт", "Комментарий")
    if len(lines) != len(labels):
        raise _error("INVALID_DETAILS_INPUT")
    values: list[str] = []
    for line, label in zip(lines, labels):
        prefix = f"{label}: "
        if not line.startswith(prefix):
            raise _error("INVALID_DETAILS_INPUT")
        values.append(line[len(prefix):])
    participants, requested_date, time_text, experience, comment = values
    if not participants.isascii() or not participants.isdecimal() or str(int(participants)) != participants or not 1 <= int(participants) <= 100:
        raise _error("INVALID_DETAILS_INPUT")
    try:
        normalized_date = date.fromisoformat(requested_date).isoformat()
    except (TypeError, ValueError):
        raise _error("INVALID_DETAILS_INPUT") from None
    if requested_date != normalized_date:
        raise _error("INVALID_DETAILS_INPUT")
    if time_text == "-":
        normalized_time = None
    elif not time_text or time_text != time_text.strip() or "\x00" in time_text or len(time_text) > 200:
        raise _error("INVALID_DETAILS_INPUT")
    else:
        normalized_time = time_text
    normalized_experience = experience.casefold()
    experiences = {"новичок": "BEGINNER", "опытный": "EXPERIENCED", "не указано": "UNKNOWN"}
    if experience != experience.strip() or normalized_experience not in experiences:
        raise _error("INVALID_DETAILS_INPUT")
    if comment == "-":
        normalized_comment = ""
    elif comment != comment.strip() or "\x00" in comment or len(comment) > 2000:
        raise _error("INVALID_DETAILS_INPUT")
    else:
        normalized_comment = comment
    return {"participants_count": int(participants), "requested_time": {"date": normalized_date, "time_text": normalized_time}, "experience": experiences[normalized_experience], "comment": normalized_comment}


def _requester_preflight(value: Any) -> None:
    if type(value) is not ConversationResult:
        raise _error("INVALID_SCREEN_RESULT")
    if value.kind != "OPEN" or value.screen != "REQUESTER" or value.adapter_metadata is not None or value.replay is not False:
        raise _error("SCREEN_INPUT_UNSUPPORTED")
    if isinstance(value.draft_version, bool) or not isinstance(value.draft_version, int) or value.draft_version < 1:
        raise _error("INVALID_SCREEN_RESULT")
    if type(value.data) is not MappingProxyType and not isinstance(value.data, Mapping):
        raise _error("INVALID_SCREEN_RESULT")
    if set(value.data) != {"catalog", "answers"}:
        raise _error("INVALID_SCREEN_RESULT")
    if not isinstance(value.data.get("catalog"), Mapping):
        raise _error("INVALID_SCREEN_RESULT")
    answers = value.data.get("answers")
    if not isinstance(answers, Mapping) or set(answers) != {"service_option_id"}:
        raise _error("INVALID_SCREEN_RESULT")
    option_id = answers.get("service_option_id")
    if not isinstance(option_id, str) or not option_id or option_id.strip() != option_id or len(option_id) > 100 or "\x00" in option_id:
        raise _error("INVALID_SCREEN_RESULT")


def _details_preflight(value: Any) -> None:
    if type(value) is not ConversationResult or value.kind != "OPEN" or value.screen != "DETAILS" or value.adapter_metadata is not None or value.replay is not False:
        raise _error("SCREEN_INPUT_UNSUPPORTED")
    if isinstance(value.draft_version, bool) or not isinstance(value.draft_version, int) or value.draft_version < 1:
        raise _error("INVALID_SCREEN_RESULT")
    if not isinstance(value.data, Mapping) or set(value.data) != {"catalog", "answers"} or not isinstance(value.data.get("catalog"), Mapping):
        raise _error("INVALID_SCREEN_RESULT")
    answers = value.data.get("answers")
    if not isinstance(answers, Mapping) or set(answers) not in ({"service_option_id", "requester"}, {"service_option_id", "requester"}):
        raise _error("INVALID_SCREEN_RESULT")


@dataclass(frozen=True, slots=True)
class TelegramScreenActionResolver:
    """Resolve one mapper-produced text input for the REQUESTER screen only."""

    def resolve(self, intent: TelegramIntent, preflight: ConversationResult) -> ChannelAction:
        intent = _intent(intent)
        if type(preflight) is not ConversationResult:
            raise _error("INVALID_SCREEN_RESULT")
        if preflight.screen not in {"REQUESTER", "DETAILS"}:
            raise _error("SCREEN_INPUT_UNSUPPORTED")
        if preflight.screen == "REQUESTER":
            _requester_preflight(preflight)
            return ChannelAction("SET_REQUESTER", MappingProxyType({"name": _text(intent.payload["text"])}))
        _details_preflight(preflight)
        return ChannelAction("SET_DETAILS", MappingProxyType(_details_text(intent.payload["text"])))


resolve_telegram_screen_action = TelegramScreenActionResolver().resolve
