import os
import sqlite3
import re
from datetime import datetime

from botApp import init_db, DB_PATH, PROJECT_RE, update_user_fields, get_user, upsert_user, TZ


def setup_module(module):
    # использовать тестовый файл БД
    test_db = os.path.join(os.path.dirname(__file__), "test.db")
    os.environ["DB_PATH"] = test_db
    # пересоздаём пустую БД
    if os.path.exists(test_db):
        os.remove(test_db)
    init_db()


def teardown_module(module):
    test_db = os.environ.get("DB_PATH")
    if test_db and os.path.exists(test_db):
        os.remove(test_db)


def test_regex_project_variants():
    ok = [
        "проект", "Проект", "  ПРОЕКТ  ", "proekt", "project", "prоekt", # латиница с похожим символом
        "проекта", # допускаем хвост
    ]
    for s in ok:
        assert re.match(PROJECT_RE, s) is not None, s

    bad = ["про", "projectX", "random"]
    for s in bad:
        assert re.match(PROJECT_RE, s) is None, s


def test_db_upsert_and_update():
    chat_id = 123
    upsert_user(chat_id, "user", "Name")
    u = get_user(chat_id)
    assert u is not None
    assert u["username"] == "user"

    now = datetime.now(TZ).isoformat()
    update_user_fields(chat_id, last_message="hello", last_interaction=now)
    u2 = get_user(chat_id)
    assert u2["last_message"] == "hello"
    assert u2["last_interaction"] == now


