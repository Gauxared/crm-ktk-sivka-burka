from dataclasses import dataclass

import pytest

from apps.api.sivka_burka_api.channel_conversation import ChannelAction, ChannelConversation
from apps.api.sivka_burka_api.channel_gateway import (
    ChannelContext, ChannelGateway, Draft, DraftSaved, GatewayError, InquiryReceipt, Platform, ResetResult,
)

OPTION = "option-active"
INACTIVE = "option-inactive"
CATALOG = {"catalog_version": 1, "services": [{"id": "service", "title": "Ride", "options": [{"id": OPTION}, {"id": INACTIVE, "active": False}]}], "contact_info": "owner", "location_link": "place", "visit_rules": "rules"}


@dataclass
class RecordingPort:
    draft: Draft | None = None
    catalog: dict | None = None
    read_error: GatewayError | None = None
    save_error: GatewayError | None = None
    submit_error: GatewayError | None = None
    reset_error: GatewayError | None = None

    def __post_init__(self):
        self.catalog = self.catalog or CATALOG
        self.mutations = []

    def read_catalog(self, context):
        if self.read_error:
            raise self.read_error
        return self.catalog

    def read_draft(self, context):
        return self.draft

    def save_draft(self, context, expected_version, answers, step, event_id):
        if self.save_error:
            raise self.save_error
        self.mutations.append(("save", expected_version, dict(answers), step, event_id))
        self.draft = Draft(dict(answers), step, expected_version + 1)
        return DraftSaved(self.draft.version, step)

    def submit_inquiry(self, context, fields, draft_version, submit_event_id):
        if self.submit_error:
            raise self.submit_error
        self.mutations.append(("submit", draft_version, dict(fields), submit_event_id))
        return InquiryReceipt("trusted-receipt", "trusted-inquiry")

    def reset_draft(self, context, expected_version, event_id):
        if self.reset_error:
            raise self.reset_error
        self.mutations.append(("reset", expected_version, event_id))
        self.draft = None
        return ResetResult(True, expected_version + 1)


def context(platform=Platform.TELEGRAM):
    return ChannelContext(platform, "integration", "sender", "conversation", "event-1")


def service(**kwargs):
    port = RecordingPort(**kwargs)
    return ChannelConversation(ChannelGateway(port)), port


def act(kind, **payload):
    return ChannelAction(kind, payload)


def test_replay_metadata_is_stable_but_public_serialization_is_private():
    flow, port = service()
    flow.handle(context(), act("SELECT_SERVICE", service_option_id=OPTION))
    flow.handle(context(), act("SET_REQUESTER", name="Ann"))
    flow.handle(context(), act("SET_DETAILS", participants_count=2, requested_time={"date": "2026-10-01", "time_text": "10:00"}, experience="UNKNOWN"))
    result = flow.handle(context(), act("SUBMIT"))
    assert result.adapter_metadata == {"receipt_id": "trusted-receipt", "inquiry_id": "trusted-inquiry"}
    assert result.as_dict() == {"kind": "ACCEPTED_UNCONFIRMED", "screen": "ACCEPTED_UNCONFIRMED", "data": {"semantic_code": "INQUIRY_ACCEPTED_UNCONFIRMED"}}
    assert not {"receipt_id", "inquiry_id"} & set(result.as_dict())
    assert len(port.mutations) == 4


@pytest.mark.parametrize("experience", ["BEGINNER", "EXPERIENCED", "UNKNOWN"])
@pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.VK])
def test_all_experience_variants_and_platforms_have_the_same_ungated_flow(experience, platform):
    flow, port = service()
    flow.handle(context(platform), act("SELECT_SERVICE", service_option_id=OPTION))
    flow.handle(context(platform), act("SET_REQUESTER", name="Ann", contact={"kind": "VK", "value": "custom"}))
    result = flow.handle(context(platform), act("SET_DETAILS", participants_count=1, requested_time={"date": "2026-10-01"}, experience=experience))
    assert result.screen == "REVIEW"
    assert "admission" not in port.mutations[-1][2]
    assert len(port.mutations) == 3


def test_omitted_contact_is_not_filled_from_trusted_sender():
    flow, port = service()
    flow.handle(context(), act("SELECT_SERVICE", service_option_id=OPTION))
    flow.handle(context(), act("SET_REQUESTER", name="Ann"))
    assert port.mutations[-1][2]["requester"] == {"name": "Ann"}
    assert "sender" not in repr(port.mutations[-1][2])


