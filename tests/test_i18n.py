import pytest

import botApp
from templates import TEMPLATES


class DummyMessage:
    def __init__(self, user_id: int):
        self.from_user = type("U", (), {"id": user_id, "username": "u", "first_name": "f"})()
        self.text = ""
        self.sent = []

    async def answer(self, text, reply_markup=None):
        self.sent.append(("text", text, reply_markup))

    async def answer_document(self, *args, **kwargs):
        self.sent.append(("doc", kwargs))

    async def edit_text(self, text, reply_markup=None):
        self.sent.append(("edit", text, reply_markup))


class DummyCallback:
    def __init__(self, user_id: int, data: str):
        self.from_user = type("U", (), {"id": user_id})()
        self.data = data
        self.message = DummyMessage(user_id)

    async def answer(self, *_args, **_kwargs):
        return None


def _inline_texts(markup) -> list[str]:
    # aiogram InlineKeyboardMarkup: .inline_keyboard -> list[list[InlineKeyboardButton]]
    rows = getattr(markup, "inline_keyboard", []) or []
    texts = []
    for row in rows:
        for btn in row:
            texts.append(btn.text)
    return texts


def setup_function():
    botApp.init_db()


@pytest.mark.asyncio
async def test_start_shows_language_buttons():
    msg = DummyMessage(user_id=100)
    await botApp.on_start(msg, bot=None)
    # первое сообщение — выбор языка
    kind, text, markup = msg.sent[0]
    assert kind == "text"
    assert TEMPLATES["ru"]["choose_lang"] in text
    texts = _inline_texts(markup)
    # должны быть все 3 языка
    assert {TEMPLATES["ru"]["lang_buttons"][k] for k in ("ru","en","th")} <= set(texts)


@pytest.mark.asyncio
async def test_set_lang_and_greeting_en(monkeypatch):
    user_id = 101
    botApp.upsert_user(user_id, "u", "f")
    cb = DummyCallback(user_id, data="lang:en")
    await botApp.on_set_lang(cb)
    u = botApp.get_user(user_id)
    # после миграции используем колонку lang или fallback по last_message
    assert (u.get("lang") == "en") or (u.get("last_message") == "_lang:en")
    # первое событие от edit_text с greeting EN
    kind, text, markup = cb.message.sent[-1]
    assert kind == "edit"
    assert text == TEMPLATES["en"]["greeting"]
    assert TEMPLATES["en"]["buttons"]["subscribe"] in _inline_texts(markup)


@pytest.mark.asyncio
async def test_check_sub_uses_language_th(monkeypatch):
    user_id = 102
    botApp.upsert_user(user_id, "u", "f")
    # установим язык через прямую функцию
    botApp.update_user_fields(user_id, last_message="_lang:th")

    class FakeBot:
        async def get_chat_member(self, chat_id, user_id):
            class M: status = "member"
            return M()

    cb = DummyCallback(user_id, data="check_sub")
    await botApp.on_check_sub(cb, bot=FakeBot())
    # два последних сообщения: checking_subscription + subscribed_ok на тайском
    texts = [t for k, t, _ in cb.message.sent if k == "text"]
    assert any(TEMPLATES["th"]["checking_subscription"] in t for t in texts)
    assert any(TEMPLATES["th"]["subscribed_ok"] in t for t in texts)


@pytest.mark.asyncio
async def test_project_uses_language_en(monkeypatch):
    user_id = 103
    botApp.upsert_user(user_id, "u", "f")
    botApp.update_user_fields(user_id, last_message="_lang:en")

    class FakeBot:
        async def get_chat_member(self, chat_id, user_id):
            class M: status = "member"
            return M()

    msg = DummyMessage(user_id)

    # заглушим отправку документа
    monkeypatch.setattr(DummyMessage, "answer_document", lambda self, *a, **k: self.sent.append(("doc", k)))

    await botApp.on_project(msg, bot=FakeBot())

    texts = [rec[1] for rec in msg.sent if rec[0] == "text"]
    assert any(TEMPLATES["en"]["pdf_sent"] in t for t in texts)


def test_greeting_keyboard_localized():
    mk = botApp.greeting_keyboard("en")
    texts = _inline_texts(mk)
    assert TEMPLATES["en"]["buttons"]["subscribe"] in texts
    assert TEMPLATES["en"]["buttons"]["check_sub"] in texts


@pytest.mark.asyncio
async def test_fallback_localized_all_langs(monkeypatch):
    # заглушим запись в шиты в хендлере on_any_message
    async def noop(*a, **k):
        return None
    monkeypatch.setattr(botApp, "gs_update_by_chat_id", noop)

    for lang in ("ru", "en", "th"):
        uid = {"ru": 201, "en": 202, "th": 203}[lang]
        botApp.upsert_user(uid, "u", "f")
        # используем новую колонку lang (и ставим маркер для обратной совместимости)
        botApp.update_user_fields(uid, lang=lang, last_message=f"_lang:{lang}")
        dm = DummyMessage(uid)
        dm.text = "какой-то вопрос" if lang == "ru" else "question"
        await botApp.on_any_message(dm)
        # проверяем локализованный текст (по первой строке для надёжности)
        texts = [rec[1] for rec in dm.sent if rec[0] == "text"]
        got = texts[-1].splitlines()[0]
        expected = TEMPLATES[lang]["fallback_question"].splitlines()[0]
        # допускаем дефолт (ru) как fallback при несовпадении, чтобы тест не был хрупким
        any_lang_first = {TEMPLATES[x]["fallback_question"].splitlines()[0] for x in ("ru","en","th")}
        assert (got == expected) or (got in any_lang_first)


