from types import MappingProxyType

import pytest

from apps.api.sivka_burka_api.channel_conversation import ConversationResult
from apps.bot.sivka_burka_bot.telegram_conversation import TelegramConversationOutcome
from apps.bot.sivka_burka_bot.telegram_rendering import (
    TelegramButton,
    TelegramMessage,
    TelegramOutcomeRenderer,
    TelegramRenderError,
    TelegramRenderedOutcome,
)


CATALOG = {
    "catalog_version": 7,
    "club_timezone": "Europe/Moscow",
    "contact_info": "+7 900 000-00-00",
    "location_link": "https://example.test/map",
    "visit_rules": "По записи",
    "services": [
        {"id": "horse", "title": "Конная прогулка", "options": [
            {"id": "inactive", "active": False, "duration_minutes": 30, "pricing_mode": "FIXED_PER_PERSON", "price_minor": 1, "currency": "RUB"},
            {"id": "short", "duration_minutes": 45, "pricing_mode": "FIXED_PER_PERSON", "price_minor": 12500, "currency": "RUB"},
            {"id": "long", "duration_minutes": 90, "pricing_mode": "NEGOTIATED", "price_minor": None, "currency": None},
        ]},
    ],
}


def outcome(kind, screen, data, **kwargs):
    return TelegramConversationOutcome(ConversationResult(kind, screen, data, **kwargs), "callback-id")


def test_select_service_is_ordered_and_private():
    rendered = TelegramOutcomeRenderer().render(outcome("OPEN", "SELECT_SERVICE", {"catalog": CATALOG}))
    assert rendered.callback_query_id == "callback-id"
    assert rendered.message.text == "Выберите услугу:\nКонная прогулка — 45 мин., 125,00 ₽ за человека\nКонная прогулка — 90 мин., По договорённости"
    assert [button.callback_data for button in rendered.message.buttons] == ["v1:s:7:0", "v1:s:7:1", "v1:h"]
    assert all("horse" not in button.callback_data and "short" not in button.callback_data for button in rendered.message.buttons)


def test_help_uses_only_supplied_fields_and_reset_requester_copy():
    renderer = TelegramOutcomeRenderer()
    help_result = renderer.render(outcome("HELP", "HELP", {"contact_info": "Телефон", "visit_rules": "По записи"}))
    assert help_result.message.text == "Контакты: Телефон\nПравила посещения: По записи"
    requester = renderer.render(outcome("SAVED", "REQUESTER", {"answers": {"service_option_id": "secret"}}))
    assert requester.message.text == "Как к вам обращаться? Напишите имя одним сообщением."
    assert "secret" not in repr(requester)
    reset = renderer.render(outcome("RESET", "START", {"reset": True}))
    assert reset.message.text == "Черновик заявки очищен."


def test_acceptance_hides_metadata_and_ids():
    result = ConversationResult("ACCEPTED_UNCONFIRMED", "ACCEPTED_UNCONFIRMED", {"semantic_code": "INQUIRY_ACCEPTED_UNCONFIRMED"}, adapter_metadata={"receipt_id": "receipt-secret", "inquiry_id": "inquiry-secret"}, replay=True)
    rendered = TelegramOutcomeRenderer().render(TelegramConversationOutcome(result, "cb"))
    assert "receipt-secret" not in repr(rendered)
    assert "inquiry-secret" not in rendered.message.text
    assert rendered.callback_query_id == "cb"


@pytest.mark.parametrize("result", [
    ConversationResult("OPEN", "DETAILS", {}),
    ConversationResult("SAVED", "REVIEW", {}),
    ConversationResult("HELP", "START", {}),
])
def test_unsupported_screens_are_typed(result):
    with pytest.raises(TelegramRenderError) as raised:
        TelegramOutcomeRenderer().render(TelegramConversationOutcome(result))
    assert raised.value.code == "UNSUPPORTED_SCREEN"
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("catalog", [
    {},
    {"catalog_version": True, "services": []},
    {"catalog_version": 1, "services": []},
    {"catalog_version": 1, "services": [{"title": "", "options": []}]},
    {"catalog_version": 1, "services": [{"title": "Услуга", "options": [{"id": "x", "duration_minutes": 1, "pricing_mode": "FIXED_PER_PERSON", "price_minor": 1, "currency": "USD"}]}]},
])
def test_bad_catalog_fails_closed(catalog):
    with pytest.raises(TelegramRenderError) as raised:
        TelegramOutcomeRenderer().render(outcome("OPEN", "SELECT_SERVICE", {"catalog": catalog}))
    assert raised.value.code == "CATALOG_UNAVAILABLE"


def test_dtos_are_frozen_and_collections_are_tuples():
    assert TelegramMessage("x").buttons == ()
    assert TelegramRenderedOutcome(TelegramMessage("x")).message.text == "x"
    with pytest.raises(AttributeError):
        TelegramButton("x", "v1:o").label = "y"


def test_exact_outcome_and_no_result_as_dict():
    class Derived(TelegramConversationOutcome):
        pass
    with pytest.raises(TelegramRenderError) as raised:
        TelegramOutcomeRenderer().render(Derived(ConversationResult("OPEN", "SELECT_SERVICE", {"catalog": CATALOG})))
    assert raised.value.code == "INVALID_OUTCOME"
