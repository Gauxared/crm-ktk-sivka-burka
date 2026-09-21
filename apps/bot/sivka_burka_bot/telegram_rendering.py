"""Pure, transport-neutral rendering of reviewed Telegram outcomes."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping
import unicodedata

from apps.api.sivka_burka_api.channel_conversation import ConversationResult
from .telegram_conversation import TelegramConversationOutcome

_MAX_INTEGER = 2_147_483_647
_MAX_MESSAGE = 4096
_MAX_LABEL = 128
_ALLOWED_DATA = frozenset({"catalog", "contact_info", "location_link", "visit_rules", "reset", "semantic_code"})


class TelegramRenderError(ValueError):
    """Safe renderer error; its representation contains no caller data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Invalid Telegram rendering")

    def __str__(self) -> str:
        return f"Telegram render error: {self.code}"

    def __repr__(self) -> str:
        return f"TelegramRenderError({self.code!r})"


@dataclass(frozen=True, slots=True)
class TelegramButton:
    label: str
    callback_data: str

    def __post_init__(self) -> None:
        _validate_button(self.label, self.callback_data)


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    text: str
    buttons: tuple[TelegramButton, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not 1 <= len(self.text) <= _MAX_MESSAGE or self.text != self.text.strip() or "\x00" in self.text:
            raise _fail("MESSAGE_TOO_LONG")
        if type(self.buttons) is not tuple or not all(type(button) is TelegramButton for button in self.buttons):
            raise _fail("INVALID_RESULT")


@dataclass(frozen=True, slots=True)
class TelegramRenderedOutcome:
    message: TelegramMessage
    callback_query_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.message) is not TelegramMessage:
            raise _fail("INVALID_RESULT")
        if self.callback_query_id is not None and (not isinstance(self.callback_query_id, str) or not self.callback_query_id or self.callback_query_id != self.callback_query_id.strip() or len(self.callback_query_id) > 256):
            raise _fail("INVALID_OUTCOME")


def _fail(code: str) -> TelegramRenderError:
    return TelegramRenderError(code)


def _text(value: Any, *, limit: int = _MAX_MESSAGE) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > limit:
        raise _fail("INVALID_RESULT")
    return value


