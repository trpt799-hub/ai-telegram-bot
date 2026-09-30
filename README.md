# AI Telegram Bot

Телеграм-бот с нейросетью Google Gemini: отвечает на вопросы, помнит контекст диалога, работает в нескольких режимах и умеет разбирать фотографии.

## Возможности
- Диалог с памятью (последние 20 сообщений), сброс командой `/reset`
- Режимы: помощник, переводчик, репетитор, помощник по коду, конспектёр (`/mode`)
- Анализ фотографий: пришли картинку с подписью-вопросом
- Ограничение частоты запросов на пользователя
- Повторные попытки и запасная модель при перегрузке API

## Технологии
Python 3.10+, aiogram 3, google-genai

## Установка
```bash
git clone https://github.com/trp799-hub/ai-telegram-bot.git
cd ai-telegram-bot
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux / macOS
pip install -r requirements.txt
```

## Настройка
1. Создай бота у @BotFather и получи токен.
2. Получи ключ Gemini API в Google AI Studio (aistudio.google.com).
3. Скопируй `.env.example` в `.env` и заполни значения:

```
BOT_TOKEN=токен_от_BotFather
GEMINI_API_KEY=ключ_из_AI_Studio
MODEL=gemini-3.8-flash
FALLBACK_MODEL=gemini-3.5-flash-lite
```

## Запуск
```bash
python main.py
```

## Команды бота
| Команда | Что делает |
|---------|-----------|
| `/start` | Приветствие |
| `/mode` | Выбор режима работы |
| `/reset` | Очистка памяти диалога |
| `/help` | Справка |

## Важно
Файл `.env` с токенами нельзя публиковать. Он добавлен в `.gitignore`.