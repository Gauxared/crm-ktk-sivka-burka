"""Pure, transport-neutral rendering of reviewed Telegram outcomes."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping
import unicodedata

from apps.api.sivka_burka_api.channel_conversation import ConversationResult
from .telegram_conversation import TelegramConversationOutcome


_MAX_PROTOCOL_INTEGER = 2_147_483_647
_MAX_MONEY_MINOR = 9_000_000_000_000
_MAX_MESSAGE = 4096
_MAX_BUTTON_LABEL = 128
_FIXED_CALLBACKS = frozenset({"v1:o", "v1:h", "v1:r", "v1:u"})
_SERVICE_CALLBACK = re.compile(r"^v1:s:([1-9][0-9]*):(0|[1-9][0-9]*)$")
_EXPERIENCE_LABELS = {
    "BEGINNER": "Новичок",
    "EXPERIENCED": "Опытный",
    "UNKNOWN": "Не указано",
}


class TelegramRenderError(ValueError):
    """Safe rendering failure that contains no caller-controlled values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("Invalid Telegram rendering")

    def __str__(self) -> str:
        return f"Telegram render error: {self.code}"

    def __repr__(self) -> str:
        return f"TelegramRenderError({self.code!r})"


def _fail(code: str) -> TelegramRenderError:
    return TelegramRenderError(code)


def _normalized_text(value: Any, *, limit: int, code: str = "INVALID_RESULT") -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > limit
    ):
        raise _fail(code)
    return value


def _catalog_title(value: Any) -> str:
    title = _normalized_text(value, limit=200, code="CATALOG_UNAVAILABLE")
    if unicodedata.normalize("NFC", title) != title:
        raise _fail("CATALOG_UNAVAILABLE")
    return title


def _validate_callback_data(value: Any) -> str:
    if not isinstance(value, str) or not value.isascii() or not 1 <= len(value.encode("utf-8")) <= 64:
        raise _fail("INVALID_RESULT")
    if value in _FIXED_CALLBACKS:
        return value
    match = _SERVICE_CALLBACK.fullmatch(value)
    if match is None:
        raise _fail("INVALID_RESULT")
    version, ordinal = (int(item) for item in match.groups())
    if version > _MAX_PROTOCOL_INTEGER or ordinal > _MAX_PROTOCOL_INTEGER:
        raise _fail("INVALID_RESULT")
    return value


@dataclass(frozen=True, slots=True)
class TelegramButton:
    label: str
    callback_data: str

    def __post_init__(self) -> None:
        _normalized_text(self.label, limit=_MAX_BUTTON_LABEL, code="MESSAGE_TOO_LONG")
        _validate_callback_data(self.callback_data)


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    text: str
    buttons: tuple[TelegramButton, ...] = ()

    def __post_init__(self) -> None:
        _normalized_text(self.text, limit=_MAX_MESSAGE, code="MESSAGE_TOO_LONG")
        if type(self.buttons) is not tuple or not all(type(button) is TelegramButton for button in self.buttons):
            raise _fail("INVALID_RESULT")


@dataclass(frozen=True, slots=True)
class TelegramRenderedOutcome:
    message: TelegramMessage
    callback_query_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.message) is not TelegramMessage:
            raise _fail("INVALID_OUTCOME")
        _validate_callback_id(self.callback_query_id)


def _validate_callback_id(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > 256
    ):
        raise _fail("INVALID_OUTCOME")
    return value


def _button(label: str, callback_data: str) -> TelegramButton:
    return TelegramButton(label, callback_data)


def _finish(
    text: str,
    buttons: tuple[TelegramButton, ...],
    callback_query_id: str | None,
) -> TelegramRenderedOutcome:
    return TelegramRenderedOutcome(TelegramMessage(text, buttons), callback_query_id)


def _outcome(value: Any) -> tuple[ConversationResult, str | None]:
    if type(value) is not TelegramConversationOutcome:
        raise _fail("INVALID_OUTCOME")
    if type(value.result) is not ConversationResult or not isinstance(value.result.data, Mapping):
        raise _fail("INVALID_RESULT")
    return value.result, _validate_callback_id(value.callback_query_id)


def _positive_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _fail("INVALID_RESULT")
    return value


def _option_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > 100
    ):
        raise _fail("CATALOG_UNAVAILABLE")
    return value


