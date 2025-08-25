ИНСТРУКЦИЯ:

1. Установи зависимости:
   pip install -r requirements.txt
   python -m playwright install

2. Запусти бота:
   python winbox_bot.py

3. Он будет проверять сайт каждую минуту.
   Если появятся слоты на 9 августа — ты получишь уведомление в Telegram.

ВАЖНО: если хочешь изменить частоту, правь строку:
   await asyncio.sleep(60)  ← число в секундах
