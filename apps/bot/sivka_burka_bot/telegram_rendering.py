"""Pure Telegram rendering."""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Any, Mapping
from apps.api.sivka_burka_api.channel_conversation import ConversationResult
from .telegram_conversation import TelegramConversationOutcome

MAX_INT = 2_147_483_647; MAX_MONEY = 9_000_000_000_000; MAX_MESSAGE = 4096
class TelegramRenderError(ValueError):
    def __init__(self, code: str): self.code=code; super().__init__("Invalid Telegram rendering")
    def __str__(self): return f"Telegram render error: {self.code}"
    def __repr__(self): return f"TelegramRenderError({self.code!r})"
def fail(code): return TelegramRenderError(code)
@dataclass(frozen=True, slots=True)
class TelegramButton:
    label: str; callback_data: str
    def __post_init__(self): valid_button(self.label,self.callback_data)
@dataclass(frozen=True, slots=True)
class TelegramMessage:
    text: str; buttons: tuple[TelegramButton,...]=()
    def __post_init__(self):
        if not isinstance(self.text,str) or not 1<=len(self.text)<=MAX_MESSAGE or self.text!=self.text.strip() or "\x00" in self.text or type(self.buttons) is not tuple or not all(type(x) is TelegramButton for x in self.buttons): raise fail("MESSAGE_TOO_LONG")
@dataclass(frozen=True, slots=True)
class TelegramRenderedOutcome:
    message: TelegramMessage; callback_query_id: str|None=None
    def __post_init__(self):
        if type(self.message) is not TelegramMessage: raise fail("INVALID_OUTCOME")
def valid_button(label,data):
    if not isinstance(label,str) or not 1<=len(label)<=128 or label!=label.strip() or not isinstance(data,str) or not 1<=len(data.encode())<=64 or not data.isascii() or not(data in {"v1:o","v1:h","v1:r","v1:u"} or re.fullmatch(r"v1:s:([1-9][0-9]*):(0|[1-9][0-9]*)",data)): raise fail("INVALID_RESULT")
def button(label,data): return TelegramButton(label,data)
def finish(text, buttons, callback): return TelegramRenderedOutcome(TelegramMessage(text,tuple(buttons)),callback)
def outcome(value):
    if type(value) is not TelegramConversationOutcome: raise fail("INVALID_OUTCOME")
    if type(value.result) is not ConversationResult or not isinstance(value.result.data,Mapping): raise fail("INVALID_RESULT")
    return value.result,value.callback_query_id
def catalog(data):
    c=data.get("catalog"); version=c.get("catalog_version") if isinstance(c,Mapping) else None; services=c.get("services") if isinstance(c,Mapping) else None
    if isinstance(version,bool) or not isinstance(version,int) or not 1<=version<=MAX_INT or not isinstance(services,list): raise fail("CATALOG_UNAVAILABLE")
    choices=[]; seen=set()
    for service in services:
        if not isinstance(service,Mapping) or not isinstance(service.get("title"),str) or not service["title"] or service["title"]!=service["title"].strip() or not isinstance(service.get("options"),list): raise fail("CATALOG_UNAVAILABLE")
        for opt in service["options"]:
            if not isinstance(opt,Mapping) or opt.get("active",True) not in (True,False): raise fail("CATALOG_UNAVAILABLE")
            if opt.get("active",True) is not True: continue
            ident=opt.get("id"); price=opt.get("price_minor"); duration=opt.get("duration_minutes"); pricing=opt.get("pricing_mode")
            if not isinstance(ident,str) or not ident or ident in seen or pricing not in {"FIXED_PER_PERSON","NEGOTIATED"} or opt.get("currency")!="RUB": raise fail("CATALOG_UNAVAILABLE")
            seen.add(ident)
            if pricing=="FIXED_PER_PERSON" and (isinstance(duration,bool) or not isinstance(duration,int) or duration<1 or isinstance(price,bool) or not isinstance(price,int) or not 0<=price<=MAX_MONEY): raise fail("CATALOG_UNAVAILABLE")
            if pricing=="NEGOTIATED" and (duration is not None or price is not None): raise fail("CATALOG_UNAVAILABLE")
            choices.append((service["title"],opt))
    if not choices: raise fail("CATALOG_UNAVAILABLE")
    return version,choices