def _catalog(value: Any) -> tuple[int, tuple[tuple[str, Mapping[str, Any]], ...]]:
    if not isinstance(value, Mapping):
        raise _fail("CATALOG_UNAVAILABLE")
    version = value.get("catalog_version")
    services = value.get("services")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 1 <= version <= _MAX_PROTOCOL_INTEGER
        or not isinstance(services, list)
    ):
        raise _fail("CATALOG_UNAVAILABLE")

    choices: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for service in services:
        if not isinstance(service, Mapping) or not isinstance(service.get("options"), list):
            raise _fail("CATALOG_UNAVAILABLE")
        title = _catalog_title(service.get("title"))
        for option in service["options"]:
            if not isinstance(option, Mapping):
                raise _fail("CATALOG_UNAVAILABLE")
            active = option.get("active", True)
            if active is not True and active is not False:
                raise _fail("CATALOG_UNAVAILABLE")
            if active is False:
                continue

            identifier = _option_id(option.get("id"))
            if identifier in seen:
                raise _fail("CATALOG_UNAVAILABLE")
            seen.add(identifier)

            pricing = option.get("pricing_mode")
            duration = option.get("duration_minutes")
            price_minor = option.get("price_minor")
            if option.get("currency") != "RUB" or pricing not in {"FIXED_PER_PERSON", "NEGOTIATED"}:
                raise _fail("CATALOG_UNAVAILABLE")
            if pricing == "FIXED_PER_PERSON":
                if (
                    isinstance(duration, bool)
                    or not isinstance(duration, int)
                    or not 1 <= duration <= _MAX_PROTOCOL_INTEGER
                    or isinstance(price_minor, bool)
                    or not isinstance(price_minor, int)
                    or not 0 <= price_minor <= _MAX_MONEY_MINOR
                ):
                    raise _fail("CATALOG_UNAVAILABLE")
            elif duration is not None or price_minor is not None:
                raise _fail("CATALOG_UNAVAILABLE")
            choices.append((title, option))

    if not choices:
        raise _fail("CATALOG_UNAVAILABLE")
    return version, tuple(choices)


def _price(minor: int) -> str:
    rubles, kopecks = divmod(minor, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kopecks:02d} ₽ за человека"


def _service_text(title: str, option: Mapping[str, Any]) -> str:
    if option["pricing_mode"] == "NEGOTIATED":
        return f"{title} — По договорённости"
    return f"{title} — {option['duration_minutes']} мин., {_price(option['price_minor'])}"


def _enriched_data(
    result: ConversationResult,
) -> tuple[str, Mapping[str, Any], Mapping[str, Any]]:
    if set(result.data) != {"catalog", "answers"} or not isinstance(result.data.get("answers"), Mapping):
        raise _fail("INVALID_RESULT")
    _, choices = _catalog(result.data.get("catalog"))
    answers = result.data["answers"]
    identifier = _option_id(answers.get("service_option_id"))
    for title, option in choices:
        if option["id"] == identifier:
            return title, option, answers
    raise _fail("CATALOG_UNAVAILABLE")


def _requester(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) not in ({"name"}, {"name", "contact"}):
        raise _fail("INVALID_RESULT")
    _normalized_text(value.get("name"), limit=200)
    if "contact" in value:
        contact = value["contact"]
        if not isinstance(contact, Mapping) or set(contact) != {"kind", "value"}:
            raise _fail("INVALID_RESULT")
        if contact.get("kind") not in {"PHONE", "TELEGRAM", "VK"}:
            raise _fail("INVALID_RESULT")
        _normalized_text(contact.get("value"), limit=300)
    return value