def _normalized_title(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > 200 or unicodedata.normalize("NFC", value) != value:
        raise _fail("CATALOG_UNAVAILABLE")
    return value


def _validate_button(label: str, callback_data: str) -> None:
    if not isinstance(label, str) or not 1 <= len(label) <= _MAX_LABEL or label != label.strip() or "\x00" in label:
        raise _fail("MESSAGE_TOO_LONG")
    if not isinstance(callback_data, str) or not 1 <= len(callback_data.encode("utf-8")) <= 64:
        raise _fail("MESSAGE_TOO_LONG")
    if not callback_data.isascii() or not (
        callback_data in {"v1:o", "v1:h", "v1:r"}
        or re.fullmatch(r"v1:s:([1-9][0-9]*):(0|[1-9][0-9]*)", callback_data)
    ):
        raise _fail("INVALID_RESULT")


def _button(label: str, callback_data: str) -> TelegramButton:
    _validate_button(label, callback_data)
    return TelegramButton(label, callback_data)


def _finish(text: str, buttons: tuple[TelegramButton, ...], callback_id: str | None) -> TelegramRenderedOutcome:
    if not isinstance(text, str) or not 1 <= len(text) <= _MAX_MESSAGE:
        raise _fail("MESSAGE_TOO_LONG")
    return TelegramRenderedOutcome(TelegramMessage(text, tuple(buttons)), callback_id)


def _outcome(value: Any) -> tuple[ConversationResult, str | None]:
    if type(value) is not TelegramConversationOutcome:
        raise _fail("INVALID_OUTCOME")
    result = value.result
    if type(result) is not ConversationResult:
        raise _fail("INVALID_RESULT")
    callback_id = value.callback_query_id
    if callback_id is not None and (not isinstance(callback_id, str) or not callback_id or callback_id != callback_id.strip() or len(callback_id) > 256):
        raise _fail("INVALID_OUTCOME")
    if not isinstance(result.data, Mapping):
        raise _fail("INVALID_RESULT")
    return result, callback_id


def _catalog(data: Mapping[str, Any]) -> tuple[int, list[tuple[str, Mapping[str, Any]]]]:
    if set(data) != {"catalog"} or not isinstance(data.get("catalog"), Mapping):
        raise _fail("CATALOG_UNAVAILABLE")
    catalog = data["catalog"]
    version = catalog.get("catalog_version")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= _MAX_INTEGER:
        raise _fail("CATALOG_UNAVAILABLE")
    services = catalog.get("services")
    if not isinstance(services, list) or not services:
        raise _fail("CATALOG_UNAVAILABLE")
    flattened: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for service in services:
        if not isinstance(service, Mapping) or not isinstance(service.get("options"), list):
            raise _fail("CATALOG_UNAVAILABLE")
        service_active = service.get("active", True)
        if service_active is not True and service_active is not False:
            raise _fail("CATALOG_UNAVAILABLE")
        title = _normalized_title(service.get("title"))
        if not service_active:
            continue
        for option in service["options"]:
            if not isinstance(option, Mapping):
                raise _fail("CATALOG_UNAVAILABLE")
            active = option.get("active", True)
            if active is not True and active is not False:
                raise _fail("CATALOG_UNAVAILABLE")
            option_id = option.get("id")
            if not isinstance(option_id, str) or not option_id or option_id != option_id.strip() or option_id in seen:
                raise _fail("CATALOG_UNAVAILABLE")
            seen.add(option_id)
            if not active:
                continue
            duration = option.get("duration_minutes")
            pricing = option.get("pricing_mode")
            if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= _MAX_INTEGER:
                raise _fail("CATALOG_UNAVAILABLE")
            if pricing not in {"FIXED_PER_PERSON", "NEGOTIATED"}:
                raise _fail("CATALOG_UNAVAILABLE")
            if pricing == "FIXED_PER_PERSON":
                price = option.get("price_minor")
                if isinstance(price, bool) or not isinstance(price, int) or price < 0 or price > _MAX_INTEGER or option.get("currency") != "RUB":
                    raise _fail("CATALOG_UNAVAILABLE")
            else:
                if "price_minor" in option and option["price_minor"] is not None:
                    raise _fail("CATALOG_UNAVAILABLE")
                if "currency" in option and option["currency"] is not None:
                    raise _fail("CATALOG_UNAVAILABLE")
            flattened.append((title, option))
    if not flattened:
        raise _fail("CATALOG_UNAVAILABLE")
    return version, flattened


def _price(minor: int) -> str:
    rubles, kopecks = divmod(minor, 100)
    whole = f"{rubles:,}".replace(",", " ")
    return f"{whole},{kopecks:02d} ₽ за человека"


def _render_select(result: ConversationResult, callback_id: str | None) -> TelegramRenderedOutcome:
    version, options = _catalog(result.data)
    buttons: list[TelegramButton] = []
    lines = ["Выберите услугу:"]
    for ordinal, (title, option) in enumerate(options):
        pricing = option["pricing_mode"]
        cost = _price(option["price_minor"]) if pricing == "FIXED_PER_PERSON" else "По договорённости"
        lines.append(f"{title} — {option['duration_minutes']} мин., {cost}")
        buttons.append(_button(title, f"v1:s:{version}:{ordinal}"))
    buttons.append(_button("Правила и контакты", "v1:h"))
    return _finish("\n".join(lines), tuple(buttons), callback_id)


def _render_help(result: ConversationResult, callback_id: str | None) -> TelegramRenderedOutcome:
    if result.kind != "HELP" or result.screen != "HELP" or not set(result.data).issubset({"contact_info", "location_link", "visit_rules"}):
        raise _fail("UNSUPPORTED_SCREEN")
    labels = (("Контакты", "contact_info"), ("Место", "location_link"), ("Правила посещения", "visit_rules"))
    lines = [f"{label}: {value}" for label, key in labels if key in result.data and (value := _text(result.data[key], limit=1000))]
    if not lines:
        raise _fail("MESSAGE_TOO_LONG")
    return _finish("\n".join(lines), (_button("Новая заявка", "v1:o"),), callback_id)


class TelegramOutcomeRenderer:
    def render(self, outcome: TelegramConversationOutcome) -> TelegramRenderedOutcome:
        result, callback_id = _outcome(outcome)
        if result.kind == "OPEN" and result.screen == "SELECT_SERVICE":
            return _render_select(result, callback_id)
        if result.kind == "HELP" and result.screen == "HELP":
            if result.adapter_metadata is not None or result.draft_version is not None or result.replay:
                raise _fail("INVALID_RESULT")
            return _render_help(result, callback_id)
        if result.kind == "SAVED" and result.screen == "REQUESTER":
            if result.adapter_metadata is not None or result.draft_version is not None or result.replay:
                raise _fail("INVALID_RESULT")
            if set(result.data) != {"answers"} or not isinstance(result.data.get("answers"), Mapping):
                raise _fail("INVALID_RESULT")
            return _finish("Как к вам обращаться? Напишите имя одним сообщением.", (_button("Правила и контакты", "v1:h"), _button("Сбросить", "v1:r")), callback_id)
        if result.kind == "RESET" and result.screen == "START" and set(result.data) == {"reset"} and result.data["reset"] is True:
            if result.adapter_metadata is not None or result.draft_version is not None or result.replay:
                raise _fail("INVALID_RESULT")
            return _finish("Черновик заявки очищен.", (_button("Новая заявка", "v1:o"),), callback_id)
        if result.kind == "ACCEPTED_UNCONFIRMED" and result.screen == "ACCEPTED_UNCONFIRMED" and set(result.data) == {"semantic_code"} and result.data["semantic_code"] == "INQUIRY_ACCEPTED_UNCONFIRMED":
            return _finish("Заявка принята и передана владельцу. Время, допуск и оплата ещё не подтверждены.", (_button("Новая заявка", "v1:o"),), callback_id)
        raise _fail("UNSUPPORTED_SCREEN")


render_telegram_outcome = TelegramOutcomeRenderer().render
TelegramRenderer = TelegramOutcomeRenderer
