import asyncio
import pytest

import botApp


class FakeCell:
    def __init__(self, row: int):
        self.row = row


class FakeSpreadsheet:
    def __init__(self, ws):
        self._ws = ws
        self._updates = []

    def worksheet(self, name):
        return self._ws

    @property
    def updates(self):
        return self._updates

    def values_batch_update(self, body):
        self._updates.append(body)


class FakeWorksheet:
    def __init__(self):
        # заголовок, как в маппинге
        self._header = [
            "chat_id", "username", "first_name", "date_joined", "subscribed",
            "last_message", "file_sent", "followup_attempts", "manager_contacted"
        ]
        # первая строка — заголовок
        self._rows = [self._header[:]]
        self.spreadsheet = FakeSpreadsheet(self)

    def append_row(self, row, value_input_option=None):
        self._rows.append([str(v) for v in row])

    def row_values(self, idx: int):
        return self._rows[idx - 1]

    def find(self, value: str):
        for i, row in enumerate(self._rows[1:], start=2):
            if row and row[0] == value:
                return FakeCell(i)
        raise Exception("not found")


class FakeGC:
    def __init__(self, ws: FakeWorksheet):
        self._ws = ws

    def open_by_key(self, key):
        return FakeSpreadsheet(self._ws)


@pytest.mark.asyncio
async def test_gs_write_and_update(monkeypatch):
    ws = FakeWorksheet()
    gc = FakeGC(ws)

    monkeypatch.setattr(botApp, "_build_gspread_client", lambda: gc)

    user = {
        "chat_id": 111,
        "username": "u",
        "first_name": "f",
        "subscribed": 1,
        "last_message": "start",
        "followup_attempts": 0,
        "manager_contacted": 0,
    }

    await botApp.gs_write_new_user(user)
    # должна появиться новая строка (вторая, т.к. первая — шапка)
    assert len(ws._rows) == 2
    assert ws._rows[1][0] == str(user["chat_id"])  # chat_id

    # обновление существующей строки по chat_id
    await botApp.gs_update_by_chat_id(111, {"last_message": "project_requested"})
    # должен быть батч-апдейт
    assert ws.spreadsheet.updates, "batch update not called"

    # обновление отсутствующего chat_id — добавление строки
    await botApp.gs_update_by_chat_id(222, {"last_message": "hello"})
    assert any(row[0] == "222" for row in ws._rows[1:])


