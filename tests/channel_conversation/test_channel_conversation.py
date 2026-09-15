from dataclasses import dataclass

import pytest

from apps.api.sivka_burka_api.channel_conversation import ChannelAction, ChannelConversation
from apps.api.sivka_burka_api.channel_gateway import (
    ChannelContext, ChannelGateway, Draft, DraftSaved, InquiryReceipt, Platform, ResetResult,
)


OPTION = "option-active"
CATALOG = {"catalog_version": 1, "services": [{"id": "service", "title": "Ride", "options": [{"id": OPTION}]}], "contact_info": "owner", "location_link": "place", "visit_rules": "rules"}


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
    assert result.kind == "ACCEPTED_UNCONFIRMED" and result.receipt_id == "trusted-receipt"
    assert "trusted-inquiry" not in result.data.values()
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
