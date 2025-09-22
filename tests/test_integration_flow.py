import pytest

import botApp


@pytest.mark.asyncio
async def test_flow_start_check_project(monkeypatch):
    # 1) Sheets стабы
    class WS:
        def __init__(self):
            self.rows = []
            self._header = [
                "chat_id","username","first_name","date_joined","subscribed",
                "last_message","file_sent","followup_attempts","manager_contacted"
            ]
        def append_row(self, row, value_input_option=None):
            self.rows.append(row)
        def row_values(self, i):
            return self._header
        def find(self, value):
            raise Exception("not found")
        @property
        def spreadsheet(self):
            class S:
                def values_batch_update(self, body):
                    pass
            return S()

    class GC:
        def __init__(self):
            self.ws = WS()
        def open_by_key(self, key):
            class S:
                def __init__(self, ws):
                    self._ws = ws
                def worksheet(self, name):
                    return self._ws
            return S(self.ws)

    monkeypatch.setattr(botApp, "_build_gspread_client", lambda: GC())

    # 2) Telegram get_chat_member всегда member
    async def ok_member(chat_id, user_id):
        class M: status = "member"
        return M()
    # создадим фейковый bot-объект, который используется в хендлере
    class FakeBot:
        async def get_chat_member(self, chat_id, user_id):
            return await ok_member(chat_id, user_id)
    fake_bot = FakeBot()

    # 3) Сообщение/ответы фейковые
    class Dummy:
        def __init__(self, user_id=1):
            self.from_user = type("U", (), {"id": user_id, "username": "u", "first_name": "f"})()
            self.text = "проект"
            self.sent = []
        async def answer(self, text, reply_markup=None):
            self.sent.append(text)
        async def answer_document(self, *args, **kwargs):
            self.sent.append("doc")

    dummy = Dummy()

    # 4) Выполняем handlers вручную
    await botApp.on_start(dummy, bot=fake_bot)
    # кнопка check_sub
    class Callback:
        def __init__(self, u):
            self.from_user = u
            class Msg:
                async def answer(self, *_args, **_kwargs):
                    return None
            self.message = Msg()
        async def answer(self, *_args, **_kwargs):
            return None
    cb = Callback(dummy.from_user)
    await botApp.on_check_sub(cb, bot=fake_bot)
    await botApp.on_project(dummy, bot=fake_bot)

    # проверим, что текст pdf_sent и документ отправлены
    assert any("подборка" in s for s in dummy.sent if isinstance(s, str))
    assert any(s == "doc" for s in dummy.sent)


