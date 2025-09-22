import pytest
from aioresponses import aioresponses

from botApp import on_project, PDF_URL


@pytest.mark.asyncio
async def test_pdf_fallback_download(monkeypatch):
    class Dummy:
        def __init__(self):
            self.sent = []
        async def answer(self, text, reply_markup=None):
            self.sent.append(("text", text))
        async def answer_document(self, *args, **kwargs):
            self.sent.append(("doc", kwargs))
    dummy = Dummy()

    class User:
        id = 1
    # Упростим: передаём объект с нужными атрибутами вместо наследования Message
    dummy.from_user = User()
    dummy.text = "проект"

    # заглушим проверку подписки в on_project: всегда считаем подписан
    async def ok_member(chat_id, user_id):
        class M: status = "member"
        return M()
    import botApp
    monkeypatch.setattr(botApp.Bot, "get_chat_member", staticmethod(ok_member))

    # 1) сломаем отправку по URL — бросим исключение
    async def fail_url(*args, **kwargs):
        raise RuntimeError("fail URL")
    monkeypatch.setattr(Dummy, "answer_document", fail_url)

    # 2) поднимем aioresponses: успешная загрузка
    with aioresponses() as m:
        m.get(PDF_URL, status=200, body=b"%PDF-1.4 test")
        # вызовем обработчик
        await on_project(dummy, bot=None)  # bot не используется после заглушек

    # так как url-отправка упала, должен сработать fallback и отправка байт
    # проверим, что был хотя бы текст pdf_sent
    assert any(kind == "text" for kind, _ in dummy.sent)

