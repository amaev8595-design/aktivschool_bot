import asyncio
import os
from datetime import date

from db import (
    bind_user_to_class,
    create_class,
    create_idea,
    create_event,
    create_report,
    ensure_user_role,
    get_admin_ids,
    get_moderator_ids,
    get_setting_text,
    get_user_role,
    get_user_class,
    initialize_database,
    list_classes_with_codes,
    list_events,
    list_pending_reports,
    list_pending_ideas,
    list_rating,
    list_users,
    moderate_idea,
    moderate_report,
    set_user_role,
    set_setting,
    signup_for_event,
)
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


def media_tokens(message: MessageCreated) -> str | None:
    tokens: list[str] = []
    for attachment in message.message.body.photo:
        tokens.append(attachment.payload.token)
    for attachment in message.message.body.video:
        tokens.append(attachment.payload.token)
    return ",".join(tokens) if tokens else None


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


async def is_admin_user(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if user_id in get_admin_ids():
        return True
    return await get_user_role(user_id) == "admin"


async def is_moderator_user(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if user_id in get_admin_ids() or user_id in get_moderator_ids():
        return True
    return await get_user_role(user_id) in {"admin", "moderator"}


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
    if not await is_admin_user(message.user_id):
        await message.answer("У вас нет прав администратора.")
        return
    await ensure_user_role(message.user_id, "admin")
    await message.answer(
        "⚙️ Панель администратора\n"
        "Роль подтверждена на стороне сервера.\n\n"
        "Команды:\n"
        "/setvalue Текст ценности месяца\n"
        "/addclass Название|группа|код\n"
        "/addevent Название|Описание|YYYY-MM-DD|Класс\n"
        "Учитель: /addevent Название|Описание|YYYY-MM-DD\n"
        "/classes — список классов и кодов\n"
        "/users — список пользователей и ролей\n"
        "/setrole ID moderator|teacher|student\n"
        "/events — список мероприятий\n"
        "/signup Номер — запись класса\n"
        "/report Номер|Комментарий — отправить отчёт\n"
        "/reports — очередь отчётов\n"
        "/approve_report Номер\n"
        "/reject_report Номер Причина\n"
        "/ideas — очередь идей\n"
        "/approve Номер\n"
        "/reject Номер Комментарий"
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

    is_admin = await is_admin_user(user_id)
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
            keyboard=main_keyboard(await is_admin_user(message.user_id)),
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
                keyboard=main_keyboard(await is_admin_user(message.user_id)),
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
            keyboard=main_keyboard(await is_admin_user(message.user_id)),
        )
        return

    if text == "/classes":
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        classes = await list_classes_with_codes()
        lines = ["📚 Классы и коды приглашения:"]
        lines.extend(f"• {name} ({age_group}) — {code}" for name, age_group, code in classes)
        await message.answer("\n".join(lines))
        return

    if text == "/users":
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        users = await list_users()
        if not users:
            await message.answer("Пользователей пока нет.")
            return
        lines = ["👥 Пользователи и роли:"]
        for max_user_id, role, class_name in users:
            lines.append(
                f"{max_user_id} — {role} — класс: {class_name or 'не привязан'}"
            )
        await message.answer("\n".join(lines))
        return

    if text == "/setrole" or text.startswith("/setrole "):
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        parts = text.split()
        allowed_roles = {"student", "teacher", "moderator"}
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in allowed_roles:
            await message.answer(
                "Формат: /setrole ID moderator|teacher|student\n"
                "Пример: /setrole 123456 moderator"
            )
            return
        target_id = int(parts[1])
        if target_id in get_admin_ids():
            await message.answer("Пользователь из ADMIN_IDS не может быть понижен этой командой.")
            return
        await set_user_role(target_id, parts[2])
        await message.answer(f"Для пользователя {target_id} назначена роль: {parts[2]}.")
        return

    if text == "/setvalue" or text.startswith("/setvalue "):
        if not await is_admin_user(message.user_id):
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
        if not await is_admin_user(message.user_id):
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

    if text == "/addevent" or text.startswith("/addevent "):
        is_admin = await is_admin_user(message.user_id)
        user_role = await get_user_role(message.user_id) if message.user_id is not None else None
        if not is_admin and user_role != "teacher":
            await message.answer("Добавлять мероприятия может только администратор или учитель.")
            return
        payload = text.removeprefix("/addevent").strip()
        parts = [part.strip() for part in payload.split("|")]
        if is_admin:
            if len(parts) != 4 or not all(parts):
                await message.answer(
                    "Формат администратора: /addevent Название|Описание|YYYY-MM-DD|Класс\n"
                    "Пример: /addevent Квест благодарности|Командная игра|2026-11-08|9А"
                )
                return
            author_class_name = parts[3]
        else:
            if len(parts) != 3 or not all(parts):
                await message.answer(
                    "Формат учителя: /addevent Название|Описание|YYYY-MM-DD\n"
                    "Мероприятие будет создано для привязанного класса."
                )
                return
            user_class = await get_user_class(message.user_id)
            if user_class is None:
                await message.answer("Сначала привяжите учителя к классу через /start.")
                return
            author_class_name = user_class[0]
        try:
            event_date = date.fromisoformat(parts[2])
        except ValueError:
            await message.answer("Дата должна быть в формате YYYY-MM-DD, например 2026-11-08.")
            return
        event_id = await create_event(parts[0], parts[1], event_date, author_class_name)
        if event_id is None:
            await message.answer("Класс не найден. Сначала проверьте список командой /classes.")
            return
        await message.answer(
            f"Мероприятие №{event_id} добавлено в афишу.\n"
            "Пользователи увидят его через кнопку «🎪 Афиша»."
        )
        return

    if text == "/events" or text == "🎪 Афиша":
        events = await list_events()
        if not events:
            await message.answer(
                "🎪 Афиша пока пуста. Администратор может добавить мероприятие командой /addevent."
            )
            return
        lines = ["🎪 Афиша мероприятий:"]
        for event_id, title, description, event_date, class_name in events:
            lines.append(
                f"\n№{event_id} · {event_date} · {title}\n"
                f"{description}\n"
                f"Организатор: класс {class_name}\n"
                f"Запись класса: /signup {event_id}"
            )
        await message.answer(
            "\n".join(lines),
            keyboard=main_keyboard(await is_admin_user(message.user_id)),
        )
        return

    if text == "/signup" or text.startswith("/signup "):
        parts = text.split(maxsplit=1)
        if message.user_id is None or len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Формат: /signup Номер мероприятия")
            return
        result = await signup_for_event(message.user_id, int(parts[1]))
        responses = {
            "not_bound": "Сначала привяжите пользователя к классу через /start.",
            "not_found": "Мероприятие не найдено или уже недоступно.",
            "already": "Ваш класс уже записан на это мероприятие.",
            "ok": "Готово! Ваш класс записан на мероприятие.",
        }
        await message.answer(responses[result])
        return

    if text == "/report" or text.startswith("/report "):
        if message.user_id is None:
            await message.answer("Не удалось определить пользователя MAX.")
            return
        payload = text.removeprefix("/report").strip()
        parts = [part.strip() for part in payload.split("|", maxsplit=1)]
        attachment_token = media_tokens(message)
        if (
            len(parts) not in {1, 2}
            or not parts[0].isdigit()
            or (len(parts) == 1 and not attachment_token)
            or (len(parts) == 2 and not parts[1] and not attachment_token)
        ):
            await message.answer(
                "Формат: /report Номер мероприятия|Комментарий\n"
                "Пример: /report 1|Провели мероприятие, участвовали 18 учеников.\n"
                "Фото или видео можно прикрепить к этому же сообщению."
            )
            return
        comment = parts[1] if len(parts) == 2 and parts[1] else None
        if comment and len(comment) > 2000:
            await message.answer("Комментарий отчёта должен быть не длиннее 2000 символов.")
            return
        result, report_id = await create_report(
            message.user_id,
            int(parts[0]),
            comment,
            attachment_token,
        )
        responses = {
            "not_bound": "Сначала привяжите пользователя к классу через /start.",
            "not_signed": "Ваш класс не записан на это мероприятие.",
        }
        if result != "ok":
            await message.answer(responses[result])
            return
        media_note = " Фото/видео сохранено." if attachment_token else ""
        await message.answer(
            f"Отчёт №{report_id} отправлен на проверку модератору.{media_note}"
        )
        return

    is_moderator = await is_moderator_user(message.user_id)

    if text == "/ideas":
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        ideas = await list_pending_ideas()
        if not ideas:
            await message.answer("Очередь идей пуста.")
            return
        lines = ["📝 Идеи на модерации:"]
        for idea_id, title, description, target_group, class_name in ideas:
            lines.append(
                f"\n№{idea_id} от класса {class_name}\n"
                f"{title}\n{description}\nДля: {target_group}"
            )
        await message.answer("\n".join(lines))
        return

    if text == "/approve" or text.startswith("/approve "):
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Формат: /approve Номер")
            return
        changed, status = await moderate_idea(int(parts[1]), message.user_id, "approved")
        if not changed:
            await message.answer("Идея не найдена или уже обработана.")
            return
        await message.answer("Идея одобрена. Классу начислено 10 баллов.")
        return

    if text == "/reject" or text.startswith("/reject "):
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        parts = text.split(maxsplit=2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].strip():
            await message.answer("Формат: /reject Номер Причина отклонения")
            return
        changed, status = await moderate_idea(
            int(parts[1]),
            message.user_id,
            "rejected",
            parts[2].strip(),
        )
        if not changed:
            await message.answer("Идея не найдена или уже обработана.")
            return
        await message.answer("Идея отклонена. Комментарий сохранён.")
        return

    if text == "/reports":
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        reports = await list_pending_reports()
        if not reports:
            await message.answer("Очередь отчётов пуста.")
            return
        lines = ["📋 Отчёты на проверке:"]
        for report_id, event_id, title, class_name, comment, media_token in reports:
            lines.append(
                f"\nОтчёт №{report_id}, мероприятие №{event_id}: {title}\n"
                f"Класс: {class_name}\n{comment}"
                + ("\nПрикреплён медиа-файл." if media_token else "")
            )
        await message.answer("\n".join(lines))
        return

    if text == "/approve_report" or text.startswith("/approve_report "):
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Формат: /approve_report Номер отчёта")
            return
        changed, status = await moderate_report(
            int(parts[1]), message.user_id, "approved"
        )
        if not changed:
            await message.answer("Отчёт не найден или уже обработан.")
            return
        await message.answer("Отчёт подтверждён. Классу начислено 5 баллов.")
        return

    if text == "/reject_report" or text.startswith("/reject_report "):
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        parts = text.split(maxsplit=2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].strip():
            await message.answer("Формат: /reject_report Номер Причина отклонения")
            return
        changed, status = await moderate_report(
            int(parts[1]), message.user_id, "rejected", parts[2].strip()
        )
        if not changed:
            await message.answer("Отчёт не найден или уже обработан.")
            return
        await message.answer("Отчёт отклонён. Комментарий сохранён.")
        return

    if text == "🏆 Рейтинг":
        rating = await list_rating()
        lines = ["🏆 Рейтинг классов:"]
        for position, (name, age_group, points) in enumerate(rating, start=1):
            lines.append(f"{position}. {name} ({age_group}) — {points} баллов")
        await message.answer(
            "\n".join(lines),
            keyboard=main_keyboard(await is_admin_user(message.user_id)),
        )
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

