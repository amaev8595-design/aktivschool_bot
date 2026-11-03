import asyncio
import os

from db import initialize_database
from maxo import Bot, Dispatcher
from maxo.routing.filters import CommandStart
from maxo.types import MessageCreated


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Переменная {name} не заполнена в файле .env")
    return value


bot = Bot(required_env("MAX_BOT_TOKEN"))
dispatcher = Dispatcher()


@dispatcher.message_created(CommandStart())
async def start_handler(message: MessageCreated) -> None:
    await message.answer(
        "Привет! Это бот проекта «Актив».\n"
        "Пока работает тестовый режим."
    )


@dispatcher.message_created()
async def echo_handler(message: MessageCreated) -> None:
    text = message.text or "Сообщение получено, но в нём нет текста."
    await message.answer(f"Вы написали: {text}")


async def prepare_database() -> None:
    pool = await initialize_database()
    await pool.close()
    print("PostgreSQL подключён, схема базы данных проверена.")


if __name__ == "__main__":
    asyncio.run(prepare_database())
    dispatcher.run_polling(bot)

