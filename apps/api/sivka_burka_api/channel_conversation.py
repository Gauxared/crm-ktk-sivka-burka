"""Platform-neutral deterministic conversation service for trusted adapters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from .channel_gateway import ChannelContext, ChannelGateway, Draft, GatewayError, validate_context

STEPS = frozenset({"REQUESTER", "DETAILS", "REVIEW"})
BUSINESS_FIELDS = frozenset({"service_option_id", "requester", "participants_count", "requested_time", "experience", "comment"})
ACTION_KINDS = frozenset({"OPEN", "HELP", "SELECT_SERVICE", "SET_REQUESTER", "SET_DETAILS", "SUBMIT", "RESET"})
_ACTION_FIELDS = {"OPEN": frozenset(), "HELP": frozenset(), "SELECT_SERVICE": frozenset({"service_option_id"}), "SET_REQUESTER": frozenset({"name", "contact"}), "SET_DETAILS": frozenset({"participants_count", "requested_time", "experience", "comment"}), "SUBMIT": frozenset(), "RESET": frozenset()}
_REQUIRED_ACTION_FIELDS = {"SELECT_SERVICE": frozenset({"service_option_id"}), "SET_REQUESTER": frozenset({"name"}), "SET_DETAILS": frozenset({"participants_count", "requested_time"})}

@dataclass(frozen=True, slots=True)
class ChannelAction:
    kind: str
    payload: Mapping[str, Any]
    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in ACTION_KINDS or not isinstance(self.payload, Mapping):
            raise GatewayError("INVALID_ACTION")
        fields = set(self.payload)
        if not fields.issubset(_ACTION_FIELDS[self.kind]) or not _REQUIRED_ACTION_FIELDS.get(self.kind, frozenset()).issubset(fields):
            raise GatewayError("INVALID_ACTION")

@dataclass(frozen=True, slots=True)
class ConversationResult:
    kind: str
    screen: str
    data: Mapping[str, Any]
    draft_version: int | None = None
    adapter_metadata: Mapping[str, Any] | None = None
    replay: bool = False
    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind, "screen": self.screen, "data": dict(self.data)}
        if self.draft_version is not None:
            result["draft_version"] = self.draft_version
        if self.replay:
            result["replay"] = True
        return result

ConversationAction = ChannelAction

def _text(value: Any, code: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or value.strip() != value or "\x00" in value:
        raise GatewayError(code)
    return value

def _catalog_options(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    services = catalog.get("services")
    if not isinstance(services, list):
        raise GatewayError("CATALOG_UNAVAILABLE")
    options: dict[str, Mapping[str, Any]] = {}
    for service in services:
        if not isinstance(service, Mapping) or not isinstance(service.get("options"), list):
            raise GatewayError("CATALOG_UNAVAILABLE")
        for option in service["options"]:
            if not isinstance(option, Mapping) or not isinstance(option.get("id"), str):
                raise GatewayError("CATALOG_UNAVAILABLE")
            if option.get("active", True) is True:
                options[option["id"]] = option
    return options

def _mapping(value: Any, allowed: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != allowed:
        raise GatewayError(code)
    return value

def _validate_contact(value: Any) -> dict[str, str]:
    contact = _mapping(value, frozenset({"kind", "value"}), "CORRUPT_DRAFT")
    kind = _text(contact["kind"], "CORRUPT_DRAFT", 20).upper()
    if kind not in {"PHONE", "TELEGRAM", "VK"}:
        raise GatewayError("CORRUPT_DRAFT")
    return {"kind": kind, "value": _text(contact["value"], "CORRUPT_DRAFT", 300)}

def _validate_answers(draft: Draft, options: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if draft.step not in STEPS or isinstance(draft.version, bool) or not isinstance(draft.version, int) or draft.version < 1 or not isinstance(draft.answers, Mapping):
        raise GatewayError("CORRUPT_DRAFT")
    required = {"REQUESTER": {"service_option_id"}, "DETAILS": {"service_option_id", "requester"}, "REVIEW": set(BUSINESS_FIELDS)}[draft.step]
    if set(draft.answers) != required:
        raise GatewayError("CORRUPT_DRAFT")
    answers = dict(draft.answers)
    option_id = _text(answers["service_option_id"], "CORRUPT_DRAFT", 100)
    if option_id not in options:
        raise GatewayError("UNKNOWN_SERVICE_OPTION")
    answers["service_option_id"] = option_id
    if draft.step in {"DETAILS", "REVIEW"}:
        requester = answers["requester"]
        allowed = frozenset({"name", "contact"}) if isinstance(requester, Mapping) and "contact" in requester else frozenset({"name"})
        requester = _mapping(requester, allowed, "CORRUPT_DRAFT")
        normalized = {"name": _text(requester["name"], "CORRUPT_DRAFT", 200)}
        if "contact" in requester:
            normalized["contact"] = _validate_contact(requester["contact"])
        answers["requester"] = normalized
    if draft.step == "REVIEW":
        participants = answers["participants_count"]
        if isinstance(participants, bool) or not isinstance(participants, int) or not 1 <= participants <= 100:
            raise GatewayError("CORRUPT_DRAFT")
        requested = _mapping(answers["requested_time"], frozenset({"date", "time_text"}), "CORRUPT_DRAFT")
        if not isinstance(requested["date"], str):
            raise GatewayError("CORRUPT_DRAFT")
        try:
            normalized_date = date.fromisoformat(requested["date"]).isoformat()
        except (TypeError, ValueError):
            raise GatewayError("CORRUPT_DRAFT") from None
        if requested["time_text"] is not None:
            _text(requested["time_text"], "CORRUPT_DRAFT", 200)
        if answers["experience"] not in {"BEGINNER", "EXPERIENCED", "UNKNOWN"} or not isinstance(answers["comment"], str) or len(answers["comment"]) > 2000 or answers["comment"].strip() != answers["comment"]:
            raise GatewayError("CORRUPT_DRAFT")
        answers["participants_count"] = participants
        answers["requested_time"] = {"date": normalized_date, "time_text": requested["time_text"]}
    return answers

class ChannelConversation:
    """Finite state machine over the already-authenticated ChannelGateway."""
    def __init__(self, gateway: ChannelGateway) -> None:
        if not all(callable(getattr(gateway, name, None)) for name in ("read_catalog", "read_draft", "save_draft", "submit_inquiry", "reset_draft")):
            raise GatewayError("INVALID_GATEWAY")
        self._gateway = gateway

    def handle(self, context: ChannelContext, action: ChannelAction) -> ConversationResult:
        validate_context(context)
        if not isinstance(action, ChannelAction):
            raise GatewayError("INVALID_ACTION")
        catalog_result = self._gateway.read_catalog(context)
        catalog = getattr(catalog_result, "catalog", None)
        if not isinstance(catalog, Mapping):
            raise GatewayError("CATALOG_UNAVAILABLE")
        options = _catalog_options(catalog)
        if action.kind == "HELP":
            return ConversationResult("HELP", "HELP", {key: catalog[key] for key in ("contact_info", "location_link", "visit_rules") if key in catalog})
        draft_result = self._gateway.read_draft(context)
        draft = getattr(draft_result, "draft", None)
        answers = None if draft is None else _validate_answers(draft, options)
        if action.kind == "OPEN":
            return self._open(catalog, draft, answers)
        if action.kind == "RESET":
            if draft is None:
                raise GatewayError("INVALID_TRANSITION")
            reset = self._gateway.reset_draft(context, expected_version=draft.version, event_id=context.event_id)
            return ConversationResult("RESET", "START", {"reset": True}, reset.version)
        if action.kind == "SUBMIT":
            if draft is None or draft.step != "REVIEW":
                raise GatewayError("INVALID_TRANSITION")
            receipt = self._gateway.submit_inquiry(context, fields=answers, draft_version=draft.version, submit_event_id=context.event_id)
            return ConversationResult("ACCEPTED_UNCONFIRMED", "ACCEPTED_UNCONFIRMED", {"semantic_code": "INQUIRY_ACCEPTED_UNCONFIRMED"}, adapter_metadata={"receipt_id": receipt.receipt_id, "inquiry_id": receipt.inquiry_id}, replay=receipt.replay)
        return self._save(context, options, draft, answers, action)

    def _open(self, catalog: Mapping[str, Any], draft: Draft | None, answers: dict[str, Any] | None) -> ConversationResult:
        if draft is None:
            return ConversationResult("OPEN", "SELECT_SERVICE", {"catalog": catalog})
        return ConversationResult("OPEN", draft.step, {"catalog": catalog, "answers": answers}, draft.version)

    def _save(self, context: ChannelContext, options: Mapping[str, Mapping[str, Any]], draft: Draft | None, current: dict[str, Any] | None, action: ChannelAction) -> ConversationResult:
        expected = 0 if draft is None else draft.version
        answers = dict(current or {})
        if action.kind == "SELECT_SERVICE":
            if draft is not None:
                raise GatewayError("INVALID_TRANSITION")
            option_id = _text(action.payload["service_option_id"], "INVALID_FORM", 100)
            if option_id not in options:
                raise GatewayError("UNKNOWN_SERVICE_OPTION")
            answers, step = {"service_option_id": option_id}, "REQUESTER"
        elif action.kind == "SET_REQUESTER":
            if draft is None or draft.step != "REQUESTER":
                raise GatewayError("INVALID_TRANSITION")
            requester: dict[str, Any] = {"name": _text(action.payload["name"], "INVALID_FORM", 200)}
            if "contact" in action.payload:
                contact = _mapping(action.payload["contact"], frozenset({"kind", "value"}), "INVALID_FORM")
                kind = _text(contact["kind"], "INVALID_FORM", 20).upper()
                if kind not in {"PHONE", "TELEGRAM", "VK"}:
                    raise GatewayError("INVALID_FORM")
                requester["contact"] = {"kind": kind, "value": _text(contact["value"], "INVALID_FORM", 300)}
            answers["requester"], step = requester, "DETAILS"
        elif action.kind == "SET_DETAILS":
            if draft is None or draft.step != "DETAILS":
                raise GatewayError("INVALID_TRANSITION")
            participants = action.payload["participants_count"]
            if isinstance(participants, bool) or not isinstance(participants, int) or not 1 <= participants <= 100:
                raise GatewayError("INVALID_FORM")
            requested_value = action.payload["requested_time"]
            allowed = frozenset({"date", "time_text"}) if isinstance(requested_value, Mapping) and "time_text" in requested_value else frozenset({"date"})
            requested = _mapping(requested_value, allowed, "INVALID_FORM")
            try:
                normalized_date = date.fromisoformat(requested["date"]).isoformat()
            except (TypeError, ValueError):
                raise GatewayError("INVALID_FORM") from None
            time_text = requested.get("time_text")
            if time_text is not None:
                _text(time_text, "INVALID_FORM", 200)
            experience, comment = action.payload.get("experience", "UNKNOWN"), action.payload.get("comment", "")
            if experience not in {"BEGINNER", "EXPERIENCED", "UNKNOWN"} or not isinstance(comment, str) or len(comment) > 2000 or comment.strip() != comment:
                raise GatewayError("INVALID_FORM")
            answers.update({"participants_count": participants, "requested_time": {"date": normalized_date, "time_text": time_text}, "experience": experience, "comment": comment})
            step = "REVIEW"
        else:
            raise GatewayError("INVALID_ACTION")
        saved = self._gateway.save_draft(context, expected_version=expected, answers=answers, step=step, event_id=context.event_id)
        return ConversationResult("SAVED", step, {"answers": answers}, saved.version)
