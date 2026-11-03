import asyncio
import os

from db import (
    bind_user_to_class,
    create_class,
    create_idea,
    ensure_user_role,
    get_admin_ids,
    get_setting_text,
    get_user_class,
    initialize_database,
    list_classes_with_codes,
    set_setting,
)
from classes import SCHOOL_CLASSES, format_rating
from maxo import Bot, Dispatcher
from maxo.routing.filters import Command, CommandStart
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
pending_ideas: dict[int, dict[str, str]] = {}


def main_keyboard(is_admin: bool = False):
    keyboard = (
        KeyboardBuilder()
        .add_message(text="🎯 Ценность месяца")
        .add_message(text="🏆 Рейтинг")
        .add_message(text="🎪 Афиша")
        .add_message(text="💡 Подать идею")
    )
    if is_admin:
        keyboard.add_message(text="⚙️ Администрирование")
        return keyboard.adjust(2, 2, 1).build()
    return keyboard.adjust(2, 2).build()


@dispatcher.message_created(Command("myid"))
async def my_id_handler(message: MessageCreated) -> None:
    if message.user_id is None:
        await message.answer("Не удалось определить ID пользователя MAX.")
        return
    await message.answer(
        f"Ваш технический ID MAX: {message.user_id}\n"
        "Не публикуйте его. Он нужен только для ADMIN_IDS в локальном .env."
    )


async def admin_message(message: MessageCreated) -> None:
    if message.user_id is None or message.user_id not in get_admin_ids():
        await message.answer("У вас нет прав администратора.")
        return
    await ensure_user_role(message.user_id, "admin")
    await message.answer(
        "⚙️ Панель администратора\n"
        "Роль подтверждена на стороне сервера.\n\n"
        "Команды:\n"
        "/setvalue Текст ценности месяца\n"
        "/addclass Название|группа|код\n"
        "/classes — список классов и кодов"
    )


@dispatcher.message_created(Command("admin"))
async def admin_command_handler(message: MessageCreated) -> None:
    await admin_message(message)


@dispatcher.message_created(CommandStart())
async def start_handler(message: MessageCreated) -> None:
    user_id = message.user_id
    if user_id is None:
        await message.answer("Не удалось определить пользователя MAX. Попробуйте ещё раз.")
        return

    is_admin = user_id in get_admin_ids()
    if is_admin:
        await ensure_user_role(user_id, "admin")
        await message.answer(
            "Привет! Вы вошли как администратор проекта «Актив».\n"
            "Выберите раздел меню:",
            keyboard=main_keyboard(is_admin=True),
        )
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

    if text.strip().lower() == "/cancel":
        pending_class_code_users.discard(message.user_id)
        pending_ideas.pop(message.user_id, None)
        await message.answer(
            "Текущий сценарий отменён.",
            keyboard=main_keyboard(message.user_id in get_admin_ids()),
        )
        return

    if message.user_id in pending_ideas:
        idea = pending_ideas[message.user_id]
        value = text.strip()

        if idea["step"] == "title":
            if not value or len(value) > 120:
                await message.answer("Название должно быть от 1 до 120 символов. Попробуйте ещё раз.")
                return
            idea["title"] = value
            idea["step"] = "description"
            await message.answer("Шаг 2 из 3. Опишите идею (до 1000 символов).")
            return

        if idea["step"] == "description":
            if not value or len(value) > 1000:
                await message.answer("Описание должно быть от 1 до 1000 символов. Попробуйте ещё раз.")
                return
            idea["description"] = value
            idea["step"] = "target_group"
            await message.answer("Шаг 3 из 3. Для кого подходит идея? Например: 5-8 классы.")
            return

        if idea["step"] == "target_group":
            if not value or len(value) > 100:
                await message.answer("Укажите группу длиной до 100 символов.")
                return
            idea_id = await create_idea(
                message.user_id,
                idea["title"],
                idea["description"],
                value,
            )
            if idea_id is None:
                await message.answer("Не удалось сохранить идею. Сначала привяжите пользователя к классу.")
                return
            pending_ideas.pop(message.user_id, None)
            await message.answer(
                f"Идея №{idea_id} отправлена на модерацию.\n"
                "Решение модератора появится в уведомлениях.",
                keyboard=main_keyboard(message.user_id in get_admin_ids()),
            )
            return

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

    if text == "⚙️ Администрирование":
        await admin_message(message)
        return

    if text == "🎯 Ценность месяца":
        value = await get_setting_text("monthly_value", "Взаимопомощь")
        await message.answer(
            f"🎯 Ценность месяца: {value}",
            keyboard=main_keyboard(message.user_id in get_admin_ids()),
        )
        return

    if text == "/classes":
        if message.user_id not in get_admin_ids():
            await message.answer("У вас нет прав администратора.")
            return
        classes = await list_classes_with_codes()
        lines = ["📚 Классы и коды приглашения:"]
        lines.extend(f"• {name} ({age_group}) — {code}" for name, age_group, code in classes)
        await message.answer("\n".join(lines))
        return

    if text == "/setvalue" or text.startswith("/setvalue "):
        if message.user_id not in get_admin_ids():
            await message.answer("У вас нет прав администратора.")
            return
        value = text.removeprefix("/setvalue").strip()
        if not value:
            await message.answer("Формат: /setvalue Текст ценности месяца")
            return
        await ensure_user_role(message.user_id, "admin")
        await set_setting("monthly_value", value)
        await message.answer(f"Ценность месяца изменена: {value}")
        return

    if text == "/addclass" or text.startswith("/addclass "):
        if message.user_id not in get_admin_ids():
            await message.answer("У вас нет прав администратора.")
            return
        payload = text.removeprefix("/addclass").strip()
        parts = [part.strip() for part in payload.split("|")]
        if len(parts) != 3 or parts[1] not in {"1-4", "5-8", "9-11"}:
            await message.answer(
                "Формат: /addclass Название|группа|код\n"
                "Пример: /addclass 11А|9-11|AKTIV-11A"
            )
            return
        created = await create_class(parts[0], parts[1], parts[2])
        if not created:
            await message.answer("Не удалось добавить класс: имя или код уже используются.")
            return
        await message.answer(f"Класс {parts[0]} добавлен. Код: {parts[2].upper()}")
        return

    if text == "💡 Подать идею":
        if message.user_id is None or await get_user_class(message.user_id) is None:
            await message.answer("Сначала привяжите пользователя к классу через /start.")
            return
        pending_ideas[message.user_id] = {"step": "title"}
        await message.answer(
            "Шаг 1 из 3. Введите название идеи (до 120 символов).\n"
            "Для отмены отправьте /cancel."
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

