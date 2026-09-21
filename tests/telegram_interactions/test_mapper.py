from apps.api.sivka_burka_api.channel_gateway import ChannelContext, Platform
from apps.bot.sivka_burka_bot.telegram_interactions import TelegramInteractionMapper, TelegramIntentKind
from apps.bot.sivka_burka_bot.telegram_updates import AcceptedTelegramUpdate, InputKind


def update(text=None, data=None, kind=InputKind.TEXT):
    return AcceptedTelegramUpdate(ChannelContext(Platform.TELEGRAM, "i", "s", "c", "e"), kind, text=text, data=data, callback_query_id="qid" if kind is InputKind.CALLBACK else None)


def test_commands_text_callbacks_and_service_are_explicit():
    mapper = TelegramInteractionMapper()
    assert mapper.map(update(text="/START@bot")).kind is TelegramIntentKind.OPEN
    assert mapper.map(update(text="hello")).payload["text"] == "hello"
    assert mapper.map(update(data="v1:u", kind=InputKind.CALLBACK)).kind is TelegramIntentKind.SUBMIT
    intent = mapper.map(update(data="v1:s:1:0", kind=InputKind.CALLBACK))
    assert intent.kind is TelegramIntentKind.SELECT_SERVICE
    assert dict(intent.payload) == {"catalog_version": 1, "option_ordinal": 0}