@pytest.mark.parametrize("kind, payload", [("SET_REQUESTER", {"name": "Ann"}), ("SET_DETAILS", {"participants_count": 1, "requested_time": {"date": "2026-10-01"}}), ("SUBMIT", {}), ("RESET", {})])
def test_out_of_order_mutations_are_typed_and_zero_mutation(kind, payload):
    flow, port = service()
    with pytest.raises(GatewayError) as exc:
        flow.handle(context(), act(kind, **payload))
    assert exc.value.code == "INVALID_TRANSITION"
    assert port.mutations == []


@pytest.mark.parametrize("kind, payload", [("SELECT_SERVICE", {"service_option_id": OPTION, "extra": True}), ("SET_REQUESTER", {"contact": None}), ("SET_DETAILS", {"participants_count": 1})])
def test_required_extra_and_malformed_actions_reject_before_mutation(kind, payload):
    flow, port = service()
    with pytest.raises(GatewayError) as exc:
        flow.handle(context(), ChannelAction(kind, payload))
    assert exc.value.code == "INVALID_ACTION"
    assert port.mutations == []


def test_reset_without_draft_and_inactive_initial_selection_do_not_mutate():
    flow, port = service()
    for action, code in [(act("RESET"), "INVALID_TRANSITION"), (act("SELECT_SERVICE", service_option_id=INACTIVE), "UNKNOWN_SERVICE_OPTION")]:
        with pytest.raises(GatewayError) as exc:
            flow.handle(context(), action)
        assert exc.value.code == code
        assert port.mutations == []


@pytest.mark.parametrize("draft", [Draft({"service_option_id": INACTIVE}, "REQUESTER", 1), Draft({"service_option_id": OPTION, "requester": {"name": "Ann"}}, "DETAILS", 1), Draft({"service_option_id": OPTION, "requester": {"name": "Ann"}, "participants_count": 1, "requested_time": {"date": "2026-10-01", "time_text": None}, "experience": "UNKNOWN", "comment": ""}, "REVIEW", 1)])
def test_resume_rejects_inactive_or_changed_catalog_before_mutation(draft):
    flow, port = service(draft=draft, catalog={"services": [{"options": [{"id": OPTION, "active": False}]}]})
    with pytest.raises(GatewayError) as exc:
        flow.handle(context(), act("OPEN"))
    assert exc.value.code == "UNKNOWN_SERVICE_OPTION"
    assert port.mutations == []


def test_submit_rejects_option_that_became_inactive_after_review():
    draft = Draft({"service_option_id": OPTION, "requester": {"name": "Ann"}, "participants_count": 1, "requested_time": {"date": "2026-10-01", "time_text": None}, "experience": "UNKNOWN", "comment": ""}, "REVIEW", 3)
    flow, port = service(draft=draft, catalog={"services": [{"options": [{"id": OPTION, "active": False}]}]})
    with pytest.raises(GatewayError) as exc:
        flow.handle(context(), act("SUBMIT"))
    assert exc.value.code == "UNKNOWN_SERVICE_OPTION"
    assert port.mutations == []


@pytest.mark.parametrize("operation, error_code", [("save", "VERSION_CONFLICT"), ("save", "IDEMPOTENCY_MISMATCH"), ("reset", "RESULT_EXPIRED"), ("submit", "CATALOG_UNAVAILABLE")])
def test_original_gateway_error_identity_is_preserved(operation, error_code):
    original = GatewayError(error_code)
    kwargs = {f"{operation}_error": original}
    flow, port = service(**kwargs)
    if operation == "save":
        action = act("SELECT_SERVICE", service_option_id=OPTION)
    elif operation == "reset":
        flow.handle(context(), act("SELECT_SERVICE", service_option_id=OPTION))
        action = act("RESET")
    else:
        flow.handle(context(), act("SELECT_SERVICE", service_option_id=OPTION))
        flow.handle(context(), act("SET_REQUESTER", name="Ann"))
        flow.handle(context(), act("SET_DETAILS", participants_count=1, requested_time={"date": "2026-10-01"}))
        action = act("SUBMIT")
    with pytest.raises(GatewayError) as exc:
        flow.handle(context(), action)
    assert exc.value is original


def test_open_help_reset_and_successful_mutations_have_exact_counts():
    flow, port = service()
    assert flow.handle(context(), act("OPEN")).kind == "OPEN"
    assert flow.handle(context(), act("HELP")).kind == "HELP"
    flow.handle(context(), act("SELECT_SERVICE", service_option_id=OPTION))
    flow.handle(context(), act("RESET"))
    assert [call[0] for call in port.mutations] == ["save", "reset"]
