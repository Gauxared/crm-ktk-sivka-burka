from dataclasses import dataclass

import pytest

from apps.api.sivka_burka_api.channel_conversation import ChannelAction, ChannelConversation
from apps.api.sivka_burka_api.channel_gateway import (
    ChannelContext, ChannelGateway, Draft, DraftSaved, InquiryReceipt, Platform, ResetResult,
)


OPTION = "option-active"
INACTIVE = "option-inactive"
CATALOG = {"catalog_version": 1, "services": [{"id": "service", "title": "Ride", "options": [{"id": OPTION}, {"id": INACTIVE, "active": False}]}], "contact_info": "owner", "location_link": "place", "visit_rules": "rules"}


@dataclass
class RecordingPort:
    draft: Draft | None = None

    def __post_init__(self): self.mutations = []
    def read_catalog(self, context): return CATALOG
    def read_draft(self, context): return self.draft
    def save_draft(self, context, expected_version, answers, step, event_id):
        self.mutations.append(("save", expected_version, dict(answers), step, event_id))
        self.draft = Draft(dict(answers), step, expected_version + 1)
        return DraftSaved(self.draft.version, step)
    def submit_inquiry(self, context, fields, draft_version, submit_event_id):
        self.mutations.append(("submit", draft_version, dict(fields), submit_event_id))
        return InquiryReceipt("trusted-receipt", "trusted-inquiry")
    def reset_draft(self, context, expected_version, event_id):
        self.mutations.append(("reset", expected_version, event_id))
        self.draft = None
        return ResetResult(True, expected_version + 1)


def context(platform=Platform.TELEGRAM):
    return ChannelContext(platform, "integration", "sender", "conversation", "event-1")


def service():
    port = RecordingPort()
    return ChannelConversation(ChannelGateway(port)), port


def act(kind, **payload): return ChannelAction(kind, payload)


def test_full_flow_has_exact_versions_one_mutation_and_no_public_ids():
    flow, port = service(); ctx = context()
    assert flow.handle(ctx, act("SELECT_SERVICE", service_option_id=OPTION)).draft_version == 1
    assert flow.handle(ctx, act("SET_REQUESTER", name="Ann")).draft_version == 2
    assert flow.handle(ctx, act("SET_DETAILS", participants_count=2, requested_time={"date": "2026-10-01", "time_text": "10:00"}, experience="BEGINNER")).draft_version == 3
    result = flow.handle(ctx, act("SUBMIT"))
    assert result.kind == "ACCEPTED_UNCONFIRMED"
    assert result.adapter_metadata == {"receipt_id": "trusted-receipt", "inquiry_id": "trusted-inquiry"}
    assert "trusted-receipt" not in result.as_dict().__repr__()
    assert "trusted-inquiry" not in result.as_dict().__repr__()
    assert result.data == {"semantic_code": "INQUIRY_ACCEPTED_UNCONFIRMED"}
    assert [call[1] for call in port.mutations[:3]] == [0, 1, 2]


def test_open_resume_help_and_reset_are_platform_neutral():
    flow, port = service(); ctx = context(Platform.VK)
    flow.handle(ctx, act("SELECT_SERVICE", service_option_id=OPTION))
    opened = flow.handle(ctx, act("OPEN"))
    assert opened.screen == "REQUESTER" and opened.data["catalog"] == CATALOG
    help_result = flow.handle(ctx, act("HELP"))
    assert set(help_result.data) == {"contact_info", "location_link", "visit_rules"}
    flow.handle(ctx, act("RESET"))
    assert [x[0] for x in port.mutations] == ["save", "reset"]


