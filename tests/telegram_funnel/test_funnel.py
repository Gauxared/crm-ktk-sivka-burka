from dataclasses import dataclass, field

import pytest

from apps.api.sivka_burka_api.channel_conversation import ChannelConversation
from apps.api.sivka_burka_api.channel_gateway import (
    ChannelContext,
    ChannelGateway,
    Draft,
    DraftSaved,
    InquiryReceipt,
    Platform,
    ResetResult,
)
from apps.bot.sivka_burka_bot.telegram_actions import TelegramActionError, TelegramActionResolver
from apps.bot.sivka_burka_bot.telegram_conversation import TelegramConversationOrchestrator
from apps.bot.sivka_burka_bot.telegram_interactions import TelegramInteractionMapper
from apps.bot.sivka_burka_bot.telegram_rendering import TelegramOutcomeRenderer
from apps.bot.sivka_burka_bot.telegram_screen_actions import (
    TelegramScreenActionError,
    TelegramScreenActionResolver,
)
from apps.bot.sivka_burka_bot.telegram_updates import AcceptedTelegramUpdate, InputKind


CATALOG = {
    "catalog_version": 4,
    "contact_info": "Телефон",
    "location_link": "Клуб",
    "visit_rules": "По записи",
    "services": [{
        "title": "Конная прогулка",
        "options": [
            {
                "id": "fixed",
                "duration_minutes": 45,
                "pricing_mode": "FIXED_PER_PERSON",
                "price_minor": 12_500,
                "currency": "RUB",
            },
            {
                "id": "negotiated",
                "duration_minutes": None,
                "pricing_mode": "NEGOTIATED",
                "price_minor": None,
                "currency": "RUB",
            },
        ],
    }],
}
DETAILS_TEXT = (
    "Участники: 2\n"
    "Дата: 2026-10-01\n"
    "Время: 10:00\n"
    "Опыт: новичок\n"
    "Комментарий: -"
)


@dataclass
class RecordingPort:
    draft: Draft | None = None
    mutations: list[tuple] = field(default_factory=list)
    submissions: dict[str, InquiryReceipt] = field(default_factory=dict)

    def read_catalog(self, context):
        return CATALOG

    def read_draft(self, context):
        return self.draft

    def save_draft(self, context, expected_version, answers, step, event_id):
        actual_version = 0 if self.draft is None else self.draft.version
        assert expected_version == actual_version
        self.mutations.append(("save", expected_version, step, event_id))
        self.draft = Draft(dict(answers), step, expected_version + 1)
        return DraftSaved(self.draft.version, step)

    def submit_inquiry(self, context, fields, draft_version, submit_event_id):
        assert self.draft is not None and draft_version == self.draft.version
        previous = self.submissions.get(submit_event_id)
        if previous is not None:
            return InquiryReceipt(previous.receipt_id, previous.inquiry_id, replay=True)
        receipt = InquiryReceipt("receipt-private", "inquiry-private")
        self.submissions[submit_event_id] = receipt
        self.mutations.append(("submit", draft_version, submit_event_id))
        return receipt

    def reset_draft(self, context, expected_version, event_id):
        assert self.draft is not None and expected_version == self.draft.version
        self.mutations.append(("reset", expected_version, event_id))
        self.draft = None
        return ResetResult(True, expected_version + 1)


def update(event: str, *, text: str | None = None, data: str | None = None) -> AcceptedTelegramUpdate:
    context = ChannelContext(Platform.TELEGRAM, "tg", "sender", "chat", event)
    input_kind = InputKind.TEXT if text is not None else InputKind.CALLBACK
    callback_id = None if text is not None else f"callback-{event}"
    return AcceptedTelegramUpdate(
        context,
        input_kind,
        text=text,
        data=data,
        callback_query_id=callback_id,
    )


def app():
    port = RecordingPort()
    conversation = ChannelConversation(ChannelGateway(port))
    orchestrator = TelegramConversationOrchestrator(
        TelegramInteractionMapper(),
        TelegramActionResolver(),
        TelegramScreenActionResolver(),
        conversation,
    )
    return orchestrator, TelegramOutcomeRenderer(), port


def advance_to_review(orchestrator):
    orchestrator.handle(update("1", text="/start"))
    orchestrator.handle(update("2", data="v1:s:4:0"))
    orchestrator.handle(update("3", text="Алиса"))
    return orchestrator.handle(update("4", text=DETAILS_TEXT))


def test_full_funnel_uses_actual_boundaries_and_never_renders_private_ids():
    orchestrator, renderer, port = app()

    opened = renderer.render(orchestrator.handle(update("1", text="/start")))
    assert opened.callback_query_id is None
    assert "Конная прогулка" in opened.message.text

    requester = renderer.render(orchestrator.handle(update("2", data="v1:s:4:0")))
    assert requester.callback_query_id == "callback-2"
    assert "Как к вам" in requester.message.text

    details = renderer.render(orchestrator.handle(update("3", text="Алиса")))
    assert details.callback_query_id is None
    assert "ровно пять строк" in details.message.text

    review = renderer.render(orchestrator.handle(update("4", text=DETAILS_TEXT)))
    assert [button.callback_data for button in review.message.buttons] == ["v1:u", "v1:r", "v1:h"]
    assert "Имя: Алиса" in review.message.text

    accepted = renderer.render(orchestrator.handle(update("5", data="v1:u")))
    assert accepted.callback_query_id == "callback-5"
    assert "receipt-private" not in repr(accepted)
    assert "inquiry-private" not in accepted.message.text
    assert port.mutations == [
        ("save", 0, "REQUESTER", "2"),
        ("save", 1, "DETAILS", "3"),
        ("save", 2, "REVIEW", "4"),
        ("submit", 3, "5"),
    ]


def test_malformed_details_stops_after_read_only_open_preflight():
    orchestrator, _, port = app()
    orchestrator.handle(update("1", text="/start"))
    orchestrator.handle(update("2", data="v1:s:4:0"))
    orchestrator.handle(update("3", text="Алиса"))
    before = list(port.mutations)

    with pytest.raises(TelegramScreenActionError) as raised:
        orchestrator.handle(update("4", text="Дата: nope"))

    assert raised.value.code == "INVALID_DETAILS_INPUT"
    assert port.mutations == before


def test_stale_selection_stops_after_read_only_open_preflight():
    orchestrator, _, port = app()

    with pytest.raises(TelegramActionError) as raised:
        orchestrator.handle(update("1", data="v1:s:3:0"))

    assert raised.value.code == "STALE_CATALOG"
    assert port.mutations == []


def test_duplicate_submit_replays_without_duplicate_mutation_or_private_output():
    orchestrator, renderer, port = app()
    advance_to_review(orchestrator)
    submit = update("5", data="v1:u")

    first = orchestrator.handle(submit)
    second = orchestrator.handle(submit)

    assert first.result.replay is False
    assert second.result.replay is True
    assert [mutation for mutation in port.mutations if mutation[0] == "submit"] == [("submit", 3, "5")]
    rendered = renderer.render(second)
    assert "receipt-private" not in repr(rendered)
    assert "inquiry-private" not in rendered.message.text
