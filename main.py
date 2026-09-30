import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from io import BytesIO

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai import errors

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("MODEL", "gemini-3.8-flash")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "gemini-3.5-flash-lite")
MODELS = [m for m in (MODEL, FALLBACK_MODEL) if m]

if not BOT_TOKEN or not GEMINI_API_KEY:
    raise SystemExit("Заполни BOT_TOKEN и GEMINI_API_KEY в файле .env")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ai-bot")

client = genai.Client(api_key=GEMINI_API_KEY)
router = Router()

# ---------- Настройки ----------
MAX_HISTORY = 20          # сколько последних сообщений помним
RATE_LIMIT = 5            # запросов...
RATE_WINDOW = 60          # ...за столько секунд на одного пользователя
TG_LIMIT = 4096           # лимит длины сообщения в Telegram

MODES = {
    "default": {
        "title": "💬 Обычный помощник",
        "prompt": "Ты дружелюбный и полезный помощник. Отвечай чётко и по делу "
                  "на том языке, на котором пишет пользователь.",
    },
    "translator": {
        "title": "🌍 Переводчик",
        "prompt": "Ты профессиональный переводчик. Если текст на русском, переведи "
                  "на английский; если на другом языке, переведи на русский. "
                  "Выдавай только перевод без лишних комментариев. "
                  "Если пользователь указал язык явно, переводи на него.",
    },
    "tutor": {
        "title": "🎓 Репетитор",
        "prompt": "Ты терпеливый репетитор. Объясняй простыми словами, с примерами, "
                  "по шагам. В конце задай короткий проверочный вопрос.",
    },
    "coder": {
        "title": "👨‍💻 Помощник по коду",
        "prompt": "Ты опытный разработчик. Пиши рабочий, аккуратный код, коротко "
                  "объясняй решение, указывай на возможные ошибки и edge cases.",
    },
    "notes": {
        "title": "📝 Конспектёр",
        "prompt": "Ты делаешь краткие структурированные конспекты. Выделяй главное, "
                  "используй списки, убирай воду.",

    },
     

}

BASE_RULES = (
    "Отвечай кратко, по делу, без вступлений и заключений. "
    "Делай только то, о чём просит пользователь, ничего не добавляй от себя "
    "и не предлагай лишнего. Ответ обычно укладывается в 3-6 предложений, "
    "если пользователь не просит подробнее.")




# ---------- Состояние пользователей (в памяти) ----------
# история: [{"role": "user" | "model", "text": "..."}]
history: dict[int, list[dict]] = defaultdict(list)
user_mode: dict[int, str] = defaultdict(lambda: "default")
request_times: dict[int, deque] = defaultdict(deque)
locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def is_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    q = request_times[user_id]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def trim_history(user_id: int) -> None:
    h = history[user_id]
    if len(h) > MAX_HISTORY:
        del h[: len(h) - MAX_HISTORY]
    # история должна начинаться с сообщения пользователя
    while h and h[0]["role"] != "user":
        h.pop(0)


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def modes_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=m["title"], callback_data=f"mode:{key}")]
        for key, m in MODES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def generate_with_retry(contents, system_prompt):
    """Пробует основную модель 3 раза, при проблемах переходит на запасную."""
    last_error = None
    for model in MODELS:
        for attempt in range(3):
            try:
                return await client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                    ),
                )
            except errors.ServerError as e:  # 5xx: перегрузка, пробуем ещё раз
                last_error = e
                log.warning("%s: сервер занят (попытка %d)", model, attempt + 1)
                await asyncio.sleep(2 * (attempt + 1))
            except errors.ClientError as e:
                last_error = e
                if e.code in (404, 429):  # модель недоступна или лимит: к следующей
                    log.warning("%s: ошибка %s, пробую другую модель", model, e.code)
                    break
                raise
    raise last_error

async def ask_gemini(user_id: int, current_parts: list, history_text: str) -> str:
    """current_parts: части текущего сообщения (текст и/или картинка).
    history_text: что сохранить в историю (без картинки)."""
    contents = [
        types.Content(role=m["role"], parts=[types.Part.from_text(text=m["text"])])
        for m in history[user_id]
    ]
    contents.append(types.Content(role="user", parts=current_parts))

    system_prompt = BASE_RULES + "\n\n" + MODES[user_mode[user_id]]["prompt"]
    resp = await generate_with_retry(contents, system_prompt)
    answer = (resp.text or "").strip()
    if not answer:
        log.warning(
            "ПУСТОЙ ОТВЕТ. candidates=%s feedback=%s",
            resp.candidates,
            resp.prompt_feedback,
        )
        answer = "Не получилось сформировать ответ, попробуй переформулировать."
    else:
        log.info("Ответ получен, символов: %d", len(answer))
    history[user_id].append({"role": "user", "text": history_text})
    history[user_id].append({"role": "model", "text": answer})
    trim_history(user_id)
    return answer


async def process(message: Message, bot: Bot, current_parts: list, history_text: str):
    user_id = message.from_user.id

    if is_rate_limited(user_id):
        await message.answer("⏳ Слишком много запросов, подожди минуту.")
        return

    # один запрос за раз на пользователя, чтобы история не ломалась
    async with locks[user_id]:
        try:
            async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
                answer = await ask_gemini(user_id, current_parts, history_text)
        except Exception as e:
            log.exception("Ошибка при обращении к API")
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                await message.answer("⚠️ Достигнут лимит бесплатного API, попробуй позже.")
            elif "503" in err or "UNAVAILABLE" in err:
                await message.answer("⚠️ Gemini сейчас перегружен, попробуй через минуту.")
            else:
                await message.answer("⚠️ Что-то пошло не так, попробуй ещё раз позже.")
            return

    for part in split_text(answer):
        await message.answer(part)


# ---------- Команды ----------
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я AI-бот. Просто напиши мне вопрос или отправь фото.\n\n"
        "/mode — выбрать режим (переводчик, репетитор, код, конспекты)\n"
        "/reset — очистить память диалога\n"
        "/help — помощь"
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Что я умею:\n"
        "• отвечать на вопросы и помнить контекст диалога\n"
        "• работать в разных режимах (/mode)\n"
        "• разбирать фотографии (пришли фото с подписью-вопросом)\n\n"
        "/reset — начать диалог заново"
    )


@router.message(Command("mode"))
async def cmd_mode(message: Message):
    current = MODES[user_mode[message.from_user.id]]["title"]
    await message.answer(
        f"Текущий режим: {current}\nВыбери новый:", reply_markup=modes_keyboard()
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    history[message.from_user.id].clear()
    await message.answer("🧹 Память диалога очищена.")


@router.callback_query(F.data.startswith("mode:"))
async def on_mode(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    if key not in MODES:
        await callback.answer("Неизвестный режим")
        return
    user_id = callback.from_user.id
    user_mode[user_id] = key
    history[user_id].clear()  # при смене режима начинаем с чистого листа
    await callback.message.edit_text(f"Режим переключён: {MODES[key]['title']}")
    await callback.answer()


# ---------- Обработка сообщений ----------
@router.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    buf = BytesIO()
    await bot.download(message.photo[-1], destination=buf)
    caption = message.caption or "Опиши, что на этой картинке."

    parts = [
        types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
        types.Part.from_text(text=caption),
    ]
    await process(message, bot, parts, f"[фото] {caption}")


@router.message(F.text)
async def on_text(message: Message, bot: Bot):
    parts = [types.Part.from_text(text=message.text)]
    await process(message, bot, parts, message.text)


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())