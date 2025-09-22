from datetime import datetime
from freezegun import freeze_time

from botApp import schedule_followup, update_user_fields, init_db, upsert_user


def setup_function():
    init_db()


@freeze_time("2025-01-01 10:00:00")
def test_schedule_followup_sets_time(monkeypatch):
    # заглушим add_job, чтобы не создавать реальную задачу
    calls = {}
    def fake_add_job(func, trigger, args, id, replace_existing, misfire_grace_time):
        calls["id"] = id
        calls["run_date"] = trigger.run_date
        calls["args"] = args
        return None

    import botApp
    # гарантируем, что функция schedule_followup вызовет наш хук
    monkeypatch.setattr(botApp.scheduler, "add_job", fake_add_job, raising=True)

    chat_id = 999
    # создаём пользователя, иначе schedule_followup завершится ранее
    upsert_user(chat_id, "test", "User")
    update_user_fields(chat_id, followup_attempts=0)
    schedule_followup(chat_id, initial=True)

    # убедимся, что add_job был вызван
    assert "args" in calls and calls["args"] == [chat_id]
    assert isinstance(calls["run_date"], datetime)