def test_strict_shapes_active_option_and_no_gate():
    flow, port = service(); ctx = context()
    with pytest.raises(Exception): flow.handle(ctx, act("SELECT_SERVICE", service_option_id="inactive"))
    with pytest.raises(Exception): ChannelAction("SELECT_SERVICE", {"service_option_id": OPTION, "hidden": True})
    result = flow.handle(ctx, act("SELECT_SERVICE", service_option_id=OPTION))
    result = flow.handle(ctx, act("SET_REQUESTER", name="Ann", contact={"kind": "TELEGRAM", "value": "custom"}))
    result = flow.handle(ctx, act("SET_DETAILS", participants_count=1, requested_time={"date": "2026-10-01"}, experience="BEGINNER"))
    assert result.screen == "REVIEW" and not port.mutations[-1][2].get("admission")


def test_omitted_contact_is_not_filled_from_trusted_sender_and_platforms_match():
    telegram, tp = service(); vk, vp = service()
    for flow, port, platform in ((telegram, tp, Platform.TELEGRAM), (vk, vp, Platform.VK)):
        flow.handle(context(platform), act("SELECT_SERVICE", service_option_id=OPTION))
        flow.handle(context(platform), act("SET_REQUESTER", name="Ann"))
        assert port.mutations[-1][2]["requester"] == {"name": "Ann"}
        assert "sender" not in repr(port.mutations[-1][2])


def test_malformed_action_context_and_corrupt_draft_are_typed_and_do_not_mutate():
    flow, port = service()
    with pytest.raises(Exception) as exc:
        flow.handle(context(), ChannelAction("SET_REQUESTER", {"contact": None}))
    assert getattr(exc.value, "code", None) == "INVALID_ACTION"
    with pytest.raises(Exception) as exc:
        flow.handle("not-context", act("OPEN"))
    assert getattr(exc.value, "code", None) == "INVALID_CONTEXT"
    port.draft = Draft({"service_option_id": OPTION, "unexpected": True}, "REQUESTER", 1)
    with pytest.raises(Exception) as exc:
        flow.handle(context(), act("OPEN"))
    assert getattr(exc.value, "code", None) == "CORRUPT_DRAFT"
    assert port.mutations == []


def test_gateway_error_identity_is_preserved_and_rejections_make_zero_mutations():
    flow, port = service()
    flow.handle(context(), act("SELECT_SERVICE", service_option_id=OPTION))
    original = __import__("apps.api.sivka_burka_api.channel_gateway", fromlist=["GatewayError"]).GatewayError("VERSION_CONFLICT")
    port.reset_draft = lambda *args, **kwargs: (_ for _ in ()).throw(original)
    with pytest.raises(Exception) as exc:
        flow.handle(context(), act("RESET"))
    assert exc.value is original
    assert [mutation[0] for mutation in port.mutations] == ["save"]


@pytest.mark.parametrize("draft", [
    Draft({}, "UNKNOWN", 1),
    Draft({"service_option_id": OPTION, "requester": {"name": "Ann", "extra": "x"}}, "DETAILS", 1),
    Draft({"service_option_id": OPTION, "requester": {"name": "Ann"}, "participants_count": 1, "requested_time": {"date": "2026-10-01"}, "experience": "UNKNOWN", "comment": ""}, "REVIEW", 1),
    Draft({"service_option_id": INACTIVE}, "REQUESTER", 1),
])
def test_every_corrupt_or_inactive_stored_shape_is_rejected_before_mutation(draft):
    flow, port = service(); port.draft = draft
    with pytest.raises(Exception) as exc:
        flow.handle(context(), act("OPEN"))
    assert getattr(exc.value, "code", None) in {"CORRUPT_DRAFT", "UNKNOWN_SERVICE_OPTION"}
    assert port.mutations == []


def test_inactive_selection_and_missing_required_values_are_typed_rejections():
    flow, port = service()
    with pytest.raises(Exception) as exc:
        flow.handle(context(), act("SELECT_SERVICE", service_option_id=INACTIVE))
    assert getattr(exc.value, "code", None) == "UNKNOWN_SERVICE_OPTION"
    assert port.mutations == []
    with pytest.raises(Exception) as exc:
        flow.handle(context(), ChannelAction("SET_DETAILS", {"participants_count": 1}))
    assert getattr(exc.value, "code", None) == "INVALID_ACTION"
    assert port.mutations == []
