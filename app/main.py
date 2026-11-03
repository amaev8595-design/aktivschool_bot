import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI

from db import (
    award_prize,
    bind_user_to_class,
    create_class,
    create_idea,
    create_event,
    create_report,
    ensure_user_role,
    get_admin_ids,
    get_class_report,
    get_moderator_ids,
    get_setting_text,
    get_idea_author_id,
    get_class_place,
    get_class_team,
    get_user_role,
    get_user_class,
    get_report_submitter_id,
    initialize_database,
    list_classes_with_codes,
    list_events,
    list_pending_reports,
    list_pending_ideas,
    list_rating,
    list_signed_up_events,
    list_user_ids_by_roles,
    list_users,
    moderate_idea,
    moderate_report,
    revise_report,
    set_user_role,
    set_setting,
    signup_for_event,
)
from maxo import Bot, Dispatcher
from maxo.routing.filters import Command, CommandStart
from maxo.types import MessageCreated
from maxo.transport.webhook.adapters.fastapi import FastApiWebAdapter
from maxo.transport.webhook.engines.simple import SimpleEngine
from maxo.transport.webhook.routing.static import StaticRouting
from maxo.transport.webhook.security.secret_token import StaticSecretToken
from maxo.utils.builders import KeyboardBuilder
import uvicorn


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Переменная {name} не заполнена в файле .env")
    return value


bot = Bot(required_env("MAX_BOT_TOKEN"))
dispatcher = Dispatcher()
pending_class_code_users: set[int] = set()
pending_ideas: dict[int, dict[str, str]] = {}
pending_admin_actions: dict[int, str] = {}
pending_admin_forms: dict[int, dict[str, str]] = {}
pending_event_selection_users: set[int] = set()
pending_report_selection_users: set[int] = set()
pending_user_reports: dict[int, dict[str, str]] = {}
BOT_MODE = os.getenv("BOT_MODE", "polling").strip().lower()


def media_tokens(message: MessageCreated) -> str | None:
    tokens: list[str] = []
    for attachment in message.message.body.photo:
        tokens.append(attachment.payload.token)
    for attachment in message.message.body.video:
        tokens.append(attachment.payload.token)
    return ",".join(tokens) if tokens else None


async def notify_user(user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id=user_id, text=text)
    except Exception as error:  # noqa: BLE001 - уведомление не должно останавливать бота
        print(f"Не удалось отправить уведомление пользователю {user_id}: {error}")


async def notify_moderators(text: str) -> None:
    user_ids = set(get_admin_ids()) | set(get_moderator_ids())
    user_ids.update(await list_user_ids_by_roles({"admin", "moderator"}))
    await asyncio.gather(*(notify_user(user_id, text) for user_id in user_ids))


def main_keyboard(is_admin: bool = False):
    keyboard = (
        KeyboardBuilder()
        .add_message(text="🎯 Ценность месяца")
        .add_message(text="🏆 Рейтинг")
        .add_message(text="🎪 Афиша")
        .add_message(text="💡 Подать идею")
        .add_message(text="📍 Место")
        .add_message(text="👥 Команда")
    )
    if is_admin:
        keyboard.add_message(text="⚙️ Администрирование")
        return keyboard.adjust(2, 2, 1).build()
    return keyboard.adjust(2, 2).build()


def admin_keyboard():
    keyboard = (
        KeyboardBuilder()
        .add_message(text="⚙️ Ценность месяца")
        .add_message(text="➕ Добавить класс")
        .add_message(text="📅 Добавить мероприятие")
        .add_message(text="📚 Классы")
        .add_message(text="👥 Пользователи")
        .add_message(text="🛡 Назначить роль")
        .add_message(text="🎪 Мероприятия")
        .add_message(text="💡 Очередь идей")
        .add_message(text="📋 Очередь отчётов")
        .add_message(text="✅ Одобрить идею")
        .add_message(text="❌ Отклонить идею")
        .add_message(text="✅ Одобрить отчёт")
        .add_message(text="❌ Отклонить отчёт")
        .add_message(text="🏅 Призовые баллы")
        .add_message(text="⚙️ Лимиты")
        .add_message(text="↩️ Главное меню")
    )
    return keyboard.adjust(2, 2, 2, 2, 2, 2, 2, 2).build()


def class_group_keyboard():
    keyboard = (
        KeyboardBuilder()
        .add_message(text="1–4 классы")
        .add_message(text="5–8 классы")
        .add_message(text="9–11 классы")
        .add_message(text="↩️ Отмена")
    )
    return keyboard.adjust(1, 1, 1, 1).build()


def class_confirmation_keyboard():
    keyboard = (
        KeyboardBuilder()
        .add_message(text="✅ Завершить добавление")
        .add_message(text="✏️ Изменить")
        .add_message(text="↩️ Отмена")
    )
    return keyboard.adjust(1, 1, 1).build()


def cancel_keyboard():
    return KeyboardBuilder().add_message(text="↩️ Отмена").adjust(1).build()


def options_keyboard(*labels: str):
    keyboard = KeyboardBuilder()
    for label in labels:
        keyboard.add_message(text=label)
    return keyboard.adjust(1).build()


def report_actions_keyboard():
    return options_keyboard("✅ Одобрить отчёт", "❌ Отклонить отчёт")


def report_confirmation_keyboard(confirm_label: str):
    return options_keyboard(confirm_label, "↩️ Отмена")


def context_keyboard(context: str):
    keyboards = {
        "value": options_keyboard("⚙️ Ценность месяца", "↩️ Администрирование"),
        "class": options_keyboard(
            "➕ Добавить класс", "📚 Классы", "↩️ Администрирование"
        ),
        "event": options_keyboard(
            "📅 Добавить мероприятие", "🎪 Мероприятия", "↩️ Администрирование"
        ),
        "role": options_keyboard(
            "🛡 Назначить роль", "👥 Пользователи", "↩️ Администрирование"
        ),
        "idea": options_keyboard("✅ Одобрить идею", "❌ Отклонить идею"),
        "report": report_actions_keyboard(),
        "award": options_keyboard("🏅 Призовые баллы", "↩️ Администрирование"),
        "limit": options_keyboard("⚙️ Лимиты", "↩️ Администрирование"),
    }
    return keyboards[context]


