"""Platform-neutral deterministic conversation service for trusted channel adapters."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from .channel_gateway import ChannelContext, ChannelGateway, Draft, GatewayError


STEPS = frozenset({"REQUESTER", "DETAILS", "REVIEW"})
BUSINESS_FIELDS = frozenset({"service_option_id", "requester", "participants_count", "requested_time", "experience", "comment"})
ACTION_KINDS = frozenset({"OPEN", "HELP", "SELECT_SERVICE", "SET_REQUESTER", "SET_DETAILS", "SUBMIT", "RESET"})


@dataclass(frozen=True, slots=True)
class ChannelAction:
    kind: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS or not isinstance(self.payload, Mapping):
            raise GatewayError("INVALID_ACTION")
        if not set(self.payload).issubset(_ACTION_FIELDS.get(self.kind, frozenset())):
            raise GatewayError("UNKNOWN_FIELD")


@dataclass(frozen=True, slots=True)
class ConversationResult:
    kind: str
    screen: str
    data: Mapping[str, Any]
    draft_version: int | None = None
    receipt_id: str | None = None
    inquiry_id: str | None = None
    replay: bool = False

    def as_dict(self) -> dict[str, Any]:
        result = {"kind": self.kind, "screen": self.screen, "data": dict(self.data)}
        if self.draft_version is not None:
            result["draft_version"] = self.draft_version
        if self.receipt_id is not None:
            result["receipt_id"] = self.receipt_id
        if self.inquiry_id is not None:
            result["inquiry_id"] = self.inquiry_id
        if self.replay:
            result["replay"] = True
        return result


_ACTION_FIELDS = {
    "OPEN": frozenset(), "HELP": frozenset(), "SELECT_SERVICE": frozenset({"service_option_id"}),
    "SET_REQUESTER": frozenset({"name", "contact"}),
    "SET_DETAILS": frozenset({"participants_count", "requested_time", "experience", "comment"}),
    "SUBMIT": frozenset(), "RESET": frozenset(),
}

ConversationAction = ChannelAction


def _text(value: Any, code: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or value.strip() != value:
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
            options[option["id"]] = option
    return options


def _validate_draft(draft: Draft | None) -> None:
    if draft is None:
        return
    if draft.step not in STEPS or isinstance(draft.version, bool) or not isinstance(draft.version, int) or draft.version < 1:
        raise GatewayError("CORRUPT_DRAFT")
    if not isinstance(draft.answers, Mapping) or not set(draft.answers).issubset(BUSINESS_FIELDS):
        raise GatewayError("CORRUPT_DRAFT")
    required = {"REQUESTER": {"service_option_id"}, "DETAILS": {"service_option_id", "requester"}, "REVIEW": BUSINESS_FIELDS}
    if not required[draft.step].issubset(draft.answers):
        raise GatewayError("CORRUPT_DRAFT")


class ChannelConversation:
    """Finite state machine over the already-authenticated ChannelGateway."""

    def __init__(self, gateway: ChannelGateway) -> None:
        # Duck typing keeps pure recording fakes at the application boundary;
        # production callers still inject the reviewed ChannelGateway.
        if not all(callable(getattr(gateway, name, None)) for name in ("read_catalog", "read_draft", "save_draft", "submit_inquiry", "reset_draft")):
            raise TypeError("gateway must implement ChannelGateway operations")
        self._gateway = gateway

    def handle(self, context: ChannelContext, action: ChannelAction) -> ConversationResult:
        if not isinstance(action, ChannelAction):
            raise GatewayError("INVALID_ACTION")
        catalog = self._gateway.read_catalog(context).catalog
        if action.kind == "HELP":
            return ConversationResult("HELP", "HELP", {key: catalog[key] for key in ("contact_info", "location_link", "visit_rules") if key in catalog})
        draft_result = self._gateway.read_draft(context)
        _validate_draft(draft_result.draft)
        draft = draft_result.draft
        if action.kind == "OPEN":
            return self._open(catalog, draft)
        if action.kind == "RESET":
            if draft is None:
                raise GatewayError("VERSION_CONFLICT")
            reset = self._gateway.reset_draft(context, expected_version=draft.version, event_id=context.event_id)
            return ConversationResult("RESET", "START", {"reset": True}, reset.version)
        if action.kind == "SUBMIT":
            if draft is None or draft.step != "REVIEW" or set(draft.answers) != BUSINESS_FIELDS:
                raise GatewayError("INVALID_TRANSITION")
            if draft.answers.get("service_option_id") not in _catalog_options(catalog):
                raise GatewayError("UNKNOWN_SERVICE_OPTION")
            receipt = self._gateway.submit_inquiry(context, fields=draft.answers, draft_version=draft.version, submit_event_id=context.event_id)
            return ConversationResult("ACCEPTED_UNCONFIRMED", "ACCEPTED", {"message": "Заявка принята. Владелец свяжется для согласования."}, receipt_id=receipt.receipt_id, inquiry_id=receipt.inquiry_id, replay=receipt.replay)
        return self._save(context, catalog, draft, action)

    def _open(self, catalog: Mapping[str, Any], draft: Draft | None) -> ConversationResult:
        if draft is None:
            return ConversationResult("OPEN", "SELECT_SERVICE", {"catalog": catalog})
        if draft.step not in STEPS:
            raise GatewayError("CORRUPT_DRAFT")
        return ConversationResult("OPEN", draft.step, {"catalog": catalog, "answers": dict(draft.answers)}, draft.version)

    def _save(self, context: ChannelContext, catalog: Mapping[str, Any], draft: Draft | None, action: ChannelAction) -> ConversationResult:
        expected = 0 if draft is None else draft.version
        answers = dict(draft.answers) if draft else {}
        kind = action.kind
        if kind == "SELECT_SERVICE":
            if draft is not None:
                raise GatewayError("INVALID_TRANSITION")
            option_id = _text(action.payload["service_option_id"], "INVALID_FORM", 100)
            if option_id not in _catalog_options(catalog):
                raise GatewayError("UNKNOWN_SERVICE_OPTION")
            answers = {"service_option_id": option_id}
            step = "REQUESTER"
        elif kind == "SET_REQUESTER":
            if draft is None or draft.step != "REQUESTER":
                raise GatewayError("INVALID_TRANSITION")
            name = _text(action.payload["name"], "INVALID_FORM", 200)
            contact = action.payload.get("contact")
            if contact is None:
                contact = {"kind": context.platform.value, "value": context.external_sender_id}
            if not isinstance(contact, Mapping) or set(contact) != {"kind", "value"}:
                raise GatewayError("INVALID_FORM")
            contact_kind = _text(contact["kind"], "INVALID_FORM", 20).upper()
            if contact_kind not in {"PHONE", "TELEGRAM", "VK"}:
                raise GatewayError("INVALID_FORM")
            answers["requester"] = {"name": name, "contact": {"kind": contact_kind, "value": _text(contact["value"], "INVALID_FORM", 300)}}
            step = "DETAILS"
        elif kind == "SET_DETAILS":
            if draft is None or draft.step != "DETAILS":
                raise GatewayError("INVALID_TRANSITION")
            participants = action.payload.get("participants_count")
            if isinstance(participants, bool) or not isinstance(participants, int) or not 1 <= participants <= 100:
                raise GatewayError("INVALID_FORM")
            requested = action.payload.get("requested_time")
            if not isinstance(requested, Mapping) or set(requested) - {"date", "time_text"} or not isinstance(requested.get("date"), str):
                raise GatewayError("INVALID_FORM")
            try:
                normalized_date = date.fromisoformat(requested["date"]).isoformat()
            except ValueError:
                raise GatewayError("INVALID_FORM") from None
            if "time_text" in requested and requested["time_text"] is not None:
                _text(requested["time_text"], "INVALID_FORM", 200)
            experience = action.payload.get("experience", "UNKNOWN")
            comment = action.payload.get("comment", "")
            if experience not in {"BEGINNER", "EXPERIENCED", "UNKNOWN"} or not isinstance(comment, str) or len(comment) > 2000:
                raise GatewayError("INVALID_FORM")
            answers.update({"participants_count": participants, "requested_time": {"date": normalized_date, "time_text": requested.get("time_text")}, "experience": experience, "comment": comment.strip()})
            step = "REVIEW"
        else:
            raise GatewayError("INVALID_ACTION")
        saved = self._gateway.save_draft(context, expected_version=expected, answers=answers, step=step, event_id=context.event_id)
        return ConversationResult("SAVED", step, {"answers": answers}, saved.version)
