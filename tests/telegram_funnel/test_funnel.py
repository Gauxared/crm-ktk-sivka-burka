from dataclasses import dataclass

import pytest

from apps.api.sivka_burka_api.channel_conversation import ChannelConversation
from apps.api.sivka_burka_api.channel_gateway import ChannelContext, ChannelGateway, Draft, DraftSaved, InquiryReceipt, Platform
from apps.bot.sivka_burka_bot.telegram_actions import TelegramActionResolver
from apps.bot.sivka_burka_bot.telegram_conversation import TelegramConversationOrchestrator
from apps.bot.sivka_burka_bot.telegram_interactions import TelegramInteractionMapper
from apps.bot.sivka_burka_bot.telegram_rendering import TelegramOutcomeRenderer
from apps.bot.sivka_burka_bot.telegram_screen_actions import TelegramScreenActionError, TelegramScreenActionResolver
from apps.bot.sivka_burka_bot.telegram_updates import AcceptedTelegramUpdate, InputKind

CATALOG={"catalog_version":4,"contact_info":"Телефон","location_link":"Клуб","visit_rules":"По записи","services":[{"title":"Конная прогулка","options":[{"id":"fixed","duration_minutes":45,"pricing_mode":"FIXED_PER_PERSON","price_minor":12500,"currency":"RUB"},{"id":"negotiated","duration_minutes":None,"pricing_mode":"NEGOTIATED","price_minor":None,"currency":"RUB"}]}]}
@dataclass
class Port:
    draft: Draft|None=None
    def __post_init__(self): self.mutations=[]
    def read_catalog(self,c): return CATALOG
    def read_draft(self,c): return self.draft
    def save_draft(self,c,expected_version,answers,step,event_id):
        self.mutations.append((step,event_id)); self.draft=Draft(dict(answers),step,expected_version+1); return DraftSaved(self.draft.version,step)
    def submit_inquiry(self,c,fields,draft_version,submit_event_id): self.mutations.append(("SUBMIT",submit_event_id)); return InquiryReceipt("receipt-private","inquiry-private")
    def reset_draft(self,*args): raise AssertionError
def update(event, *, text=None, data=None):
    return AcceptedTelegramUpdate(ChannelContext(Platform.TELEGRAM,"tg","sender","chat",event),InputKind.TEXT if text is not None else InputKind.CALLBACK,text=text,data=data,callback_query_id=None if text is not None else f"callback-{event}")
def app():
    port=Port(); return TelegramConversationOrchestrator(TelegramInteractionMapper(),TelegramActionResolver(),TelegramScreenActionResolver(),ChannelConversation(ChannelGateway(port))),TelegramOutcomeRenderer(),port
def test_full_funnel_uses_actual_boundaries_and_never_renders_private_ids():
    orchestration,renderer,port=app()
    assert "Конная прогулка" in renderer.render(orchestration.handle(update("1",text="/start"))).message.text
    requester=renderer.render(orchestration.handle(update("2",data="v1:s:4:0"))); assert "Как к вам" in requester.message.text
    details=renderer.render(orchestration.handle(update("3",text="Алиса"))); assert "Отправьте" in details.message.text
    review=renderer.render(orchestration.handle(update("4",text="Участники: 2\nДата: 2026-10-01\nВремя: 10:00\nОпыт: новичок\nКомментарий: -")))
    assert [b.callback_data for b in review.message.buttons]==["v1:u","v1:r","v1:h"]
    accepted=renderer.render(orchestration.handle(update("5",data="v1:u")))
    assert "receipt-private" not in repr(accepted) and "inquiry-private" not in accepted.message.text
    assert port.mutations==[("REQUESTER","2"),("DETAILS","3"),("REVIEW","4"),("SUBMIT","5")]
def test_malformed_details_stops_after_open_preflight():
    orchestration,_,port=app(); orchestration.handle(update("1",text="/start")); orchestration.handle(update("2",data="v1:s:4:0")); orchestration.handle(update("3",text="Алиса"))
    with pytest.raises(TelegramScreenActionError) as raised: orchestration.handle(update("4",text="Дата: nope"))
    assert raised.value.code=="INVALID_DETAILS_INPUT" and len(port.mutations)==2
