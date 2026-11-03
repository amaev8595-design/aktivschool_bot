import asyncio
import os

from db import initialize_database
from classes import SCHOOL_CLASSES, format_rating
from maxo import Bot, Dispatcher
from maxo.routing.filters import CommandStart
from maxo.types import MessageCreated
from maxo.utils.builders import KeyboardBuilder


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Переменная {name} не заполнена в файле .env")
    return value


bot = Bot(required_env("MAX_BOT_TOKEN"))
dispatcher = Dispatcher()


def main_keyboard():
    return (
        KeyboardBuilder()
        .add_message(text="🎯 Ценность месяца")
        .add_message(text="🏆 Рейтинг")
        .add_message(text="🎪 Афиша")
        .add_message(text="💡 Подать идею")
        .adjust(2, 2)
        .build()
    )


@dispatcher.message_created(CommandStart())
async def start_handler(message: MessageCreated) -> None:
    await message.answer(
        "Привет! Это бот проекта «Актив».\n"
        "Выберите раздел меню:",
        keyboard=main_keyboard(),
    )


@dispatcher.message_created()
async def echo_handler(message: MessageCreated) -> None:
    text = message.text or "Сообщение получено, но в нём нет текста."

    menu_messages = {
        "🎯 Ценность месяца": "🎯 Ценность месяца — Взаимопомощь.\nНастройку этого значения подключим к базе данных.",
        "🏆 Рейтинг": format_rating(SCHOOL_CLASSES),
        "🎪 Афиша": "🎪 Афиша пока пуста. Одобренные идеи будут появляться здесь.",
        "💡 Подать идею": "💡 Форма подачи идеи будет добавлена следующим этапом.",
    }

    if text in menu_messages:
        await message.answer(menu_messages[text], keyboard=main_keyboard())
        return

    await message.answer(f"Вы написали: {text}")


async def prepare_database() -> None:
    pool = await initialize_database()
    await pool.close()
    print("PostgreSQL подключён, схема базы данных проверена.")


if __name__ == "__main__":
    asyncio.run(prepare_database())
    dispatcher.run_polling(bot)

