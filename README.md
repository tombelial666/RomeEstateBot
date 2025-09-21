## RomeEstateBot (aiogram v3)

Телеграм‑бот для фильтрации подписчиков канала, выдачи PDF и записи лидов в Google Sheets.

### Быстрый старт (локально)

1. Python 3.11+ и virtualenv:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Заполните `environment.ini` (примерные поля внутри файла). Важно:
- `BOT_TOKEN` — токен бота
- `CHANNEL_ID` — id канала вида `-100...`
- `CHANNEL_LINK` — `https://t.me/<username_канала>` или инвайт‑ссылка
- `GSHEET_ID` — id таблицы (без URL)
- `GOOGLE_SERVICE_JSON` — путь к `sa.json`

3. Сервисный аккаунт Google:
- Создайте ключ JSON и сохраните как `sa.json` рядом с кодом
- Дайте доступ `Editor` таблице Google Sheets по email из `sa.json`

4. Запуск:
```bash
python botApp.py
```

### Docker / Compose
```bash
docker compose up --build -d
```
Тома:
- `environment.ini` и `sa.json` монтируются в контейнер только для чтения
- `bot.db` хранится на хосте

### Webhook режим
Включите переменные в compose (или env) и пробросьте порт 8080:
```
WEBHOOK_URL=https://your.domain.com/telegram/webhook
WEBHOOK_PATH=/telegram/webhook
WEBHOOK_SECRET=WEBHOOK_SECRET
WEBAPP_HOST=0.0.0.0
WEBAPP_PORT=8080
```
Nginx-пример location:
```
location /telegram/ {
    proxy_pass http://127.0.0.1:8080/telegram/;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Host $host;
}
```

### Команды админа
- `/update_pdf <url>` — обновить ссылку на PDF
- `/force_followup <chat_id>` — поставить фоллоу‑ап
- `/export_leads` — выгрузить CSV из локальной БД
- `/manager_contacted <chat_id> [on|off]` — пометить контакт менеджера
- `/health` — проверить доступность
- `/chat_id` — показать текущий chat_id

### Шаблоны сообщений
Редактируйте тексты в `templates.py`.

### Примечания
- Для проверки подписки бот должен быть админом канала
- PDF с Google Drive — используйте прямой URL вида `uc?id=...&export=download`