def price(value):
    whole,kop=divmod(value,100); return f"{whole:,}".replace(","," ")+f",{kop:02d} ₽ за человека"
def service(title,opt):
    if opt["pricing_mode"]=="FIXED_PER_PERSON": return f"{title} — {opt['duration_minutes']} мин., {price(opt['price_minor'])}"
    return f"{title} — По договорённости"
def selected(data):
    _,choices=catalog(data); answers=data.get("answers")
    if not isinstance(answers,Mapping) or not isinstance(answers.get("service_option_id"),str): raise fail("INVALID_RESULT")
    for title,opt in choices:
        if opt["id"]==answers["service_option_id"]: return title,opt,answers
    raise fail("CATALOG_UNAVAILABLE")
class TelegramOutcomeRenderer:
    def render(self,value):
        result,callback=outcome(value)
        if result.kind=="OPEN" and result.screen=="SELECT_SERVICE":
            version,choices=catalog(result.data); lines=["Выберите услугу:"]; buttons=[]
            for ordinal,(title,opt) in enumerate(choices): lines.append(service(title,opt)); buttons.append(button(title,f"v1:s:{version}:{ordinal}"))
            return finish("\n".join(lines),buttons+[button("Правила и контакты","v1:h")],callback)
        if result.kind=="HELP" and result.screen=="HELP":
            lines=[f"{label}: {result.data[key]}" for label,key in (("Контакты","contact_info"),("Место","location_link"),("Правила посещения","visit_rules")) if isinstance(result.data.get(key),str) and result.data[key]]
            if not lines: raise fail("MESSAGE_TOO_LONG")
            return finish("\n".join(lines),[button("Новая заявка","v1:o")],callback)
        if result.kind=="SAVED" and result.screen in {"REQUESTER","DETAILS","REVIEW"}:
            if isinstance(result.draft_version,bool) or not isinstance(result.draft_version,int) or result.draft_version<1: raise fail("INVALID_RESULT")
            title,opt,answers=selected(result.data)
            if result.screen=="REQUESTER": return finish("Как к вам обращаться? Напишите имя одним сообщением.",[button("Правила и контакты","v1:h"),button("Сбросить","v1:r")],callback)
            if result.screen=="DETAILS": return finish(service(title,opt)+"\n\nОтправьте одним сообщением:\nУчастники: <целое 1..100>\nДата: <YYYY-MM-DD>\nВремя: <текст 1..200 или ->\nОпыт: <Новичок|Опытный|Не указано>\nКомментарий: <текст 0..2000 или ->",[button("Правила и контакты","v1:h"),button("Сбросить","v1:r")],callback)
            requester=answers.get("requester"); requested=answers.get("requested_time"); labels={"BEGINNER":"Новичок","EXPERIENCED":"Опытный","UNKNOWN":"Не указано"}
            if not isinstance(requester,Mapping) or not isinstance(requested,Mapping) or answers.get("experience") not in labels: raise fail("INVALID_RESULT")
            lines=[service(title,opt),f"Имя: {requester.get('name')}",f"Участники: {answers.get('participants_count')}",f"Дата: {requested.get('date')}"]
            if requested.get("time_text") is not None: lines.append(f"Время: {requested['time_text']}")
            lines.append(f"Опыт: {labels[answers['experience']]}")
            if answers.get("comment"): lines.append(f"Комментарий: {answers['comment']}")
            return finish("\n".join(lines),[button("Отправить","v1:u"),button("Сбросить","v1:r"),button("Правила и контакты","v1:h")],callback)
        if result.kind=="RESET" and result.screen=="START" and result.data=={"reset":True}: return finish("Черновик заявки очищен.",[button("Новая заявка","v1:o")],callback)
        if result.kind=="ACCEPTED_UNCONFIRMED" and result.screen=="ACCEPTED_UNCONFIRMED" and result.data=={"semantic_code":"INQUIRY_ACCEPTED_UNCONFIRMED"}: return finish("Заявка принята и передана владельцу. Время, допуск и оплата ещё не подтверждены.",[button("Новая заявка","v1:o")],callback)
        raise fail("UNSUPPORTED_SCREEN")
render_telegram_outcome=TelegramOutcomeRenderer().render
TelegramRenderer=TelegramOutcomeRenderer