def _review_answers(answers: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    required = {
        "service_option_id",
        "requester",
        "participants_count",
        "requested_time",
        "experience",
        "comment",
    }
    if set(answers) != required:
        raise _fail("INVALID_RESULT")
    requester = _requester(answers.get("requester"))
    participants = answers.get("participants_count")
    if isinstance(participants, bool) or not isinstance(participants, int) or not 1 <= participants <= 100:
        raise _fail("INVALID_RESULT")
    requested = answers.get("requested_time")
    if not isinstance(requested, Mapping) or set(requested) != {"date", "time_text"}:
        raise _fail("INVALID_RESULT")
    _normalized_text(requested.get("date"), limit=10)
    if requested.get("time_text") is not None:
        _normalized_text(requested["time_text"], limit=200)
    if answers.get("experience") not in _EXPERIENCE_LABELS:
        raise _fail("INVALID_RESULT")
    comment = answers.get("comment")
    if not isinstance(comment, str) or comment != comment.strip() or "\x00" in comment or len(comment) > 2000:
        raise _fail("INVALID_RESULT")
    return requester, requested


def _render_select(result: ConversationResult, callback_id: str | None) -> TelegramRenderedOutcome:
    if (
        set(result.data) != {"catalog"}
        or result.draft_version is not None
        or result.adapter_metadata is not None
        or result.replay is not False
    ):
        raise _fail("INVALID_RESULT")
    version, choices = _catalog(result.data["catalog"])
    lines = ["Выберите услугу:"]
    buttons: list[TelegramButton] = []
    for ordinal, (title, option) in enumerate(choices):
        lines.append(_service_text(title, option))
        buttons.append(_button(title, f"v1:s:{version}:{ordinal}"))
    buttons.append(_button("Правила и контакты", "v1:h"))
    return _finish("\n".join(lines), tuple(buttons), callback_id)


def _render_help(result: ConversationResult, callback_id: str | None) -> TelegramRenderedOutcome:
    allowed = {"contact_info", "location_link", "visit_rules"}
    if (
        not set(result.data).issubset(allowed)
        or not result.data
        or result.draft_version is not None
        or result.adapter_metadata is not None
        or result.replay is not False
    ):
        raise _fail("INVALID_RESULT")
    labels = (
        ("Контакты", "contact_info"),
        ("Место", "location_link"),
        ("Правила посещения", "visit_rules"),
    )
    lines = [
        f"{label}: {_normalized_text(result.data[key], limit=1000)}"
        for label, key in labels
        if key in result.data
    ]
    return _finish("\n".join(lines), (_button("Новая заявка", "v1:o"),), callback_id)


def _render_saved(result: ConversationResult, callback_id: str | None) -> TelegramRenderedOutcome:
    if result.adapter_metadata is not None or type(result.replay) is not bool:
        raise _fail("INVALID_RESULT")
    _positive_version(result.draft_version)
    title, option, answers = _enriched_data(result)
    common = (_button("Правила и контакты", "v1:h"), _button("Сбросить", "v1:r"))

    if result.screen == "REQUESTER":
        if set(answers) != {"service_option_id"}:
            raise _fail("INVALID_RESULT")
        return _finish("Как к вам обращаться? Напишите имя одним сообщением.", common, callback_id)

    if result.screen == "DETAILS":
        if set(answers) != {"service_option_id", "requester"}:
            raise _fail("INVALID_RESULT")
        _requester(answers["requester"])
        prompt = (
            f"{_service_text(title, option)}\n\n"
            "Отправьте одним сообщением ровно пять строк:\n"
            "Участники: <целое 1..100>\n"
            "Дата: <YYYY-MM-DD>\n"
            "Время: <текст 1..200 или ->\n"
            "Опыт: <Новичок|Опытный|Не указано>\n"
            "Комментарий: <текст 0..2000 или ->"
        )
        return _finish(prompt, common, callback_id)

    if result.screen != "REVIEW":
        raise _fail("UNSUPPORTED_SCREEN")
    requester, requested = _review_answers(answers)
    lines = [
        _service_text(title, option),
        f"Имя: {requester['name']}",
        f"Участники: {answers['participants_count']}",
        f"Дата: {requested['date']}",
    ]
    if requested["time_text"] is not None:
        lines.append(f"Время: {requested['time_text']}")
    lines.append(f"Опыт: {_EXPERIENCE_LABELS[answers['experience']]}")
    if answers["comment"]:
        lines.append(f"Комментарий: {answers['comment']}")
    buttons = (
        _button("Отправить", "v1:u"),
        _button("Сбросить", "v1:r"),
        _button("Правила и контакты", "v1:h"),
    )
    return _finish("\n".join(lines), buttons, callback_id)


class TelegramOutcomeRenderer:
    """Render one validated conversation outcome without transport side effects."""

    def render(self, outcome: TelegramConversationOutcome) -> TelegramRenderedOutcome:
        result, callback_id = _outcome(outcome)
        if result.kind == "OPEN" and result.screen == "SELECT_SERVICE":
            return _render_select(result, callback_id)
        if result.kind == "HELP" and result.screen == "HELP":
            return _render_help(result, callback_id)
        if result.kind == "SAVED" and result.screen in {"REQUESTER", "DETAILS", "REVIEW"}:
            return _render_saved(result, callback_id)
        if result.kind == "RESET" and result.screen == "START":
            if (
                result.data != {"reset": True}
                or result.adapter_metadata is not None
                or type(result.replay) is not bool
            ):
                raise _fail("INVALID_RESULT")
            _positive_version(result.draft_version)
            return _finish("Черновик заявки очищен.", (_button("Новая заявка", "v1:o"),), callback_id)
        if result.kind == "ACCEPTED_UNCONFIRMED" and result.screen == "ACCEPTED_UNCONFIRMED":
            if (
                result.data != {"semantic_code": "INQUIRY_ACCEPTED_UNCONFIRMED"}
                or result.draft_version is not None
                or (result.adapter_metadata is not None and not isinstance(result.adapter_metadata, Mapping))
                or type(result.replay) is not bool
            ):
                raise _fail("INVALID_RESULT")
            text = "Заявка принята и передана владельцу. Время, допуск и оплата ещё не подтверждены."
            return _finish(text, (_button("Новая заявка", "v1:o"),), callback_id)
        raise _fail("UNSUPPORTED_SCREEN")


render_telegram_outcome = TelegramOutcomeRenderer().render
TelegramRenderer = TelegramOutcomeRenderer
