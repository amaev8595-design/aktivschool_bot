import asyncio
import os

from db import bind_user_to_class, get_user_class, initialize_database
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
pending_class_code_users: set[int] = set()


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
    user_id = message.user_id
    if user_id is None:
        await message.answer("Не удалось определить пользователя MAX. Попробуйте ещё раз.")
        return

    user_class = await get_user_class(user_id)
    if user_class is None:
        pending_class_code_users.add(user_id)
        await message.answer(
            "Добро пожаловать в «Актив»!\n"
            "Введите код приглашения вашего класса.\n"
            "Для демонстрации используйте код AKTIV-9A."
        )
        return

    await message.answer(
        f"Привет! Вы привязаны к классу {user_class[0]} ({user_class[1]}).\n"
        "Выберите раздел меню:",
        keyboard=main_keyboard(),
    )


@dispatcher.message_created()
async def echo_handler(message: MessageCreated) -> None:
    text = message.text or "Сообщение получено, но в нём нет текста."

    if message.user_id in pending_class_code_users:
        user_class = await bind_user_to_class(message.user_id, text)
        if user_class is None:
            await message.answer(
                "Такого кода нет. Проверьте код и отправьте его ещё раз."
            )
            return

        pending_class_code_users.remove(message.user_id)
        await message.answer(
            f"Готово! Вы привязаны к классу {user_class[0]} ({user_class[1]}).\n"
            "Теперь доступно главное меню:",
            keyboard=main_keyboard(),
        )
        return

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

