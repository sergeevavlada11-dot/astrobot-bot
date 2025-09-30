# AstroBot FINAL (Webhook, Render/Railway)

Функции:
- Сбор данных рождения (город, дата, время)
- Сферы: Личность, Деньги, Карьера, Отношения, Предназначение
- Подтемы: Общее описание, Прогноз на 5 лет, Советы по гармонизации
- Дружелюбные ответы с эмодзи ✨
- 1 бесплатная консультация → затем блокировка → разблокировка по коду
- /help — описание возможностей + кнопка оплаты
- /reset — только для оплативших, очищает историю (профиль и статус оплаты остаются)

Оплата:
- Кнопка в /help ведёт на https://pay.example.com

## Деплой (Render)
1. Загрузите файлы в GitHub.
2. На https://render.com → New → Web Service
   - Build: `pip install -r requirements.txt`
   - Start: `python main.py`
3. В Environment добавьте переменные:
   - `TELEGRAM_TOKEN`
   - `OPENAI_API_KEY`
   - После первого деплоя возьмите домен и добавьте `WEBHOOK_HOST=https://your-bot.onrender.com`
4. Restart — вебхук поставится автоматически.

## Деплой (Railway)
1. Подключите репозиторий к https://railway.app
2. Variables: `TELEGRAM_TOKEN`, `OPENAI_API_KEY`, `WEBHOOK_HOST`
3. Start Command: `python main.py`

## Чеклист запуска (5–7 минут)
- [ ] Создать репозиторий с `main.py`, `requirements.txt`, `README.md`
- [ ] Настроить сервис (Render/Railway) и подключить репозиторий
- [ ] Добавить переменные окружения
- [ ] Первый деплой → получить домен
- [ ] Добавить `WEBHOOK_HOST` → Restart
- [ ] В Telegram отправить боту /start и пройти сценарий

## Настройки
- Код разблокировки по умолчанию: `ASTROVIP` — можно поменять переменной окружения `UNLOCK_CODE`
- Модель: в `main.py` стоит `model="gpt-5"` — при отсутствии доступа замените на `gpt-4o-mini`
- База: SQLite (`astrobot.sqlite3`). Для постоянного хранения используйте внешний PostgreSQL.