def user_context_keyboard(*labels: str, is_admin: bool = False):
    buttons = list(labels) + ["↩️ Главное меню"]
    if is_admin:
        buttons.append("⚙️ Администрирование")
    return options_keyboard(*buttons)


def event_entry_keyboard(is_admin: bool = False):
    return user_context_keyboard(
        "🤝 Принять участие",
        "📋 Подать отчёт",
        is_admin=is_admin,
    )


def event_selection_keyboard(events: list[tuple[int, str, str, str, str]]):
    return options_keyboard(
        *(f"№{event_id}" for event_id, *_ in events),
        "↩️ Назад",
    )


def report_selection_keyboard(events: list[tuple[int, str, str, str, str]]):
    return options_keyboard(
        *(f"📋 Отчёт №{event_id}" for event_id, *_ in events),
        "↩️ Назад",
    )


def user_report_confirmation_keyboard():
    return options_keyboard(
        "✅ Отправить отчёт",
        "✏️ Изменить комментарий",
        "↩️ Отмена",
    )


def confirmation_keyboard(confirm_label: str):
    return options_keyboard(confirm_label, "✏️ Изменить", "↩️ Отмена")


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
        "Выберите нужное действие:",
        keyboard=admin_keyboard(),
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
            "Для демонстрации используйте код AKTIV-9A.",
            keyboard=cancel_keyboard(),
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
    admin_events_view = False

    if text.strip().lower() == "/cancel" or text.strip() == "↩️ Отмена":
        pending_class_code_users.discard(message.user_id)
        pending_ideas.pop(message.user_id, None)
        pending_admin_actions.pop(message.user_id, None)
        pending_admin_forms.pop(message.user_id, None)
        pending_event_selection_users.discard(message.user_id)
        pending_report_selection_users.discard(message.user_id)
        pending_user_reports.pop(message.user_id, None)
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
            await message.answer(
                "Шаг 2 из 3. Опишите идею (до 1000 символов).",
                keyboard=cancel_keyboard(),
            )
            return

        if idea["step"] == "description":
            if not value or len(value) > 1000:
                await message.answer("Описание должно быть от 1 до 1000 символов. Попробуйте ещё раз.")
                return
            idea["description"] = value
            idea["step"] = "target_group"
            await message.answer(
                "Шаг 3 из 3. Для кого подходит идея? Например: 5-8 классы.",
                keyboard=cancel_keyboard(),
            )
            return

        if idea["step"] == "target_group":
            if not value or len(value) > 100:
                await message.answer("Укажите группу длиной до 100 символов.")
                return
            idea_status, idea_id = await create_idea(
                message.user_id,
                idea["title"],
                idea["description"],
                value,
            )
            if idea_status == "limit":
                await message.answer(
                    "Лимит идей класса на текущий месяц исчерпан. "
                    "Попробуйте снова в следующем месяце."
                )
                return
            if idea_id is None:
                await message.answer("Не удалось сохранить идею. Сначала привяжите пользователя к классу.")
                return
            pending_ideas.pop(message.user_id, None)
            await notify_moderators(
                f"📝 Новая идея №{idea_id} от пользователя {message.user_id}.\n"
                "Откройте очередь командой /ideas."
            )
            await message.answer(
                f"Идея №{idea_id} отправлена на модерацию.\n"
                "Решение модератора появится в уведомлениях.",
                keyboard=user_context_keyboard(
                    "💡 Подать идею",
                    is_admin=await is_admin_user(message.user_id),
                ),
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

    if message.user_id in pending_admin_forms:
        form = pending_admin_forms[message.user_id]
        value = text.strip()

        if value == "↩️ Отмена":
            pending_admin_forms.pop(message.user_id, None)
            await message.answer("Добавление класса отменено.", keyboard=context_keyboard("class"))
            return

        if form.get("kind") == "addclass" and form["step"] == "name":
            if not value or len(value) > 40:
                await message.answer("Название класса должно быть от 1 до 40 символов.")
                return
            form["name"] = value
            form["step"] = "group"
            await message.answer(
                "Выберите возрастную группу:",
                keyboard=class_group_keyboard(),
            )
            return

        if form.get("kind") == "addclass" and form["step"] == "group":
            group_values = {
                "1–4 классы": "1-4",
                "5–8 классы": "5-8",
                "9–11 классы": "9-11",
                "1-4": "1-4",
                "5-8": "5-8",
                "9-11": "9-11",
            }
            if value not in group_values:
                await message.answer(
                    "Выберите одну из кнопок возрастной группы.",
                    keyboard=class_group_keyboard(),
                )
                return
            form["group"] = group_values[value]
            form["step"] = "code"
            await message.answer(
                "Введите код приглашения класса, например AKTIV-11A.",
                keyboard=cancel_keyboard(),
            )
            return

        if form.get("kind") == "addclass" and form["step"] == "code":
            if not value or len(value) > 50 or "|" in value or " " in value:
                await message.answer(
                    "Код должен быть без пробелов и символа |, длиной до 50 символов."
                )
                return
            form["code"] = value.upper()
            form["step"] = "confirm"
            await message.answer(
                "Проверьте данные:\n"
                f"Класс: {form['name']}\n"
                f"Группа: {form['group']}\n"
                f"Код: {form['code']}\n\n"
                "Если всё верно, нажмите «✅ Завершить добавление».",
                keyboard=class_confirmation_keyboard(),
            )
            return

        if form.get("kind") == "addclass" and form["step"] == "confirm":
            if value == "✏️ Изменить":
                form.clear()
                form["step"] = "name"
                await message.answer(
                    "Введите название класса заново.",
                    keyboard=cancel_keyboard(),
                )
                return
            if value != "✅ Завершить добавление":
                await message.answer(
                    "Нажмите «✅ Завершить добавление» или «✏️ Изменить».",
                    keyboard=class_confirmation_keyboard(),
                )
                return
            created = await create_class(form["name"], form["group"], form["code"])
            pending_admin_forms.pop(message.user_id, None)
            if not created:
                await message.answer(
                    "Не удалось добавить класс: имя или код уже используются.",
                    keyboard=context_keyboard("class"),
                )
                return
            await message.answer(
                f"Класс {form['name']} добавлен. Код: {form['code']}.",
                keyboard=context_keyboard("class"),
            )
            return

        kind = form["kind"]

        if kind == "setvalue":
            if form["step"] == "value":
                if not value or len(value) > 200:
                    await message.answer("Ценность должна быть от 1 до 200 символов.")
                    return
                form["value"] = value
                form["step"] = "confirm"
                await message.answer(
                    f"Новая ценность месяца:\n{value}\n\nСохранить её?",
                    keyboard=confirmation_keyboard("✅ Сохранить ценность"),
                )
                return
            if value == "✏️ Изменить":
                form["step"] = "value"
                await message.answer("Введите ценность месяца заново.", keyboard=cancel_keyboard())
                return
            if value == "✅ Сохранить ценность":
                text = f"/setvalue {form['value']}"
                pending_admin_forms.pop(message.user_id, None)
            else:
                await message.answer(
                    "Нажмите «✅ Сохранить ценность» или «✏️ Изменить».",
                    keyboard=confirmation_keyboard("✅ Сохранить ценность"),
                )
                return

        elif kind == "addevent":
            if form["step"] == "title":
                if not value or len(value) > 120:
                    await message.answer("Название должно быть от 1 до 120 символов.")
                    return
                form["title"] = value
                form["step"] = "description"
                await message.answer("Введите описание мероприятия.", keyboard=cancel_keyboard())
                return
            if form["step"] == "description":
                if not value or len(value) > 1000:
                    await message.answer("Описание должно быть от 1 до 1000 символов.")
                    return
                form["description"] = value
                form["step"] = "date"
                await message.answer("Введите дату в формате YYYY-MM-DD.", keyboard=cancel_keyboard())
                return
            if form["step"] == "date":
                try:
                    date.fromisoformat(value)
                except ValueError:
                    await message.answer("Дата должна быть в формате YYYY-MM-DD.")
                    return
                form["date"] = value
                form["step"] = "class"
                await message.answer("Введите класс-организатор, например 9А.", keyboard=cancel_keyboard())
                return
            if form["step"] == "class":
                if not value or len(value) > 40:
                    await message.answer("Введите название класса длиной до 40 символов.")
                    return
                form["class"] = value
                form["step"] = "confirm"
                await message.answer(
                    "Проверьте мероприятие:\n"
                    f"Название: {form['title']}\n"
                    f"Описание: {form['description']}\n"
                    f"Дата: {form['date']}\n"
                    f"Класс: {form['class']}\n\n"
                    "Сохранить мероприятие?",
                    keyboard=confirmation_keyboard("✅ Сохранить мероприятие"),
                )
                return
            if value == "✏️ Изменить":
                form["step"] = "title"
                await message.answer("Введите название мероприятия заново.", keyboard=cancel_keyboard())
                return
            if value == "✅ Сохранить мероприятие":
                text = (
                    f"/addevent {form['title']}|{form['description']}|"
                    f"{form['date']}|{form['class']}"
                )
                pending_admin_forms.pop(message.user_id, None)
            else:
                await message.answer(
                    "Нажмите «✅ Сохранить мероприятие» или «✏️ Изменить».",
                    keyboard=confirmation_keyboard("✅ Сохранить мероприятие"),
                )
                return

        elif kind == "setrole":
            if form["step"] == "id":
                if not value.isdigit():
                    await message.answer("Введите числовой ID пользователя.")
                    return
                form["user_id"] = value
                form["step"] = "role"
                await message.answer(
                    "Выберите новую роль:",
                    keyboard=options_keyboard(
                        "🛡 Модератор",
                        "👩‍🏫 Учитель",
                        "👤 Участник",
                        "↩️ Отмена",
                    ),
                )
                return
            roles = {
                "🛡 Модератор": "moderator",
                "👩‍🏫 Учитель": "teacher",
                "👤 Участник": "student",
            }
            if form["step"] == "role":
                if value not in roles:
                    await message.answer("Выберите роль кнопкой.")
                    return
                form["role"] = roles[value]
                form["step"] = "confirm"
                await message.answer(
                    f"Назначить пользователю {form['user_id']} роль «{form['role']}»?",
                    keyboard=confirmation_keyboard("✅ Сохранить роль"),
                )
                return
            if value == "✏️ Изменить":
                form["step"] = "id"
                await message.answer("Введите ID пользователя заново.", keyboard=cancel_keyboard())
                return
            if value == "✅ Сохранить роль":
                text = f"/setrole {form['user_id']} {form['role']}"
                pending_admin_forms.pop(message.user_id, None)
            else:
                await message.answer(
                    "Нажмите «✅ Сохранить роль» или «✏️ Изменить».",
                    keyboard=confirmation_keyboard("✅ Сохранить роль"),
                )
                return

        elif kind in {"approve_idea", "approve_report"}:
            command = "/approve" if kind == "approve_idea" else "/approve_report"
            title = "идею" if kind == "approve_idea" else "отчёт"
            confirm = (
                "✅ Одобрить идею"
                if kind == "approve_idea"
                else "✅ Подтвердить одобрение"
            )
            if form["step"] == "id":
                if not value.isdigit():
                    await message.answer(f"Введите числовой номер {title}.")
                    return
                form["item_id"] = value
                form["step"] = "confirm"
                await message.answer(
                    f"Подтвердить одобрение: {title} №{value}?",
                    keyboard=(
                        report_confirmation_keyboard(confirm)
                        if kind == "approve_report"
                        else confirmation_keyboard(confirm)
                    ),
                )
                return
            if value == "✏️ Изменить":
                form["step"] = "id"
                await message.answer(f"Введите номер {title} заново.", keyboard=cancel_keyboard())
                return
            if value == confirm:
                text = f"{command} {form['item_id']}"
                pending_admin_forms.pop(message.user_id, None)
            else:
                await message.answer(
                    (
                        f"Нажмите «{confirm}»."
                        if kind == "approve_report"
                        else f"Нажмите «{confirm}» или «✏️ Изменить»."
                    ),
                    keyboard=(
                        report_confirmation_keyboard(confirm)
                        if kind == "approve_report"
                        else confirmation_keyboard(confirm)
                    ),
                )
                return

        elif kind in {"reject_idea", "reject_report"}:
            command = "/reject" if kind == "reject_idea" else "/reject_report"
            title = "идею" if kind == "reject_idea" else "отчёт"
            confirm = (
                "❌ Отклонить идею"
                if kind == "reject_idea"
                else "❌ Подтвердить отклонение"
            )
            if form["step"] == "id":
                if not value.isdigit():
                    await message.answer(f"Введите числовой номер {title}.")
                    return
                form["item_id"] = value
                form["step"] = "reason"
                await message.answer(
                    f"Введите причину отклонения {title}.",
                    keyboard=cancel_keyboard(),
                )
                return
            if form["step"] == "reason":
                if not value or len(value) > 500:
                    await message.answer("Причина должна быть от 1 до 500 символов.")
                    return
                form["reason"] = value
                form["step"] = "confirm"
                await message.answer(
                    f"Отклонить {title} №{form['item_id']}?\nПричина: {value}",
                    keyboard=(
                        report_confirmation_keyboard(confirm)
                        if kind == "reject_report"
                        else confirmation_keyboard(confirm)
                    ),
                )
                return
            if value == "✏️ Изменить":
                form["step"] = "id"
                await message.answer(f"Введите номер {title} заново.", keyboard=cancel_keyboard())
                return
            if value == confirm:
                text = f"{command} {form['item_id']} {form['reason']}"
                pending_admin_forms.pop(message.user_id, None)
            else:
                await message.answer(
                    (
                        f"Нажмите «{confirm}»."
                        if kind == "reject_report"
                        else f"Нажмите «{confirm}» или «✏️ Изменить»."
                    ),
                    keyboard=(
                        report_confirmation_keyboard(confirm)
                        if kind == "reject_report"
                        else confirmation_keyboard(confirm)
                    ),
                )
                return

        elif kind == "award":
            if form["step"] == "class":
                if not value or len(value) > 40:
                    await message.answer("Введите название класса длиной до 40 символов.")
                    return
                form["class"] = value
                form["step"] = "place"
                await message.answer(
                    "Выберите призовое место:",
                    keyboard=options_keyboard("🥇 1 место", "🥈 2 место", "🥉 3 место", "↩️ Отмена"),
                )
                return
            places = {"🥇 1 место": "1", "🥈 2 место": "2", "🥉 3 место": "3"}
            if form["step"] == "place":
                if value not in places:
                    await message.answer("Выберите место кнопкой.")
                    return
                form["place"] = places[value]
                form["step"] = "confirm"
                await message.answer(
                    f"Начислить призовые баллы классу {form['class']} за {form['place']} место?",
                    keyboard=confirmation_keyboard("✅ Начислить баллы"),
                )
                return
            if value == "✏️ Изменить":
                form["step"] = "class"
                await message.answer("Введите название класса заново.", keyboard=cancel_keyboard())
                return
            if value == "✅ Начислить баллы":
                text = f"/award {form['class']}|{form['place']}"
                pending_admin_forms.pop(message.user_id, None)
            else:
                await message.answer(
                    "Нажмите «✅ Начислить баллы» или «✏️ Изменить».",
                    keyboard=confirmation_keyboard("✅ Начислить баллы"),
                )
                return

        elif kind == "setlimit":
            limit_types = {"💡 Лимит идей": "ideas", "✅ Лимит одобрений": "approvals"}
            if form["step"] == "type":
                if value not in limit_types:
                    await message.answer("Выберите тип лимита кнопкой.")
                    return
                form["limit_type"] = limit_types[value]
                form["step"] = "number"
                await message.answer(
                    "Введите новое значение лимита от 1 до 100.",
                    keyboard=cancel_keyboard(),
                )
                return
            if form["step"] == "number":
                if not value.isdigit() or not 1 <= int(value) <= 100:
                    await message.answer("Введите целое число от 1 до 100.")
                    return
                form["number"] = value
                form["step"] = "confirm"
                await message.answer(
                    f"Установить лимит «{form['limit_type']}» равным {value} в месяц?",
                    keyboard=confirmation_keyboard("✅ Сохранить лимит"),
                )
                return
            if value == "✏️ Изменить":
                form["step"] = "type"
                await message.answer(
                    "Выберите тип лимита:",
                    keyboard=options_keyboard("💡 Лимит идей", "✅ Лимит одобрений", "↩️ Отмена"),
                )
                return
            if value == "✅ Сохранить лимит":
                text = f"/setlimit {form['limit_type']}|{form['number']}"
                pending_admin_forms.pop(message.user_id, None)
            else:
                await message.answer(
                    "Нажмите «✅ Сохранить лимит» или «✏️ Изменить».",
                    keyboard=confirmation_keyboard("✅ Сохранить лимит"),
                )
                return

    if text == "📋 Подать отчёт":
        if message.user_id is None:
            await message.answer("Не удалось определить пользователя MAX.")
            return
        report_events = await list_signed_up_events(message.user_id)
        if not report_events:
            await message.answer(
                "Нет мероприятий, на которые записан ваш класс.",
                keyboard=event_entry_keyboard(await is_admin_user(message.user_id)),
            )
            return
        pending_report_selection_users.add(message.user_id)
        lines = ["📋 Выберите мероприятие для отчёта:"]
        for event_id, title, description, event_date, class_name in report_events:
            lines.append(
                f"\n№{event_id} · {event_date} · {title}\n"
                f"{description}\nОрганизатор: класс {class_name}"
            )
        await message.answer(
            "\n".join(lines),
            keyboard=report_selection_keyboard(report_events),
        )
        return

    if message.user_id in pending_report_selection_users:
        if text == "↩️ Назад":
            pending_report_selection_users.discard(message.user_id)
            await message.answer(
                "Возвращаемся к афише.",
                keyboard=event_entry_keyboard(await is_admin_user(message.user_id)),
            )
            return
        if text.startswith("📋 Отчёт №"):
            event_id_text = text.removeprefix("📋 Отчёт №").strip()
            if not event_id_text.isdigit():
                await message.answer("Выберите мероприятие кнопкой.")
                return
            event_id = int(event_id_text)
            existing_report = await get_class_report(message.user_id, event_id)
            if existing_report is not None:
                report_id, status, author_id, moderator_comment = existing_report
                if status == "pending":
                    await message.answer(
                        "Ваш класс уже отправил отчёт по этому мероприятию. "
                        "Он находится на проверке модератора."
                    )
                    return
                if status == "approved":
                    await message.answer(
                        "Ваш класс уже получил баллы за отчёт по этому мероприятию. "
                        "Повторная подача невозможна."
                    )
                    return
                if author_id != message.user_id:
                    await message.answer(
                        "Отчёт вашего класса был отклонён. Исправить его может "
                        "только участник, который подавал этот отчёт."
                    )
                    return
                pending_user_reports[message.user_id] = {
                    "event_id": event_id_text,
                    "report_id": str(report_id),
                    "mode": "revise",
                    "step": "comment",
                }
                reason = moderator_comment or "Причина не указана."
                await message.answer(
                    f"Отчёт №{report_id} отклонён модератором.\n"
                    f"Причина: {reason}\n\n"
                    "Введите исправленный комментарий. Фото или видео можно "
                    "прикрепить к этому же сообщению.",
                    keyboard=cancel_keyboard(),
                )
                pending_report_selection_users.discard(message.user_id)
                return
            pending_report_selection_users.discard(message.user_id)
            pending_user_reports[message.user_id] = {
                "event_id": event_id_text,
                "mode": "new",
                "step": "comment",
            }
            await message.answer(
                "Введите комментарий к отчёту. Фото или видео можно прикрепить "
                "к этому же сообщению.",
                keyboard=cancel_keyboard(),
            )
            return

    if message.user_id in pending_user_reports:
        report_form = pending_user_reports[message.user_id]
        raw_text = (message.text or "").strip()
        attachment_token = media_tokens(message)

        if report_form["step"] == "comment":
            if not raw_text and not attachment_token:
                await message.answer(
                    "Введите комментарий или прикрепите фото/видео.",
                    keyboard=cancel_keyboard(),
                )
                return
            if len(raw_text) > 2000:
                await message.answer("Комментарий должен быть не длиннее 2000 символов.")
                return
            report_form["comment"] = raw_text
            report_form["media_token"] = attachment_token or ""
            report_form["step"] = "confirm"
            media_note = "Да" if attachment_token else "Нет"
            await message.answer(
                f"Отчёт по мероприятию №{report_form['event_id']}\n"
                f"Комментарий: {raw_text or 'без комментария'}\n"
                f"Фото/видео: {media_note}\n\n"
                "Отправить отчёт на проверку?",
                keyboard=user_report_confirmation_keyboard(),
            )
            return

        if report_form["step"] == "confirm":
            if text.strip() == "✏️ Изменить комментарий":
                report_form["step"] = "comment"
                await message.answer(
                    "Введите комментарий заново. Фото или видео можно прикрепить "
                    "к этому же сообщению.",
                    keyboard=cancel_keyboard(),
                )
                return
            if text.strip() != "✅ Отправить отчёт":
                await message.answer(
                    "Нажмите «✅ Отправить отчёт» или «✏️ Изменить комментарий».",
                    keyboard=user_report_confirmation_keyboard(),
                )
                return
            if report_form.get("mode") == "revise":
                result, report_id = await revise_report(
                    message.user_id,
                    int(report_form["event_id"]),
                    report_form["comment"] or None,
                    report_form["media_token"] or None,
                )
            else:
                result, report_id = await create_report(
                    message.user_id,
                    int(report_form["event_id"]),
                    report_form["comment"] or None,
                    report_form["media_token"] or None,
                )
            pending_user_reports.pop(message.user_id, None)
            if result in {"class_already_reported", "already_reported"}:
                await message.answer(
                    "Ваш класс уже отправлял отчёт по этому мероприятию. "
                    "Повторная отправка невозможна.",
                    keyboard=event_entry_keyboard(await is_admin_user(message.user_id)),
                )
                return
            if result == "rejected_exists":
                await message.answer(
                    "Ваш класс уже имеет отклонённый отчёт. "
                    "Исправить его может автор первоначальной подачи.",
                    keyboard=event_entry_keyboard(await is_admin_user(message.user_id)),
                )
                return
            if result == "not_report_author":
                await message.answer(
                    "Исправить этот отчёт может только его автор.",
                    keyboard=event_entry_keyboard(await is_admin_user(message.user_id)),
                )
                return
            if result == "not_bound":
                await message.answer("Сначала привяжите пользователя к классу через /start.")
                return
            if result == "not_signed":
                await message.answer("Ваш класс не записан на это мероприятие.")
                return
            report_kind = (
                "Исправленный отчёт"
                if report_form.get("mode") == "revise"
                else "Новый отчёт"
            )
            await notify_moderators(
                f"📋 {report_kind} №{report_id} по мероприятию №{report_form['event_id']}.\n"
                "Откройте очередь отчётов для проверки."
            )
            await message.answer(
                (
                    f"Исправленный отчёт №{report_id} повторно отправлен на проверку."
                    if report_form.get("mode") == "revise"
                    else f"Отчёт №{report_id} отправлен на проверку."
                ),
                keyboard=event_entry_keyboard(await is_admin_user(message.user_id)),
            )
            return

    if text == "⚙️ Администрирование":
        await admin_message(message)
        return

    if text == "↩️ Администрирование":
        await admin_message(message)
        return

    if text == "➕ Добавить класс":
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        pending_admin_actions.pop(message.user_id, None)
        pending_admin_forms[message.user_id] = {"kind": "addclass", "step": "name"}
        await message.answer(
            "Введите название класса, например 11А.",
            keyboard=cancel_keyboard(),
        )
        return

    if text in {
        "📚 Классы",
        "👥 Пользователи",
        "🎪 Мероприятия",
        "💡 Очередь идей",
        "📋 Очередь отчётов",
    }:
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        pending_admin_actions.pop(message.user_id, None)
        admin_events_view = text == "🎪 Мероприятия"
        text = {
            "📚 Классы": "/classes",
            "👥 Пользователи": "/users",
            "🎪 Мероприятия": "/events",
            "💡 Очередь идей": "/ideas",
            "📋 Очередь отчётов": "/reports",
        }[text]

    elif text == "↩️ Главное меню":
        pending_admin_actions.pop(message.user_id, None)
        await message.answer(
            "Возвращаемся в главное меню.",
            keyboard=main_keyboard(await is_admin_user(message.user_id)),
        )
        return

    admin_form_starters = {
        "⚙️ Ценность месяца": (
            {"kind": "setvalue", "step": "value"},
            "Введите новую ценность месяца.",
        ),
        "📅 Добавить мероприятие": (
            {"kind": "addevent", "step": "title"},
            "Введите название мероприятия.",
        ),
        "🛡 Назначить роль": (
            {"kind": "setrole", "step": "id"},
            "Введите ID пользователя.",
        ),
        "✅ Одобрить идею": (
            {"kind": "approve_idea", "step": "id"},
            "Введите номер идеи.",
        ),
        "❌ Отклонить идею": (
            {"kind": "reject_idea", "step": "id"},
            "Введите номер идеи.",
        ),
        "✅ Одобрить отчёт": (
            {"kind": "approve_report", "step": "id"},
            "Введите номер отчёта.",
        ),
        "❌ Отклонить отчёт": (
            {"kind": "reject_report", "step": "id"},
            "Введите номер отчёта.",
        ),
        "🏅 Призовые баллы": (
            {"kind": "award", "step": "class"},
            "Введите название класса.",
        ),
        "⚙️ Лимиты": (
            {"kind": "setlimit", "step": "type"},
            "Выберите тип лимита:",
        ),
    }

    if text in admin_form_starters:
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        pending_admin_actions.pop(message.user_id, None)
        pending_admin_forms[message.user_id] = admin_form_starters[text][0]
        prompt = admin_form_starters[text][1]
        keyboard = cancel_keyboard()
        if text == "⚙️ Лимиты":
            keyboard = options_keyboard(
                "💡 Лимит идей",
                "✅ Лимит одобрений",
                "↩️ Отмена",
            )
        elif text in {"✅ Одобрить отчёт", "❌ Отклонить отчёт"}:
            keyboard = cancel_keyboard()
        await message.answer(
            prompt + "\n\nДля отмены нажмите «↩️ Отмена».",
            keyboard=keyboard,
        )
        return

    if text == "🎯 Ценность месяца":
        value = await get_setting_text("monthly_value", "Взаимопомощь")
        await message.answer(
            f"🎯 Ценность месяца: {value}",
            keyboard=user_context_keyboard(
                is_admin=await is_admin_user(message.user_id),
            ),
        )
        return

    if text == "/classes":
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        classes = await list_classes_with_codes()
        lines = ["📚 Классы и коды приглашения:"]
        lines.extend(f"• {name} ({age_group}) — {code}" for name, age_group, code in classes)
        await message.answer("\n".join(lines), keyboard=context_keyboard("class"))
        return

    if text == "/users":
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        users = await list_users()
        if not users:
            await message.answer("Пользователей пока нет.", keyboard=context_keyboard("role"))
            return
        lines = ["👥 Пользователи и роли:"]
        for max_user_id, role, class_name in users:
            lines.append(
                f"{max_user_id} — {role} — класс: {class_name or 'не привязан'}"
            )
        await message.answer("\n".join(lines), keyboard=context_keyboard("role"))
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
        await message.answer(
            f"Для пользователя {target_id} назначена роль: {parts[2]}.",
            keyboard=context_keyboard("role"),
        )
        return

    if text == "/setlimit" or text.startswith("/setlimit "):
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        parts = [part.strip() for part in text.removeprefix("/setlimit").strip().split("|", maxsplit=1)]
        limit_keys = {"ideas": "class_idea_limit", "approvals": "monthly_approval_limit"}
        if len(parts) != 2 or parts[0] not in limit_keys or not parts[1].isdigit():
            await message.answer(
                "Формат: /setlimit ideas|Число или /setlimit approvals|Число"
            )
            return
        limit = int(parts[1])
        if limit < 1 or limit > 100:
            await message.answer("Лимит должен быть от 1 до 100.")
            return
        await set_setting(limit_keys[parts[0]], str(limit))
        await message.answer(
            f"Лимит {parts[0]} установлен: {limit} в месяц.",
            keyboard=context_keyboard("limit"),
        )
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
        await message.answer(f"Ценность месяца изменена: {value}", keyboard=context_keyboard("value"))
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
        await message.answer(
            f"Класс {parts[0]} добавлен. Код: {parts[2].upper()}",
            keyboard=context_keyboard("class"),
        )
        return

    if text == "/award" or text.startswith("/award "):
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        parts = [part.strip() for part in text.removeprefix("/award").strip().split("|", maxsplit=1)]
        if (
            len(parts) != 2
            or not parts[0]
            or not parts[1].isdigit()
            or int(parts[1]) not in {1, 2, 3}
        ):
            await message.answer("Формат: /award Класс|Место\nПример: /award 9А|1")
            return
        if message.user_id is None:
            await message.answer("Не удалось определить администратора.")
            return
        awarded, points = await award_prize(parts[0], int(parts[1]), message.user_id)
        if not awarded:
            await message.answer("Класс не найден. Проверьте название командой /classes.")
            return
        await message.answer(
            f"Классу {parts[0]} начислено {points} баллов за {parts[1]} место.",
            keyboard=context_keyboard("award"),
        )
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
            "Пользователи увидят его через кнопку «🎪 Афиша».",
            keyboard=context_keyboard("event") if is_admin else main_keyboard(),
        )
        return

    if text == "🤝 Принять участие":
        events = await list_events()
        if not events:
            await message.answer(
                "Сейчас нет доступных мероприятий.",
                keyboard=user_context_keyboard(
                    "🎪 Афиша",
                    is_admin=await is_admin_user(message.user_id),
                ),
            )
            return
        pending_event_selection_users.add(message.user_id)
        lines = ["🎪 Выберите номер мероприятия для участия:"]
        for event_id, title, description, event_date, class_name in events:
            lines.append(
                f"\n№{event_id} · {event_date} · {title}\n"
                f"{description}\nОрганизатор: класс {class_name}"
            )
        await message.answer("\n".join(lines), keyboard=event_selection_keyboard(events))
        return

    if message.user_id in pending_event_selection_users and text.startswith("№"):
        event_id_text = text.removeprefix("№").strip()
        if not event_id_text.isdigit():
            await message.answer("Выберите номер мероприятия кнопкой.")
            return
        pending_event_selection_users.discard(message.user_id)
        result = await signup_for_event(message.user_id, int(event_id_text))
        responses = {
            "not_bound": "Сначала привяжите пользователя к классу через /start.",
            "not_found": "Мероприятие не найдено или уже недоступно.",
            "already": "Ваш класс уже записан на это мероприятие.",
            "ok": "Готово! Ваш класс записан на мероприятие.",
        }
        await message.answer(
            responses[result],
            keyboard=event_entry_keyboard(await is_admin_user(message.user_id)),
        )
        return

    if text == "↩️ Назад" and message.user_id in pending_event_selection_users:
        pending_event_selection_users.discard(message.user_id)
        text = "🎪 Афиша"

    if text == "/events" or text == "🎪 Афиша":
        events = await list_events()
        is_admin = await is_admin_user(message.user_id)
        if not events:
            await message.answer(
                "🎪 Афиша пока пуста. Администратор может добавить мероприятие командой /addevent.",
                keyboard=context_keyboard("event") if admin_events_view else event_entry_keyboard(is_admin),
            )
            return
        lines = ["🎪 Афиша мероприятий:"]
        for event_id, title, description, event_date, class_name in events:
            lines.append(
                f"\n№{event_id} · {event_date} · {title}\n"
                f"{description}\n"
                f"Организатор: класс {class_name}\n"
                "Для записи нажмите соответствующую кнопку ниже."
            )
        await message.answer(
            "\n".join(lines),
            keyboard=(
                context_keyboard("event")
                if admin_events_view
                else event_entry_keyboard(is_admin)
            ),
        )
        return

    if text.startswith("✅ Записаться №"):
        event_id_text = text.removeprefix("✅ Записаться №").strip()
        if message.user_id is None or not event_id_text.isdigit():
            await message.answer("Не удалось определить мероприятие.")
            return
        result = await signup_for_event(message.user_id, int(event_id_text))
        responses = {
            "not_bound": "Сначала привяжите пользователя к классу через /start.",
            "not_found": "Мероприятие не найдено или уже недоступно.",
            "already": "Ваш класс уже записан на это мероприятие.",
            "ok": "Готово! Ваш класс записан на мероприятие.",
        }
        await message.answer(
            responses[result],
            keyboard=user_context_keyboard("🎪 Афиша", is_admin=await is_admin_user(message.user_id)),
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
        await message.answer(
            responses[result],
            keyboard=user_context_keyboard("🎪 Афиша", is_admin=await is_admin_user(message.user_id)),
        )
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
            "class_already_reported": (
                "Ваш класс уже отправлял отчёт по этому мероприятию. "
                "Повторная подача невозможна."
            ),
            "already_reported": "Ваш класс уже отправлял отчёт по этому мероприятию.",
            "rejected_exists": (
                "Ваш класс уже имеет отклонённый отчёт. "
                "Для исправления используйте кнопку «📋 Подать отчёт»."
            ),
        }
        if result != "ok":
            await message.answer(responses[result])
            return
        media_note = " Фото/видео сохранено." if attachment_token else ""
        await message.answer(
            f"Отчёт №{report_id} отправлен на проверку модератору.{media_note}",
            keyboard=user_context_keyboard(
                "🎪 Афиша",
                is_admin=await is_admin_user(message.user_id),
            ),
        )
        await notify_moderators(
            f"📋 Новый отчёт №{report_id} по мероприятию №{parts[0]}.\n"
            "Откройте очередь командой /reports."
        )
        return

    is_moderator = await is_moderator_user(message.user_id)

    if text == "/ideas":
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        ideas = await list_pending_ideas()
        if not ideas:
            await message.answer("Очередь идей пуста.", keyboard=context_keyboard("idea"))
            return
        lines = ["📝 Идеи на модерации:"]
        for idea_id, title, description, target_group, class_name in ideas:
            lines.append(
                f"\n№{idea_id} от класса {class_name}\n"
                f"{title}\n{description}\nДля: {target_group}"
            )
        await message.answer(
            "\n".join(lines),
            keyboard=context_keyboard("idea") if await is_admin_user(message.user_id) else main_keyboard(),
        )
        return

    if text == "/approve" or text.startswith("/approve "):
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Формат: /approve Номер")
            return
        idea_id = int(parts[1])
        author_id = await get_idea_author_id(idea_id)
        changed, status, event_id = await moderate_idea(idea_id, message.user_id, "approved")
        if status == "limit":
            await message.answer(
                "Лимит одобренных идей этого класса на месяц исчерпан."
            )
            return
        if not changed:
            await message.answer("Идея не найдена или уже обработана.")
            return
        await message.answer(
            f"Идея одобрена, мероприятие №{event_id} добавлено в афишу. "
            "Баллы начислены по текущей настройке.",
            keyboard=context_keyboard("idea") if await is_admin_user(message.user_id) else main_keyboard(),
        )
        if author_id is not None:
            await notify_user(
                author_id,
                f"✅ Идея №{idea_id} одобрена.\n"
                f"Она опубликована в афише как мероприятие №{event_id}.",
            )
        return

    if text == "/reject" or text.startswith("/reject "):
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        parts = text.split(maxsplit=2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].strip():
            await message.answer("Формат: /reject Номер Причина отклонения")
            return
        idea_id = int(parts[1])
        author_id = await get_idea_author_id(idea_id)
        changed, status, event_id = await moderate_idea(
            idea_id,
            message.user_id,
            "rejected",
            parts[2].strip(),
        )
        if not changed:
            await message.answer("Идея не найдена или уже обработана.")
            return
        await message.answer(
            "Идея отклонена. Комментарий сохранён.",
            keyboard=admin_keyboard() if await is_admin_user(message.user_id) else main_keyboard(),
        )
        if author_id is not None:
            await notify_user(
                author_id,
                f"❌ Идея №{idea_id} отклонена.\nПричина: {parts[2].strip()}",
            )
        return

    if text == "/reports":
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        reports = await list_pending_reports()
        if not reports:
            await message.answer("Очередь отчётов пуста.", keyboard=report_actions_keyboard())
            return
        lines = ["📋 Отчёты на проверке:"]
        for report_id, event_id, title, class_name, comment, media_token in reports:
            lines.append(
                f"\nОтчёт №{report_id}, мероприятие №{event_id}: {title}\n"
                f"Класс: {class_name}\n{comment}"
                + ("\nПрикреплён медиа-файл." if media_token else "")
            )
        await message.answer(
            "\n".join(lines),
            keyboard=report_actions_keyboard(),
        )
        return

    if text == "/approve_report" or text.startswith("/approve_report "):
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Формат: /approve_report Номер отчёта")
            return
        report_id = int(parts[1])
        report_author_id = await get_report_submitter_id(report_id)
        changed, status, points = await moderate_report(
            report_id, message.user_id, "approved"
        )
        if not changed:
            await message.answer("Отчёт не найден или уже обработан.")
            return
        await message.answer(
            f"Отчёт подтверждён. Классу начислено {points} баллов.",
            keyboard=report_actions_keyboard(),
        )
        if report_author_id is not None:
            await notify_user(
                report_author_id,
                f"✅ Отчёт №{report_id} подтверждён. "
                f"Классу начислено {points} баллов.",
            )
        return

    if text == "/reject_report" or text.startswith("/reject_report "):
        if not is_moderator:
            await message.answer("У вас нет прав модератора.")
            return
        parts = text.split(maxsplit=2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].strip():
            await message.answer("Формат: /reject_report Номер Причина отклонения")
            return
        report_id = int(parts[1])
        report_author_id = await get_report_submitter_id(report_id)
        changed, status, points = await moderate_report(
            report_id, message.user_id, "rejected", parts[2].strip()
        )
        if not changed:
            await message.answer("Отчёт не найден или уже обработан.")
            return
        await message.answer(
            "Отчёт отклонён. Комментарий сохранён.",
            keyboard=report_actions_keyboard(),
        )
        if report_author_id is not None:
            await notify_user(
                report_author_id,
                f"❌ Отчёт №{report_id} отклонён.\n"
                f"Причина: {parts[2].strip()}\n"
                "Вы можете исправить его через кнопку «📋 Подать отчёт».",
            )
        return

    if text == "🏆 Рейтинг":
        rating = await list_rating()
        lines = ["🏆 Рейтинг классов:"]
        for position, (name, age_group, points) in enumerate(rating, start=1):
            lines.append(f"{position}. {name} ({age_group}) — {points} баллов")
        await message.answer(
            "\n".join(lines),
            keyboard=user_context_keyboard(
                "📍 Место",
                "👥 Команда",
                is_admin=await is_admin_user(message.user_id),
            ),
        )
        return

    if text == "📍 Место":
        if message.user_id is None:
            await message.answer("Не удалось определить пользователя MAX.")
            return
        class_place = await get_class_place(message.user_id)
        if class_place is None:
            await message.answer("Сначала привяжите пользователя к классу через /start.")
            return
        place, class_name, age_group, points = class_place
        await message.answer(
            f"📍 Место класса {class_name} ({age_group}): {place}\n"
            f"Баллы: {points}",
            keyboard=user_context_keyboard(
                "🏆 Рейтинг",
                "👥 Команда",
                is_admin=await is_admin_user(message.user_id),
            ),
        )
        return

    if text == "👥 Команда":
        if message.user_id is None:
            await message.answer("Не удалось определить пользователя MAX.")
            return
        team = await get_class_team(message.user_id)
        if team is None:
            await message.answer("Сначала привяжите пользователя к классу через /start.")
            return
        class_name, age_group, member_count = team
        await message.answer(
            f"👥 Команда класса {class_name} ({age_group})\n"
            f"Участников в системе: {member_count}",
            keyboard=user_context_keyboard(
                "📍 Место",
                "🏆 Рейтинг",
                is_admin=await is_admin_user(message.user_id),
            ),
        )
        return

    if text == "💡 Подать идею":
        if message.user_id is None or await get_user_class(message.user_id) is None:
            await message.answer("Сначала привяжите пользователя к классу через /start.")
            return
        pending_ideas[message.user_id] = {"step": "title"}
        await message.answer(
            "Шаг 1 из 3. Введите название идеи (до 120 символов).\n"
            "Для отмены нажмите «↩️ Отмена».",
            keyboard=cancel_keyboard(),
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


def build_webhook_app() -> tuple[FastAPI | None, SimpleEngine | None]:
    if BOT_MODE != "webhook":
        if BOT_MODE != "polling":
            raise RuntimeError("BOT_MODE должен быть polling или webhook")
        return None, None

    public_url = required_env("WEBHOOK_PUBLIC_URL")
    webhook_secret = required_env("WEBHOOK_SECRET")
    engine = SimpleEngine(
        dispatcher,
        bot,
        web_adapter=FastApiWebAdapter(),
        routing=StaticRouting(public_url),
        security=StaticSecretToken(webhook_secret),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.on_startup(app)
        await engine.set_webhook()
        print(f"Webhook подключён: {public_url}")
        try:
            yield
        finally:
            await engine.on_shutdown(app)

    app = FastAPI(title="Aktiv MAX bot", lifespan=lifespan)
    engine.register(app)
    return app, engine


webhook_app, webhook_engine = build_webhook_app()


if __name__ == "__main__":
    asyncio.run(prepare_database())
    if BOT_MODE == "webhook":
        uvicorn.run(
            webhook_app,
            host=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
            port=int(os.getenv("WEBHOOK_PORT", "8080")),
        )
    else:
        dispatcher.run_polling(bot)

