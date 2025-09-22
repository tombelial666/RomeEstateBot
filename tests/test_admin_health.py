import pytest

import botApp


@pytest.mark.asyncio
async def test_admin_health(monkeypatch):
    # подменим Bot.get_me на заглушку
    class FakeMe:
        username = "test_bot"

    class FakeBot:
        async def get_me(self):
            return FakeMe()
        async def session(self):
            return None

    monkeypatch.setattr(botApp, "Bot", lambda token=None: FakeBot())

    # вызовем внутренний healthcheck напрямую
    await botApp.async_healthcheck()
    # если не бросило исключений — ок
    assert True


