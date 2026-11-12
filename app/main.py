import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI

from db import (
    add_discipline_penalty,
    award_monthly_bonus,
    award_prize,
    archive_class,
    bind_user_to_class,
    create_class,
    create_idea,
    create_event,
    create_report,
    close_expired_months,
    create_teacher_application,
    create_teacher_invite,
    delete_class_if_empty,
    ensure_user_role,
    get_admin_ids,
    get_class_report,
    get_moderator_ids,
    get_teacher_application,
    get_teacher_invite,
    get_idea_author_id,
    get_class_place,
    get_class_team,
    get_class_usage,
    get_user_role,
    get_user_class,
    get_monthly_bonus_status,
    get_current_month_value,
    get_admin_month_dashboard,
    get_deadline_reminder_info,
    get_class_rating_breakdown,
    get_month_rating,
    get_school_year_rollover_preview,
    get_report_submitter_id,
    initialize_database,
    list_classes_with_codes,
    list_classes_for_removal,
    list_events,
    list_pending_reports,
    list_pending_teacher_applications,
    list_pending_ideas,
    list_academic_year_rating,
    list_current_month_rating,
    list_recent_rating_actions,
    list_rating_history,
    list_signed_up_events,
    list_teacher_invites,
    list_teacher_assigned_classes,
    list_user_ids_by_roles,
    list_users_with_details,
    moderate_idea,
    moderate_report,
    moderate_teacher_application,
    revise_report,
    set_user_role,
    set_monthly_value,
    set_setting,
    mark_deadline_reminder_sent,
    rollover_school_year,
    signup_for_event,
    undo_rating_action,
)
from maxo import Bot, Dispatcher
from maxo.routing.filters import Command, CommandStart
from maxo.types import BotStarted, MessageCreated, InlineKeyboardAttachmentRequest
from maxo.transport.webhook.adapters.fastapi import FastApiWebAdapter
from maxo.transport.webhook.engines.simple import SimpleEngine
from maxo.transport.webhook.routing.static import StaticRouting
from maxo.transport.webhook.security.secret_token import StaticSecretToken
from maxo.utils.builders import KeyboardBuilder
import uvicorn


class CompatibleStaticSecretToken(StaticSecretToken):
    """Compatibility shim for maxo 0.9.1 webhook startup."""

    async def get_secret_token(self, bot: Bot) -> str:
        return self.secret_token(bot)


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
pending_users_role: set[int] = set()
pending_event_selection_users: set[int] = set()
pending_report_selection_users: set[int] = set()
pending_user_reports: dict[int, dict[str, str]] = {}
pending_teacher_forms: dict[int, dict[str, str]] = {}
pending_teacher_penalties: dict[int, dict[str, str]] = {}
pending_rating_context: dict[int, dict[str, object]] = {}
pending_school_year_rollover: dict[int, dict[str, str]] = {}
BOT_MODE = os.getenv("BOT_MODE", "polling").strip().lower()
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

AGE_GROUP_LABELS = {
    "1-4": "1–4 классы",
    "5-8": "5–8 классы",
    "9-11": "9–11 классы",
}
AGE_GROUP_BUTTONS = {
    "1–4 классы": "1-4",
    "5–8 классы": "5-8",
    "9–11 классы": "9-11",
}


def media_tokens(message: MessageCreated) -> str | None:
    tokens: list[str] = []
    for attachment in message.message.body.photo:
        tokens.append(attachment.payload.token)
    for attachment in message.message.body.video:
        tokens.append(attachment.payload.token)
    return ",".join(tokens) if tokens else None


async def notify_user(user_id: int, text: str) -> bool:
    try:
        await bot.send_message(user_id=user_id, text=text)
        return True
    except Exception as error:  # noqa: BLE001 - уведомление не должно останавливать бота
        print(f"Не удалось отправить уведомление пользователю {user_id}: {error}")
        return False


async def notify_moderators(text: str) -> None:
    user_ids = set(get_admin_ids()) | set(get_moderator_ids())
    user_ids.update(await list_user_ids_by_roles({"admin", "moderator"}))
    await asyncio.gather(*(notify_user(user_id, text) for user_id in user_ids))


def main_keyboard(is_admin: bool = False, is_teacher: bool = False):
    keyboard = (
        KeyboardBuilder()
        .add_message(text="🎯 Ценность месяца")
        .add_message(text="🏆 Рейтинг")
        .add_message(text="🎪 Афиша")
        .add_message(text="💡 Подать идею")
        .add_message(text="📍 Место")
        .add_message(text="👥 Команда")
        .add_message(text="ℹ️ Инструкция")
        .add_message(text="▶️ Старт")
    )
    if is_admin:
        keyboard.add_message(text="⚙️ Администрирование")
        return keyboard.adjust(2, 2, 2, 2, 1).build()
    if is_teacher:
        keyboard.add_message(text="⚠️ Штрафы классу")
        return keyboard.adjust(2, 2, 2, 2, 1).build()
    return keyboard.adjust(2, 2, 2, 1).build()


def registration_keyboard():
    return options_keyboard(
        "👨‍🎓 Регистрация ученика",
        "👩‍🏫 Регистрация учителя",
        "ℹ️ Инструкция",
        "▶️ Старт",
    )


def admin_keyboard():
    keyboard = (
        KeyboardBuilder()
        .add_message(text="⚙️ Ценность месяца")
        .add_message(text="➕ Добавить класс")
        .add_message(text="🗑 Удалить класс")
        .add_message(text="📅 Добавить мероприятие")
        .add_message(text="📚 Классы")
        .add_message(text="👥 Пользователи")
        .add_message(text="🛡 Назначить роль")
        .add_message(text="🎪 Мероприятия")
        .add_message(text="💡 Очередь идей")
        .add_message(text="📋 Очередь отчётов")
        .add_message(text="👩‍🏫 Заявки учителей")
        .add_message(text="🔑 Коды учителей")
        .add_message(text="✅ Одобрить идею")
        .add_message(text="❌ Отклонить идею")
        .add_message(text="✅ Одобрить заявку")
        .add_message(text="❌ Отклонить заявку")
        .add_message(text="✅ Одобрить отчёт")
        .add_message(text="❌ Отклонить отчёт")
        .add_message(text="🏅 Призовые баллы")
        .add_message(text="🎁 Дополнительные баллы")
        .add_message(text="🧾 Журнал начислений")
        .add_message(text="⚙️ Лимиты")
        .add_message(text="📊 Панель месяца")
        .add_message(text="🔄 Новый учебный год")
        .add_message(text="↩️ Главное меню")
    )
    return keyboard.adjust(2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1).build()


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
    # MAX allows at most 30 keyboard rows and 7 buttons per row. Keep short
    # choice lists easy to read, and put longer lists into enough columns to
    # stay within the row limit (for example, a whole-school class list).
    columns = min(7, max(1, (len(labels) + 29) // 30))
    return keyboard.adjust(columns).build()


BONUS_CATEGORY_BUTTONS = {
    "📍 Пунктуальность": ("punctuality", "Пунктуальность", 15),
    "📘 Посещаемость": ("attendance", "Посещаемость", 15),
    "🧭 Дисциплина": ("discipline", "Дисциплина", 25),
    "👔 Опрятность и форма": ("tidiness", "Опрятность и форма", 15),
}


def teacher_class_selection_keyboard(
    classes: list[tuple[str, str]], selected: set[str],
):
    labels = [
        f"{'✅' if name in selected else '⬜'} {name} · {rating_label(age_group)}"
        for name, age_group in classes
    ]
    return options_keyboard(*labels, "✅ Готово", "↩️ Отмена")


def class_button_names(classes: list[tuple[str, str]]) -> dict[str, str]:
    return {
        f"{name} · {rating_label(age_group)}": name
        for name, age_group in classes
    }


async def answer_lines(message, lines: list[str], keyboard) -> None:
    """Send the full ranking without exceeding MAX's message size."""
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 3500:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    if chunks:
        await message.answer(chunks[-1], keyboard=keyboard)


def report_actions_keyboard():
    return options_keyboard("✅ Одобрить отчёт", "❌ Отклонить отчёт")


def teacher_application_actions_keyboard():
    return options_keyboard("✅ Одобрить заявку", "❌ Отклонить заявку")


def report_confirmation_keyboard(confirm_label: str):
    return options_keyboard(confirm_label, "↩️ Отмена")


def context_keyboard(context: str):
    keyboards = {
        "value": options_keyboard("⚙️ Ценность месяца", "↩️ Администрирование"),
        "class": options_keyboard(
            "➕ Добавить класс",
            "🗑 Удалить класс",
            "📚 Классы",
            "↩️ Администрирование",
        ),
        "event": options_keyboard(
            "📅 Добавить мероприятие", "🎪 Мероприятия", "↩️ Администрирование"
        ),
        "role": options_keyboard(
            "🛡 Назначить роль", "👥 Пользователи", "↩️ Администрирование"
        ),
        "idea": options_keyboard("✅ Одобрить идею", "❌ Отклонить идею"),
        "report": report_actions_keyboard(),
        "teacher": teacher_application_actions_keyboard(),
        "teacher_codes": options_keyboard(
            "🔑 Создать код учителя",
            "🔑 Коды учителей",
            "↩️ Администрирование",
        ),
        "award": options_keyboard("🏅 Призовые баллы", "↩️ Администрирование"),
        "bonus": options_keyboard("🎁 Дополнительные баллы", "↩️ Администрирование"),
        "limit": options_keyboard("⚙️ Лимиты", "↩️ Администрирование"),
    }
    return keyboards[context]


def users_role_keyboard():
    return options_keyboard(
        "👨‍🎓 Ученики",
        "👩‍🏫 Учителя",
        "👑 Администраторы",
        "↩️ Администрирование",
    )


def user_context_keyboard(*labels: str, is_admin: bool = False):
    buttons = list(labels) + ["↩️ Главное меню"]
    if is_admin:
        buttons.append("⚙️ Администрирование")
    return options_keyboard(*buttons)


def rating_keyboard(is_admin: bool = False):
    return user_context_keyboard(
        "📊 Рейтинг текущего месяца",
        "🏅 Рейтинг учебного года",
        "📅 История рейтингов",
        "↩️ Выбрать категорию",
        is_admin=is_admin,
    )


def rating_group_keyboard(age_groups: list[str], is_admin: bool = False):
    labels = [AGE_GROUP_LABELS[group] for group in age_groups if group in AGE_GROUP_LABELS]
    return user_context_keyboard(*labels, is_admin=is_admin)


RATING_DETAIL_PAGE_SIZE = 20


def rating_detail_keyboard(class_names: list[str], page: int, is_admin: bool = False):
    start = page * RATING_DETAIL_PAGE_SIZE
    page_names = class_names[start : start + RATING_DETAIL_PAGE_SIZE]
    labels = [f"🔎 {name}" for name in page_names]
    if start > 0:
        labels.append("⬅️ Предыдущие классы")
    if start + RATING_DETAIL_PAGE_SIZE < len(class_names):
        labels.append("➡️ Следующие классы")
    labels.extend([
        "📊 Рейтинг текущего месяца",
        "🏅 Рейтинг учебного года",
        "📅 История рейтингов",
        "↩️ К рейтингу",
        "↩️ Выбрать категорию",
    ])
    if is_admin:
        labels.append("⚙️ Администрирование")
    return options_keyboard(*labels)


async def available_rating_groups(user_id: int) -> list[str]:
    if await is_moderator_user(user_id):
        return list(AGE_GROUP_LABELS)
    user_class = await get_user_class(user_id)
    return [user_class[1]] if user_class and user_class[1] in AGE_GROUP_LABELS else []


async def begin_rating_selection(message: MessageCreated) -> None:
    user_id = message.user_id
    if user_id is None:
        await message.answer("Не удалось определить пользователя MAX.")
        return
    age_groups = await available_rating_groups(user_id)
    if not age_groups:
        await message.answer(
            "Сначала привяжите пользователя к классу через /start.",
            keyboard=registration_keyboard(),
        )
        return
    pending_rating_context[user_id] = {"step": "age_group"}
    await message.answer(
        "🏆 Выберите возрастную группу для рейтинга:",
        keyboard=rating_group_keyboard(age_groups, await is_admin_user(user_id)),
    )


def target_group_is_within_age_group(target_group: str, age_group: str) -> bool:
    bounds = {"1-4": (1, 4), "5-8": (5, 8), "9-11": (9, 11)}
    if age_group not in bounds:
        return False
    low, high = bounds[age_group]
    normalized = target_group.replace("–", "-").replace("—", "-")
    mentioned_grades: list[int] = []
    for start, end in re.findall(
        r"(?<!\d)(1[01]|[1-9])\s*(?:-|по)\s*(1[01]|[1-9])(?!\d)",
        normalized,
        flags=re.IGNORECASE,
    ):
        mentioned_grades.extend((int(start), int(end)))
    mentioned_grades.extend(
        int(value)
        for value in re.findall(
            r"(?<!\d)(1[01]|[1-9])\s*(?:-?(?:й|ый|ой|ого|го|му)\s*)?"
            r"(?:класс(?:а|ов|е|ы)?|кл\.?)",
            normalized,
            flags=re.IGNORECASE,
        )
    )
    return all(low <= grade <= high for grade in mentioned_grades)


def rating_label(age_group: str) -> str:
    return AGE_GROUP_LABELS.get(age_group, age_group)


async def show_current_month_rating(message: MessageCreated, age_group: str) -> None:
    period_month, value, rating = await list_current_month_rating(age_group)
    lines = [
        f"📊 Рейтинг {rating_label(age_group)} за {period_month.strftime('%m.%Y')}:"
    ]
    if value:
        lines.append(f"🎯 Ценность: {value}")
    else:
        lines.append("Ценность месяца ещё не введена администратором.")
    lines.append("")
    last_points = None
    current_rank = 0
    for position, (name, _group, points, is_winner) in enumerate(rating, start=1):
        if points != last_points:
            current_rank = position
            last_points = points
        badge = "🏆 " if is_winner else ""
        lines.append(f"{current_rank}. {badge}{name} — {points} баллов")
    class_names = [name for name, _group, _points, _winner in rating]
    pending_rating_context[message.user_id] = {
        "step": "class_details",
        "age_group": age_group,
        "view": "month",
        "period_month": period_month.isoformat(),
        "academic_year": f"{period_month.year if period_month.month >= 9 else period_month.year - 1}/"
        f"{period_month.year + 1 if period_month.month >= 9 else period_month.year}",
        "class_names": class_names,
        "page": 0,
    }
    await answer_lines(
        message,
        lines,
        rating_detail_keyboard(class_names, 0, await is_admin_user(message.user_id)),
    )


async def show_academic_year_rating(message: MessageCreated, age_group: str) -> None:
    year_label, closed_count, complete, rating = await list_academic_year_rating(age_group)
    lines = [f"🏅 Рейтинг {rating_label(age_group)} за учебный год {year_label}:"]
    lines.append(
        "Итоговый рейтинг сформирован."
        if complete
        else f"Предварительный рейтинг: закрыто месяцев {closed_count} из 9."
    )
    lines.append("")
    last_points = None
    current_rank = 0
    for position, (name, _group, points, is_winner) in enumerate(rating, start=1):
        if points != last_points:
            current_rank = position
            last_points = points
        badge = "👑 " if is_winner else ""
        lines.append(f"{current_rank}. {badge}{name} — {points} баллов")
    start_year = int(year_label.split("/", 1)[0])
    class_names = [name for name, _group, _points, _winner in rating]
    pending_rating_context[message.user_id] = {
        "step": "class_details",
        "age_group": age_group,
        "view": "year",
        "academic_year": year_label,
        "period_start": date(start_year, 9, 1).isoformat(),
        "period_end": date(start_year + 1, 5, 1).isoformat(),
        "class_names": class_names,
        "page": 0,
    }
    await answer_lines(
        message,
        lines,
        rating_detail_keyboard(class_names, 0, await is_admin_user(message.user_id)),
    )


async def show_rating_history(message: MessageCreated, age_group: str) -> None:
    history = await list_rating_history(age_group)
    if not history:
        await message.answer(
            f"Для группы {rating_label(age_group)} закрытых месяцев пока нет.",
            keyboard=rating_keyboard(await is_admin_user(message.user_id)),
        )
        return
    lines = [f"📅 История рейтингов · {rating_label(age_group)}"]
    for period_month, value, winners, winner_points in history:
        winner_text = ", ".join(winners) if winners else "победитель не определён"
        points_text = f" — {winner_points} баллов" if winner_points is not None else ""
        lines.append(
            f"\n{period_month.strftime('%m.%Y')} · {value}\n"
            f"🏆 {winner_text}{points_text}"
        )
    history_keyboard = options_keyboard(
        *(f"📊 Месяц {row[0].strftime('%Y-%m')}" for row in history[:12]),
        "📊 Рейтинг текущего месяца",
        "🏅 Рейтинг учебного года",
        "↩️ Выбрать категорию",
        "🏆 Рейтинг",
    )
    await answer_lines(message, lines, history_keyboard)


async def show_archived_month_rating(
    message: MessageCreated,
    period_month: date,
    age_group: str,
) -> None:
    result = await get_month_rating(period_month, age_group)
    if result is None:
        await message.answer("Этот месяц ещё не открывался.")
        return
    value, status, rating = result
    lines = [
        f"📊 Рейтинг {rating_label(age_group)} за "
        f"{period_month.strftime('%m.%Y')} · {value}"
    ]
    lines.append(
        "Месяц закрыт. 🏆 — победители."
        if status == "closed"
        else "Приём отчётов ещё открыт до 5-го числа следующего месяца."
    )
    lines.append("Своевременно сданные отчёты учитываются после проверки; итоги могут уточняться.")
    last_points, rank = None, 0
    for position, (name, _group, points, winner) in enumerate(rating, 1):
        if points != last_points:
            rank, last_points = position, points
        badge = "🏆 " if winner else ""
        lines.append(f"{rank}. {badge}{name} — {points} баллов")
    class_names = [name for name, _group, _points, _winner in rating]
    year_start = period_month.year if period_month.month >= 9 else period_month.year - 1
    pending_rating_context[message.user_id] = {
        "step": "class_details",
        "age_group": age_group,
        "view": "archived_month",
        "period_month": period_month.isoformat(),
        "academic_year": f"{year_start}/{year_start + 1}",
        "class_names": class_names,
        "page": 0,
    }
    await answer_lines(
        message,
        lines,
        rating_detail_keyboard(class_names, 0, await is_admin_user(message.user_id)),
    )


async def show_rating_class_details(
    message: MessageCreated,
    class_name: str,
) -> None:
    user_id = message.user_id
    state = pending_rating_context.get(user_id)
    if not state or state.get("step") != "class_details":
        await begin_rating_selection(message)
        return
    class_names = state.get("class_names", [])
    if not isinstance(class_names, list) or class_name not in class_names:
        await message.answer("Выберите класс кнопкой из показанного рейтинга.")
        return
    age_group = str(state.get("age_group", ""))
    if age_group not in await available_rating_groups(user_id):
        await begin_rating_selection(message)
        return
    if state.get("view") in {"month", "archived_month"}:
        period_month = date.fromisoformat(str(state["period_month"]))
        period_start = period_month
        period_end = period_month
        period_label = period_month.strftime("%m.%Y")
    else:
        period_start = date.fromisoformat(str(state["period_start"]))
        period_end = date.fromisoformat(str(state["period_end"]))
        period_label = str(state.get("academic_year", "учебный год"))

    breakdown = await get_class_rating_breakdown(
        class_name,
        age_group,
        str(state["academic_year"]),
        period_start,
        period_end,
    )
    if breakdown is None:
        await message.answer("Не удалось найти класс в этом учебном году.")
        return

    lines = [
        f"🔎 Класс {class_name} · {rating_label(age_group)}",
        f"Период: {period_label}",
        f"Всего: {breakdown['total']} баллов",
        "",
    ]
    categories = breakdown["categories"]
    for key in ("reports", "ideas", "prizes", "bonuses", "other"):
        bucket = categories[key]
        lines.append(
            f"{bucket['label']}: {bucket['points']} баллов · записей: {bucket['count']}"
        )
        entries = bucket["entries"]
        for entry in entries[-5:]:
            lines.append(f"  • {entry['reason']} — {entry['points']} б.")
        if len(entries) > 5:
            lines.append(f"  • Ещё записей: {len(entries) - 5}")

    penalties = breakdown["penalties"]
    lines.append(f"\n⚠️ Дисциплинарные замечания: {len(penalties)}")
    for penalty in penalties[-10:]:
        month_label = penalty["period_month"].strftime("%m.%Y")
        note = (penalty["note"] or "").strip()
        detail = f" · {note}" if note else ""
        lines.append(
            f"  • {month_label} · учитель ID {penalty['teacher_max_user_id']}{detail}"
        )
    if len(penalties) > 10:
        lines.append(f"  • Ещё замечаний: {len(penalties) - 10}")

    await message.answer(
        "\n".join(lines),
        keyboard=options_keyboard(
            "📊 Рейтинг текущего месяца",
            "🏅 Рейтинг учебного года",
            "📅 История рейтингов",
            "↩️ К рейтингу",
            "↩️ Выбрать категорию",
            *(["⚙️ Администрирование"] if await is_admin_user(user_id) else []),
        ),
    )


async def send_month_dashboard(message: MessageCreated) -> None:
    data = await get_admin_month_dashboard()
    current_status = {
        "open": "открыт",
        "closed": "закрыт",
        "not_open": "ещё не открыт",
    }.get(str(data["current_status"]), str(data["current_status"]))
    report_status = {
        "open": "приём открыт",
        "closed": "срок истёк, месяц закрыт",
        "not_open": "месяц ещё не открыт",
    }.get(str(data["report_status"]), str(data["report_status"]))
    previous_status = {
        "open": "ещё открыт для отчётов",
        "closed": "закрыт",
        "not_open": "не открывался",
    }.get(str(data["previous_status"]), str(data["previous_status"]))
    value = data["current_value"] or "не задана"
    current_month = data["current_month"].strftime("%m.%Y")
    report_month = data["report_month"].strftime("%m.%Y")
    deadline = data["report_deadline"].strftime("%d.%m.%Y")
    await message.answer(
        f"📊 Панель месяца · {current_month}\n\n"
        f"🎯 Ценность: {value}\n"
        f"🔒 Статус месяца: {current_status}\n"
        f"📅 Предыдущий месяц ({data['previous_month'].strftime('%m.%Y')}): {previous_status}\n"
        f"📋 Отчёты за {report_month}: до {deadline} включительно · {report_status}\n\n"
        "⏳ На проверке:\n"
        f"• заявки учителей: {data['pending_teacher_applications']}\n"
        f"• отчёты: {data['pending_reports']}\n"
        f"• идеи: {data['pending_ideas']}",
        keyboard=admin_keyboard(),
    )


async def send_school_year_rollover_preview(message: MessageCreated) -> None:
    preview = await get_school_year_rollover_preview()
    lines = [
        "🔄 Подготовка нового учебного года",
        f"Переход: {preview['from_year']} → {preview['to_year']}",
    ]
    if preview["already_done"]:
        lines.append("Этот учебный год уже переводили.")
        await message.answer("\n".join(lines), keyboard=admin_keyboard())
        return
    if not preview["promote"] and not preview["graduate"]:
        lines.append("В текущем учебном году нет активных классов для переноса.")
        await message.answer("\n".join(lines), keyboard=admin_keyboard())
        return
    lines.extend([
        f"Классы для перевода: {len(preview['promote'])}",
        f"Выпускные классы 11-х: {len(preview['graduate'])}",
        f"Учеников и учителей переместится: {preview['moved_users']}",
        f"Назначений учителей будет перенесено: {preview['teacher_assignments']}",
        f"Заявок учителей затронет: {preview['pending_teacher_applications']}",
        f"Заявок без продолжающегося класса будет закрыто: {len(preview['rejected_applicants'])}",
        f"Пользователей 11-х классов отвяжется: {preview['unassigned_users']}",
        "",
    ])
    lines.extend(
        f"• {old_name} → {new_name}"
        for _class_id, old_name, new_name, _group in preview["promote"]
    )
    if preview["graduate"]:
        lines.append("\nАрхив выпускных классов: " + ", ".join(
            name for _class_id, name in preview["graduate"]
        ))
    lines.append(
        "\nИстория баллов останется у старых классов. Ученики 11-х будут отвязаны; "
        "новые 1-е классы добавляются отдельно после перехода."
    )
    if preview["errors"]:
        lines.append("\nПереход пока невозможен:")
        lines.extend(f"• {error}" for error in preview["errors"])
        await answer_lines(message, lines, admin_keyboard())
        return
    pending_school_year_rollover[message.user_id] = {
        "from_year": str(preview["from_year"]),
        "preview_token": str(preview["preview_token"]),
    }
    await answer_lines(
        message,
        lines,
        options_keyboard("✅ Выполнить переход", "↩️ Отмена"),
    )


async def send_deadline_reminder_if_due() -> None:
    now = datetime.now(MOSCOW_TZ)
    if now.hour < 9 or now.day not in {1, 3, 5}:
        return
    reminder = await get_deadline_reminder_info(now.date())
    if reminder is None:
        return
    period_month, pending_reports, sent_to = reminder
    recipients = set(get_admin_ids())
    recipients.update(await list_user_ids_by_roles({"admin"}))
    recipients.difference_update(sent_to)
    if not recipients:
        return
    if now.day == 5:
        deadline_text = "Сегодня последний день приёма отчётов."
    else:
        deadline_text = "Отчёты принимаются до 5-го числа включительно."
    text = (
        f"⏰ Напоминание: проверьте отчёты за {period_month.strftime('%m.%Y')}.\n"
        f"{deadline_text}\n"
        f"Непроверенных отчётов за этот месяц: {pending_reports}.\n"
        "Откройте в боте «📊 Панель месяца» для проверки очередей."
    )
    recipient_ids = sorted(recipients)
    results = await asyncio.gather(
        *(notify_user(user_id, text) for user_id in recipient_ids)
    )
    await asyncio.gather(*(
        mark_deadline_reminder_sent(now.date(), user_id)
        for user_id, sent in zip(recipient_ids, results)
        if sent
    ))


def event_entry_keyboard(is_admin: bool = False):
    return user_context_keyboard(
        "🤝 Принять участие",
        "📋 Подать отчёт",
        is_admin=is_admin,
    )


async def send_users_by_role(message: MessageCreated, role: str) -> None:
    role_titles = {
        "student": "👨‍🎓 Ученики",
        "teacher": "👩‍🏫 Учителя",
        "admin": "👑 Администраторы",
    }
    users = [user for user in await list_users_with_details() if user[1] == role]

    # Администраторы из ADMIN_IDS могут ещё не иметь записи в таблице users.
    if role == "admin":
        known_ids = {user[0] for user in users}
        users.extend(
            (admin_id, "admin", None, None, None)
            for admin_id in sorted(get_admin_ids())
            if admin_id not in known_ids
        )
        users.sort(key=lambda user: user[0])

    if not users:
        await message.answer(
            f"{role_titles[role]} пока нет.",
            keyboard=context_keyboard("role"),
        )
        return

    lines = [f"{role_titles[role]} (ID пользователей):"]
    for max_user_id, _user_role, class_name, full_name, subject in users:
        if role == "teacher" and full_name:
            teacher_details = full_name
            if subject:
                teacher_details += f" — {subject}"
            lines.append(f"• {teacher_details}\n  ID: {max_user_id}")
            if class_name:
                lines.append(f"  Классы: {class_name}")
            continue
        class_text = f" — класс: {class_name}" if class_name else ""
        lines.append(f"• {max_user_id}{class_text}")
    await message.answer("\n".join(lines), keyboard=context_keyboard("role"))


def event_selection_keyboard(events: list[tuple[int, str, str, str, str, str]]):
    return options_keyboard(
        *(f"№{event_id}" for event_id, *_ in events),
        "↩️ Назад",
    )


def report_selection_keyboard(events: list[tuple[int, str, str, str, str, str]]):
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


async def events_visible_to_user(
    user_id: int | None,
) -> list[tuple[int, str, str, str, str, str]]:
    if await is_moderator_user(user_id):
        return await list_events()
    if user_id is None:
        return []
    user_class = await get_user_class(user_id)
    if user_class is None:
        return []
    return await list_events(user_class[1])


async def can_change_admin_role(target_id: int, new_role: str) -> bool:
    """Не позволяет удалить последнего администратора проекта."""
    if target_id in get_admin_ids() and new_role != "admin":
        return False
    if new_role == "admin":
        return True
    if await get_user_role(target_id) != "admin":
        return True

    admin_ids = set(get_admin_ids())
    admin_ids.update(await list_user_ids_by_roles({"admin"}))
    admin_ids.discard(target_id)
    return bool(admin_ids)


async def entry_keyboard(user_id: int | None):
    is_admin = await is_admin_user(user_id)
    user_class = await get_user_class(user_id) if user_id is not None else None
    role = await get_user_role(user_id) if user_id is not None else None
    teacher_classes = (
        await list_teacher_assigned_classes(user_id)
        if user_id is not None and role == "teacher"
        else []
    )
    if is_admin or user_class is not None or teacher_classes:
        return main_keyboard(is_admin, is_teacher=role == "teacher")
    return registration_keyboard()


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


async def start_screen(user_id: int) -> tuple[str, object]:
    is_admin = await is_admin_user(user_id)
    if is_admin:
        await ensure_user_role(user_id, "admin")
        return (
            "Привет! Вы вошли как администратор проекта «Актив».\n"
            "Выберите раздел меню:",
            main_keyboard(is_admin=True),
        )

    user_class = await get_user_class(user_id)
    role = await get_user_role(user_id)
    teacher_classes = (
        await list_teacher_assigned_classes(user_id)
        if role == "teacher"
        else []
    )
    if user_class is None:
        if teacher_classes:
            class_names = ", ".join(
                f"{name} ({rating_label(age_group)})"
                for name, age_group in teacher_classes
            )
            return (
                f"Привет! Вы зарегистрированы как учитель. Ваши классы: {class_names}.\n"
                "Выберите раздел меню:",
                main_keyboard(is_teacher=True),
            )
        application = await get_teacher_application(user_id)
        if application is not None and application[1] == "pending":
            return (
                "Ваша заявка на регистрацию учителя уже отправлена. "
                "Ожидайте подтверждения администратора.",
                registration_keyboard(),
            )
        if application is not None and application[1] == "rejected":
            reason = application[4] or "Причина не указана."
            return (
                f"Заявка на регистрацию учителя отклонена.\nПричина: {reason}\n"
                "Можно подать новую заявку.",
                registration_keyboard(),
            )
        return (
            "Добро пожаловать в «Актив»!\n"
            "Выберите тип регистрации:",
            registration_keyboard(),
        )

    if role == "teacher":
        class_names = ", ".join(
            f"{name} ({rating_label(age_group)})"
            for name, age_group in teacher_classes
        ) or f"{user_class[0]} ({rating_label(user_class[1])})"
        return (
            f"Привет! Вы зарегистрированы как учитель. Ваши классы: {class_names}.\n"
            "Выберите раздел меню:",
            main_keyboard(is_teacher=True),
        )
    return (
        f"Привет! Вы привязаны к классу {user_class[0]} ({rating_label(user_class[1])}).\n"
        "Выберите раздел меню:",
        main_keyboard(),
    )


@dispatcher.bot_started()
async def bot_started_handler(update: BotStarted) -> None:
    user_id = update.user.user_id
    text, keyboard = await start_screen(user_id)
    await bot.send_message(
        user_id=user_id, text=text,
        attachments=[InlineKeyboardAttachmentRequest.factory(keyboard)],
    )


@dispatcher.message_created(CommandStart())
async def start_handler(message: MessageCreated) -> None:
    user_id = message.user_id
    if user_id is None:
        await message.answer("Не удалось определить пользователя MAX. Попробуйте ещё раз.")
        return
    text, keyboard = await start_screen(user_id)
    await message.answer(text, keyboard=keyboard)


@dispatcher.message_created()
async def echo_handler(message: MessageCreated) -> None:
    text = message.text or "Сообщение получено, но в нём нет текста."
    admin_events_view = False

    if text.strip().lower() == "/cancel" or text.strip() == "↩️ Отмена":
        pending_class_code_users.discard(message.user_id)
        pending_ideas.pop(message.user_id, None)
        pending_admin_actions.pop(message.user_id, None)
        pending_admin_forms.pop(message.user_id, None)
        pending_users_role.discard(message.user_id)
        pending_event_selection_users.discard(message.user_id)
        pending_report_selection_users.discard(message.user_id)
        pending_user_reports.pop(message.user_id, None)
        pending_teacher_forms.pop(message.user_id, None)
        pending_teacher_penalties.pop(message.user_id, None)
        pending_rating_context.pop(message.user_id, None)
        pending_school_year_rollover.pop(message.user_id, None)
        await message.answer(
            "Текущий сценарий отменён.",
            keyboard=await entry_keyboard(message.user_id),
        )
        return

    if text == "ℹ️ Инструкция":
        await message.answer(
            "ℹ️ Краткая инструкция\n\n"
            "👨‍🎓 Ученик: выберите регистрацию ученика и введите код класса.\n"
            "👩‍🏫 Учитель: заполните заявку и используйте персональный код, "
            "выданный администратором. В заявке отметьте все классы, где преподаёте. "
            "Кнопка «⚠️ Штрафы классу» позволяет зафиксировать замечание для одного "
            "из классов, закреплённых за учителем.\n"
            "🎪 Афиша — запись класса на мероприятия.\n"
            "📋 Подать отчёт — отправка одного отчёта от класса за мероприятие.\n"
            "💡 Подать идею — предложить новое мероприятие.\n\n"
            "Для возврата используйте кнопку «▶️ Старт».",
            keyboard=await entry_keyboard(message.user_id),
        )
        return

    if text == "▶️ Старт":
        pending_class_code_users.discard(message.user_id)
        pending_ideas.pop(message.user_id, None)
        pending_admin_actions.pop(message.user_id, None)
        pending_admin_forms.pop(message.user_id, None)
        pending_users_role.discard(message.user_id)
        pending_event_selection_users.discard(message.user_id)
        pending_report_selection_users.discard(message.user_id)
        pending_user_reports.pop(message.user_id, None)
        pending_teacher_forms.pop(message.user_id, None)
        pending_teacher_penalties.pop(message.user_id, None)
        pending_rating_context.pop(message.user_id, None)
        pending_school_year_rollover.pop(message.user_id, None)
        await start_handler(message)
        return

    if message.user_id in pending_school_year_rollover:
        if text == "✅ Выполнить переход":
            if not await is_admin_user(message.user_id):
                pending_school_year_rollover.pop(message.user_id, None)
                await message.answer("Переход учебного года доступен только администраторам.")
                return
            expected = pending_school_year_rollover.pop(message.user_id)
            status, result = await rollover_school_year(
                expected["from_year"],
                expected["preview_token"],
                message.user_id,
            )
            if status == "ok":
                await message.answer(
                    f"✅ Учебный год переведён: {result['from_year']} → {result['to_year']}.\n"
                    f"Создано классов-переходов: {len(result['promote'])}. "
                    f"Архивировано 11-х классов: {len(result['graduate'])}.\n"
                    f"Перемещено пользователей: {result['moved_users']}; "
                    f"отвязано выпускников: {result['unassigned_users']}.\n\n"
                    "Новые коды классов и учителей доступны в разделах «📚 Классы» "
                    "и «🔑 Коды учителей». Создайте новые 1-е классы отдельно.",
                    keyboard=admin_keyboard(),
                )
                for applicant_id in result["rejected_applicants"]:
                    await notify_user(
                        applicant_id,
                        "Заявку на регистрацию учителя закрыли при подготовке нового учебного года: "
                        "указанный класс 11-го года выпуска завершён, а в заявке не осталось "
                        "других действующих классов. Попросите администратора создать новый код приглашения.",
                    )
            elif status == "already_done":
                await message.answer("Этот учебный год уже переводили.", keyboard=admin_keyboard())
            elif status == "conflict":
                lines = ["Переход не выполнен. Исправьте проблемы и повторите просмотр:"]
                lines.extend(f"• {error}" for error in result["errors"])
                await message.answer("\n".join(lines), keyboard=admin_keyboard())
            elif status == "year_changed":
                await message.answer(
                    "Текущий учебный год уже изменился. Откройте переход заново.",
                    keyboard=admin_keyboard(),
                )
            elif status == "preview_changed":
                await message.answer(
                    "Данные изменились после предварительного просмотра. Переход не выполнен. "
                    "Откройте «🔄 Новый учебный год» и проверьте обновлённый список.",
                    keyboard=admin_keyboard(),
                )
            else:
                await message.answer(
                    "Не удалось выполнить переход: в текущем учебном году нет активных классов.",
                    keyboard=admin_keyboard(),
                )
            return
        if text != "↩️ Отмена":
            await message.answer(
                "Подтвердите переход кнопкой или нажмите «↩️ Отмена».",
                keyboard=options_keyboard("✅ Выполнить переход", "↩️ Отмена"),
            )
            return

    if text == "📊 Панель месяца":
        if not await is_admin_user(message.user_id):
            await message.answer("Раздел доступен только администраторам.")
            return
        pending_admin_forms.pop(message.user_id, None)
        pending_admin_actions.pop(message.user_id, None)
        await send_month_dashboard(message)
        return

    if text == "🔄 Новый учебный год":
        if not await is_admin_user(message.user_id):
            await message.answer("Переход учебного года доступен только администраторам.")
            return
        pending_admin_forms.pop(message.user_id, None)
        pending_admin_actions.pop(message.user_id, None)
        await send_school_year_rollover_preview(message)
        return

    if text in {"🏆 Рейтинг", "↩️ Выбрать категорию"}:
        pending_admin_forms.pop(message.user_id, None)
        await begin_rating_selection(message)
        return

    rating_state = pending_rating_context.get(message.user_id)
    if text.startswith("🔎 "):
        pending_admin_forms.pop(message.user_id, None)
        await show_rating_class_details(message, text.removeprefix("🔎 ").strip())
        return
    if text in {"➡️ Следующие классы", "⬅️ Предыдущие классы"}:
        if rating_state and rating_state.get("step") == "class_details":
            names = rating_state.get("class_names", [])
            if isinstance(names, list):
                current_page = int(rating_state.get("page", 0))
                delta = 1 if text.startswith("➡️") else -1
                page = max(0, min((len(names) - 1) // RATING_DETAIL_PAGE_SIZE, current_page + delta))
                rating_state["page"] = page
                await message.answer(
                    "Выберите класс, чтобы посмотреть состав баллов:",
                    keyboard=rating_detail_keyboard(
                        names, page, await is_admin_user(message.user_id)
                    ),
                )
                return
    if text == "↩️ К рейтингу" and rating_state:
        pending_admin_forms.pop(message.user_id, None)
        age_group = str(rating_state.get("age_group", ""))
        view = rating_state.get("view")
        if view == "month":
            await show_current_month_rating(message, age_group)
        elif view == "year":
            await show_academic_year_rating(message, age_group)
        elif view == "archived_month":
            await show_archived_month_rating(
                message,
                date.fromisoformat(str(rating_state["period_month"])),
                age_group,
            )
        else:
            await begin_rating_selection(message)
        return
    if text == "↩️ Выбрать категорию" and rating_state and rating_state.get("step") == "class_details":
        pending_admin_forms.pop(message.user_id, None)
        await begin_rating_selection(message)
        return

    if message.user_id in pending_users_role:
        if text == "↩️ Администрирование":
            pending_users_role.discard(message.user_id)
            await admin_message(message)
            return
        role_by_button = {
            "👨‍🎓 Ученики": "student",
            "👩‍🏫 Учителя": "teacher",
            "👑 Администраторы": "admin",
        }
        role = role_by_button.get(text)
        if role is None:
            await message.answer(
                "Выберите категорию кнопкой.",
                keyboard=users_role_keyboard(),
            )
            return
        pending_users_role.discard(message.user_id)
        await send_users_by_role(message, role)
        return

    if text == "👨‍🎓 Регистрация ученика":
        pending_teacher_forms.pop(message.user_id, None)
        pending_class_code_users.add(message.user_id)
        await message.answer(
            "Введите код приглашения вашего класса.\n"
            "Для демонстрации используется код AKTIV-9A.",
            keyboard=cancel_keyboard(),
        )
        return

    if text == "👩‍🏫 Регистрация учителя":
        pending_class_code_users.discard(message.user_id)
        pending_teacher_forms[message.user_id] = {"step": "full_name"}
        await message.answer(
            "Шаг 1 из 3. Введите ФИО учителя.",
            keyboard=cancel_keyboard(),
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
            age_group = idea.get("age_group", "")
            await message.answer(
                f"Шаг 3 из 3. Для кого подходит идея внутри вашей группы "
                f"{AGE_GROUP_LABELS.get(age_group, age_group)}? "
                "Укажите только классы из этого диапазона, например «все "
                f"{AGE_GROUP_LABELS.get(age_group, age_group)}».",
                keyboard=cancel_keyboard(),
            )
            return

        if idea["step"] == "target_group":
            if not value or len(value) > 100:
                await message.answer("Укажите группу длиной до 100 символов.")
                return
            if not target_group_is_within_age_group(value, idea.get("age_group", "")):
                allowed_group = AGE_GROUP_LABELS.get(idea.get("age_group", ""), "вашей группы")
                await message.answer(
                    f"Идею можно предложить только своей возрастной группе: {allowed_group}. "
                    "Исправьте аудиторию и отправьте ещё раз.",
                    keyboard=cancel_keyboard(),
                )
                return
            idea_status, idea_id = await create_idea(
                message.user_id,
                idea["title"],
                idea["description"],
                value,
            )
            if idea_status == "period_not_open":
                await message.answer("Месяц ещё не открыт. Администратор должен ввести ценность месяца.")
                return
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
                f"Категория: {rating_label(idea['age_group'])}.\n"
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

    if message.user_id in pending_teacher_forms:
        form = pending_teacher_forms[message.user_id]
        value = text.strip()

        if form["step"] == "full_name":
            if len(value) < 2 or len(value) > 120:
                await message.answer("Укажите ФИО длиной от 2 до 120 символов.")
                return
            form["full_name"] = value
            form["step"] = "subject"
            await message.answer(
                "Шаг 2 из 3. Введите предмет или должность, например: математика.",
                keyboard=cancel_keyboard(),
            )
            return

        if form["step"] == "subject":
            if len(value) < 2 or len(value) > 100:
                await message.answer("Предмет должен содержать от 2 до 100 символов.")
                return
            form["subject"] = value
            form["step"] = "invite_code"
            await message.answer(
                "Шаг 3 из 3. Введите персональный код учителя, выданный администратором.",
                keyboard=cancel_keyboard(),
            )
            return

        if form["step"] == "invite_code":
            invite = await get_teacher_invite(value)
            if invite is None:
                await message.answer(
                    "Код не найден, уже использован или отозван. Проверьте код и отправьте его ещё раз.",
                    keyboard=cancel_keyboard(),
                )
                return
            form["invite_code"] = value.upper()
            form["class_name"] = invite[2]
            form["selected_classes"] = invite[2]
            form["step"] = "classes"
            classes = await list_classes_for_removal()
            await message.answer(
                f"Код подтверждён для класса {form['class_name']} — он уже выбран.\n"
                "Отметьте остальные классы, где вы преподаёте, затем нажмите «✅ Готово».\n"
                "Список будет передан администратору вместе с заявкой.",
                keyboard=teacher_class_selection_keyboard(classes, {form["class_name"]}),
            )
            return

        if form["step"] == "classes":
            classes = await list_classes_for_removal()
            choices = class_button_names(classes)
            selected = set(filter(None, form.get("selected_classes", "").split("\n")))
            if value == "✅ Готово":
                if not selected:
                    await message.answer(
                        "Выберите хотя бы один класс.",
                        keyboard=teacher_class_selection_keyboard(classes, selected),
                    )
                    return
                form["selected_classes"] = "\n".join(sorted(selected))
                form["step"] = "confirm"
                class_names = ", ".join(sorted(selected))
                await message.answer(
                    f"ФИО: {form['full_name']}\n"
                    f"Предмет: {form['subject']}\n"
                    f"Классы: {class_names}\n\n"
                    "Отправить заявку администратору?",
                    keyboard=confirmation_keyboard("✅ Отправить заявку"),
                )
                return

            button_name = value[2:] if value.startswith(("✅ ", "⬜ ")) else ""
            class_name = choices.get(button_name)
            if class_name is None:
                await message.answer(
                    "Отметьте классы кнопками и нажмите «✅ Готово».",
                    keyboard=teacher_class_selection_keyboard(classes, selected),
                )
                return
            if class_name == form["class_name"]:
                await message.answer(
                    "Класс, подтверждённый персональным кодом, уже включён в заявку.",
                    keyboard=teacher_class_selection_keyboard(classes, selected),
                )
                return
            if class_name in selected:
                selected.remove(class_name)
            else:
                selected.add(class_name)
            form["selected_classes"] = "\n".join(sorted(selected))
            await message.answer(
                "Выберите все классы, где вы преподаёте, и нажмите «✅ Готово».",
                keyboard=teacher_class_selection_keyboard(classes, selected),
            )
            return

        if form["step"] == "confirm":
            if value == "✏️ Изменить":
                form["step"] = "full_name"
                await message.answer("Введите ФИО учителя заново.", keyboard=cancel_keyboard())
                return
            if value != "✅ Отправить заявку":
                await message.answer(
                    "Нажмите «✅ Отправить заявку» или «✏️ Изменить».",
                    keyboard=confirmation_keyboard("✅ Отправить заявку"),
                )
                return
            result, application_id, class_name = await create_teacher_application(
                message.user_id,
                form["full_name"],
                form["subject"],
                form["invite_code"],
                list(filter(None, form.get("selected_classes", "").split("\n"))),
            )
            pending_teacher_forms.pop(message.user_id, None)
            if result == "already_pending":
                await message.answer(
                    "Ваша заявка уже находится на проверке.",
                    keyboard=registration_keyboard(),
                )
                return
            if result == "already_approved":
                await message.answer(
                    "Ваша заявка уже одобрена.",
                    keyboard=await entry_keyboard(message.user_id),
                )
                return
            if result == "invite_in_use":
                await message.answer(
                    "Этот код уже используется в другой заявке. Получите новый код у администратора.",
                    keyboard=registration_keyboard(),
                )
                return
            if result == "invalid_classes":
                await message.answer(
                    "Список классов изменился. Начните регистрацию заново и выберите активные классы.",
                    keyboard=registration_keyboard(),
                )
                return
            if result != "ok":
                await message.answer(
                    "Код учителя больше недействителен. Получите новый код у администратора.",
                    keyboard=registration_keyboard(),
                )
                return
            await notify_moderators(
                f"👩‍🏫 Новая заявка учителя №{application_id}.\n"
                f"ФИО: {form['full_name']}\n"
                f"Предмет: {form['subject']}\n"
                f"Классы: {', '.join(filter(None, form.get('selected_classes', '').split(chr(10))))}\n"
                "Откройте раздел «👩‍🏫 Заявки учителей»."
            )
            await message.answer(
                "Заявка отправлена администратору. Ожидайте подтверждения.",
                keyboard=registration_keyboard(),
            )
            return

    if message.user_id in pending_teacher_penalties:
        form = pending_teacher_penalties[message.user_id]
        value = text.strip()

        if form["step"] == "class":
            classes = await list_teacher_assigned_classes(message.user_id)
            class_names = class_button_names(classes)
            class_name = class_names.get(value)
            if class_name is None:
                await message.answer(
                    "Выберите класс из списка закреплённых за вами.",
                    keyboard=options_keyboard(*class_names.keys(), "↩️ Отмена"),
                )
                return
            status, penalty_count, cap, current_points = await get_monthly_bonus_status(
                class_name, "discipline"
            )
            if status == "period_not_open":
                pending_teacher_penalties.pop(message.user_id, None)
                await message.answer(
                    "Пока нельзя добавить замечание: администратор ещё не открыл "
                    "текущий месяц, введя его ценность.",
                    keyboard=await entry_keyboard(message.user_id),
                )
                return
            if status != "ok":
                pending_teacher_penalties.pop(message.user_id, None)
                await message.answer(
                    "Не удалось проверить выбранный класс. Попробуйте позже.",
                    keyboard=await entry_keyboard(message.user_id),
                )
                return
            form.update(
                {
                    "class": class_name,
                    "step": "note",
                    "penalty_count": str(penalty_count),
                    "cap": str(cap),
                    "current_points": str(current_points),
                }
            )
            await message.answer(
                f"Класс {class_name}: сейчас {penalty_count} замечаний за месяц; "
                f"максимум за дисциплину — {cap}/25 баллов.\n"
                "Введите короткий комментарий или нажмите «⏭ Без комментария».",
                keyboard=options_keyboard("⏭ Без комментария", "↩️ Отмена"),
            )
            return

        if form["step"] == "note":
            if value == "⏭ Без комментария":
                form["note"] = ""
            elif len(value) > 300:
                await message.answer(
                    "Комментарий должен быть не длиннее 300 символов.",
                    keyboard=options_keyboard("⏭ Без комментария", "↩️ Отмена"),
                )
                return
            else:
                form["note"] = value
            form["step"] = "confirm"
            await message.answer(
                f"Зафиксировать замечание классу {form['class']} за дисциплину?\n"
                f"Комментарий: {form['note'] or 'не указан'}",
                keyboard=confirmation_keyboard("⚠️ Подтвердить замечание"),
            )
            return

        if form["step"] == "confirm":
            if value == "✏️ Изменить":
                form["step"] = "class"
                classes = await list_teacher_assigned_classes(message.user_id)
                class_names = class_button_names(classes)
                await message.answer(
                    "Выберите класс заново:",
                    keyboard=options_keyboard(*class_names.keys(), "↩️ Отмена"),
                )
                return
            if value != "⚠️ Подтвердить замечание":
                await message.answer(
                    "Нажмите «⚠️ Подтвердить замечание» или «✏️ Изменить»."
                )
                return

            result, penalty_count, cap, deducted = await add_discipline_penalty(
                message.user_id,
                form["class"],
                form.get("note") or None,
            )
            pending_teacher_penalties.pop(message.user_id, None)
            if result == "period_not_open":
                await message.answer(
                    "Замечание не сохранено: администратор ещё не открыл текущий месяц.",
                    keyboard=await entry_keyboard(message.user_id),
                )
                return
            if result in {"not_teacher", "class_not_assigned"}:
                await message.answer(
                    "Не удалось сохранить замечание: роль учителя или закрепление класса "
                    "изменились. Обратитесь к администратору.",
                    keyboard=await entry_keyboard(message.user_id),
                )
                return
            if result != "ok":
                await message.answer(
                    "Не удалось сохранить замечание. Обратитесь к администратору.",
                    keyboard=await entry_keyboard(message.user_id),
                )
                return
            deducted_text = (
                f"\nС уже начисленного бонуса снято {deducted} баллов."
                if deducted
                else ""
            )
            await message.answer(
                f"Замечание классу {form['class']} сохранено. Замечаний за месяц: "
                f"{penalty_count}. Максимум за дисциплину теперь {cap}/25 баллов."
                f"{deducted_text}",
                keyboard=await entry_keyboard(message.user_id),
            )
            await notify_moderators(
                f"⚠️ Учитель MAX ID {message.user_id} зафиксировал замечание классу "
                f"{form['class']} за дисциплину. Замечаний за месяц: {penalty_count}; "
                f"максимум бонуса: {cap}/25."
                + (f"\nКомментарий: {form['note']}" if form.get("note") else "")
            )
            return

    if text == "⚠️ Штрафы классу":
        if message.user_id is None or await get_user_role(message.user_id) != "teacher":
            await message.answer("Раздел доступен только пользователям с ролью учителя.")
            return
        classes = await list_teacher_assigned_classes(message.user_id)
        if not classes:
            await message.answer("За вами пока не закреплены классы. Обратитесь к администратору.")
            return
        pending_admin_forms.pop(message.user_id, None)
        pending_teacher_penalties[message.user_id] = {"step": "class"}
        class_names = class_button_names(classes)
        await message.answer(
            "Выберите свой класс, которому нужно зафиксировать замечание:",
            keyboard=options_keyboard(*class_names.keys(), "↩️ Отмена"),
        )
        return

    if message.user_id in pending_admin_forms:
        form = pending_admin_forms[message.user_id]
        value = text.strip()

        if value == "↩️ Отмена":
            pending_admin_forms.pop(message.user_id, None)
            await message.answer("Текущая форма отменена.", keyboard=admin_keyboard())
            return

        if form.get("kind") == "rating_audit_undo":
            if form["step"] == "id":
                if not value.isdigit():
                    await message.answer(
                        "Введите числовой номер записи из журнала или нажмите «↩️ Отмена».",
                        keyboard=cancel_keyboard(),
                    )
                    return
                actions = await list_recent_rating_actions(message.user_id, limit=15)
                selected = next(
                    (action for action in actions if int(action["id"]) == int(value)),
                    None,
                )
                if selected is None:
                    await message.answer(
                        "Такого номера нет в показанном журнале. Откройте журнал заново.",
                        keyboard=admin_keyboard(),
                    )
                    pending_admin_forms.pop(message.user_id, None)
                    return
                if selected["reversed_at"] is not None:
                    await message.answer("Эта запись уже была отменена.", keyboard=admin_keyboard())
                    pending_admin_forms.pop(message.user_id, None)
                    return
                if not selected["can_undo"]:
                    await message.answer(
                        "Эту запись нельзя отменить раньше более нового изменения "
                        "того же показателя. Сначала отмените новую запись.",
                        keyboard=admin_keyboard(),
                    )
                    pending_admin_forms.pop(message.user_id, None)
                    return
                form["action_id"] = value
                form["action_type"] = selected["action_type"]
                form["step"] = "confirm"
                category_labels = {
                    "punctuality": "Пунктуальность",
                    "attendance": "Посещаемость",
                    "discipline": "Дисциплина",
                    "tidiness": "Опрятность и форма",
                }
                if selected["action_type"] == "bonus":
                    before = (
                        str(selected["previous_points"])
                        if selected["had_bonus"]
                        else "начисления не было"
                    )
                    change_detail = (
                        f"Бонус изменится с {selected['new_points']} на {before} баллов."
                    )
                else:
                    change_detail = "Замечание учителя будет удалено."
                    if selected["had_bonus"]:
                        change_detail += (
                            f" Бонус по дисциплине восстановится с "
                            f"{selected['new_points']} до {selected['previous_points']} баллов."
                        )
                await message.answer(
                    f"Отменить запись №{value} для класса {selected['class_name']} "
                    f"за {category_labels[selected['category']]}?\n{change_detail}\n"
                    "После отмены рейтинг будет пересчитан.",
                    keyboard=confirmation_keyboard("↩️ Подтвердить отмену"),
                )
                return
            if value == "✏️ Изменить":
                form["step"] = "id"
                await message.answer(
                    "Введите номер записи из журнала:",
                    keyboard=cancel_keyboard(),
                )
                return
            if value != "↩️ Подтвердить отмену":
                await message.answer(
                    "Подтвердите отмену кнопкой или нажмите «✏️ Изменить».",
                    keyboard=confirmation_keyboard("↩️ Подтвердить отмену"),
                )
                return
            status, class_name, category, previous_points, new_points = await undo_rating_action(
                message.user_id,
                int(form["action_id"]),
            )
            pending_admin_forms.pop(message.user_id, None)
            if status == "ok":
                if form["action_type"] == "bonus" and previous_points is None:
                    detail = f" Начисление {new_points} баллов удалено."
                elif previous_points is not None and new_points is not None:
                    detail = f" Баллы восстановлены: {new_points} → {previous_points}."
                else:
                    detail = " Само замечание удалено; начисленный бонус не менялся."
                await message.answer(
                    f"Запись №{form['action_id']} отменена для класса {class_name} "
                    f"(показатель: {category}).{detail} Рейтинг пересчитан.",
                    keyboard=admin_keyboard(),
                )
            else:
                messages = {
                    "not_found": "Запись не найдена.",
                    "already_undone": "Эта запись уже была отменена.",
                    "class_missing": "Класс удалён, поэтому отменить запись нельзя.",
                    "newer_action_exists": (
                        "Эту запись нельзя отменить раньше более нового изменения "
                        "того же показателя. Отмените сначала новую запись."
                    ),
                    "state_changed": (
                        "Текущее состояние уже отличается от этой записи. "
                        "Ничего не изменено; обновите журнал."
                    ),
                    "not_admin": "Отменять записи может только администратор.",
                }
                await message.answer(
                    messages.get(status, "Не удалось отменить запись."),
                    keyboard=admin_keyboard(),
                )
            return

        if form.get("kind") == "deleteclass":
            if form["step"] == "class":
                classes = await list_classes_for_removal()
                class_names = {name for name, _age_group in classes}
                if value not in class_names:
                    await message.answer(
                        "Выберите класс кнопкой из списка.",
                        keyboard=options_keyboard(
                            *(name for name, _age_group in classes),
                            "↩️ Отмена",
                        ),
                    )
                    return

                usage = await get_class_usage(value)
                if usage is None:
                    await message.answer(
                        "Класс не найден. Обновите список и попробуйте снова.",
                        keyboard=context_keyboard("class"),
                    )
                    pending_admin_forms.pop(message.user_id, None)
                    return

                form["class"] = value
                usage_labels = (
                    ("учеников", usage[1]),
                    ("кодов учителей", usage[2]),
                    ("мероприятий", usage[3]),
                    ("записей на мероприятия", usage[4]),
                    ("отчётов", usage[5]),
                    ("идей", usage[6]),
                    ("начислений баллов", usage[7]),
                    ("связей учителей и заявок", usage[8]),
                    ("замечаний за дисциплину", usage[9]),
                )
                used_details = "\n".join(
                    f"• {label}: {count}"
                    for label, count in usage_labels
                    if count
                )
                if used_details:
                    form["step"] = "archive_confirm"
                    await message.answer(
                        f"Класс «{value}» нельзя удалить: он используется в системе.\n\n"
                        f"Связанные данные:\n{used_details}\n\n"
                        "Удаление может повредить историю. Архивировать класс?",
                        keyboard=options_keyboard(
                            "📦 Архивировать класс",
                            "↩️ Отмена",
                        ),
                    )
                    return

                form["step"] = "delete_confirm"
                await message.answer(
                    f"Класс «{value}» пустой и не связан с данными.\n"
                    "Удалить его окончательно?",
                    keyboard=confirmation_keyboard("🗑 Удалить класс"),
                )
                return

            if form["step"] == "archive_confirm":
                if value != "📦 Архивировать класс":
                    await message.answer(
                        "Нажмите «📦 Архивировать класс» или «↩️ Отмена».",
                        keyboard=options_keyboard(
                            "📦 Архивировать класс",
                            "↩️ Отмена",
                        ),
                    )
                    return
                archived = await archive_class(form["class"])
                pending_admin_forms.pop(message.user_id, None)
                await message.answer(
                    (
                        f"Класс «{form['class']}» архивирован. История сохранена, "
                        "новая регистрация по его коду недоступна."
                        if archived
                        else "Не удалось архивировать класс. Возможно, он уже изменён."
                    ),
                    keyboard=context_keyboard("class"),
                )
                return

            if form["step"] == "delete_confirm":
                if value == "✏️ Изменить":
                    form["step"] = "class"
                    classes = await list_classes_for_removal()
                    await message.answer(
                        "Выберите класс заново:",
                        keyboard=options_keyboard(
                            *(name for name, _age_group in classes),
                            "↩️ Отмена",
                        ),
                    )
                    return
                if value != "🗑 Удалить класс":
                    await message.answer(
                        "Нажмите «🗑 Удалить класс» или «✏️ Изменить».",
                        keyboard=confirmation_keyboard("🗑 Удалить класс"),
                    )
                    return
                deleted = await delete_class_if_empty(form["class"])
                pending_admin_forms.pop(message.user_id, None)
                await message.answer(
                    (
                        f"Класс «{form['class']}» удалён."
                        if deleted
                        else "Класс не удалён: он уже используется или был изменён."
                    ),
                    keyboard=context_keyboard("class"),
                )
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

        if kind == "teacher_invite":
            if form["step"] == "class":
                classes = await list_classes_with_codes()
                class_names = {name for name, _age_group, _code in classes}
                if value not in class_names:
                    await message.answer(
                        "Выберите класс кнопкой из списка.",
                        keyboard=options_keyboard(*sorted(class_names), "↩️ Отмена"),
                    )
                    return
                invite = await create_teacher_invite(value)
                pending_admin_forms.pop(message.user_id, None)
                if invite is None:
                    await message.answer(
                        "Класс не найден. Обновите список классов и попробуйте снова.",
                        keyboard=context_keyboard("teacher_codes"),
                    )
                    return
                code, class_name = invite
                await message.answer(
                    f"Код учителя создан для класса {class_name}:\n\n"
                    f"{code}\n\n"
                    "Передайте этот код учителю лично. Он одноразовый и не должен "
                    "публиковаться в общем чате.",
                    keyboard=context_keyboard("teacher_codes"),
                )
                return

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
                classes = await list_classes_with_codes()
                if not classes:
                    await message.answer(
                        "Сначала добавьте хотя бы один активный класс.",
                        keyboard=context_keyboard("class"),
                    )
                    pending_admin_forms.pop(message.user_id, None)
                    return
                class_choices = [
                    f"{name} · {rating_label(age_group)}"
                    for name, age_group, _code in classes
                ]
                await message.answer(
                    "Выберите класс-организатор. Мероприятие автоматически попадёт "
                    "в возрастную группу этого класса:",
                    keyboard=options_keyboard(*class_choices, "↩️ Отмена"),
                )
                return
            if form["step"] == "class":
                classes = await list_classes_with_codes()
                class_by_label = {
                    f"{name} · {rating_label(age_group)}": (name, age_group)
                    for name, age_group, _code in classes
                }
                selected_class = class_by_label.get(value)
                if selected_class is None:
                    selected_class = next(
                        ((name, group) for name, group, _code in classes if name == value),
                        None,
                    )
                if selected_class is None:
                    choices = [
                        f"{name} · {rating_label(age_group)}"
                        for name, age_group, _code in classes
                    ]
                    await message.answer(
                        "Выберите класс из списка, чтобы верно определить категорию.",
                        keyboard=options_keyboard(*choices, "↩️ Отмена"),
                    )
                    return
                form["class"], form["age_group"] = selected_class
                form["step"] = "confirm"
                await message.answer(
                    "Проверьте мероприятие:\n"
                    f"Название: {form['title']}\n"
                    f"Описание: {form['description']}\n"
                    f"Дата: {form['date']}\n"
                    f"Класс-организатор: {form['class']}\n"
                    f"Категория: {rating_label(form['age_group'])}\n\n"
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
                        "👑 Администратор",
                        "🛡 Модератор",
                        "👩‍🏫 Учитель",
                        "👤 Участник",
                        "↩️ Отмена",
                    ),
                )
                return
            roles = {
                "👑 Администратор": "admin",
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

        elif kind in {"approve_idea", "approve_report", "approve_teacher"}:
            command = {
                "approve_idea": "/approve",
                "approve_report": "/approve_report",
                "approve_teacher": "/approve_teacher",
            }[kind]
            title = {
                "approve_idea": "идею",
                "approve_report": "отчёт",
                "approve_teacher": "заявку учителя",
            }[kind]
            confirm = (
                "✅ Одобрить идею"
                if kind == "approve_idea"
                else (
                    "✅ Подтвердить одобрение заявки"
                    if kind == "approve_teacher"
                    else "✅ Подтвердить одобрение"
                )
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
                        if kind in {"approve_report", "approve_teacher"}
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
                        if kind in {"approve_report", "approve_teacher"}
                        else f"Нажмите «{confirm}» или «✏️ Изменить»."
                    ),
                    keyboard=(
                        report_confirmation_keyboard(confirm)
                        if kind in {"approve_report", "approve_teacher"}
                        else confirmation_keyboard(confirm)
                    ),
                )
                return

        elif kind in {"reject_idea", "reject_report", "reject_teacher"}:
            command = {
                "reject_idea": "/reject",
                "reject_report": "/reject_report",
                "reject_teacher": "/reject_teacher",
            }[kind]
            title = {
                "reject_idea": "идею",
                "reject_report": "отчёт",
                "reject_teacher": "заявку учителя",
            }[kind]
            confirm = (
                "❌ Отклонить идею"
                if kind == "reject_idea"
                else (
                    "❌ Подтвердить отклонение заявки"
                    if kind == "reject_teacher"
                    else "❌ Подтвердить отклонение"
                )
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
                        if kind in {"reject_report", "reject_teacher"}
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
                        if kind in {"reject_report", "reject_teacher"}
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

        elif kind == "bonus":
            category_by_button = {
                button: (key, label, limit)
                for button, (key, label, limit) in BONUS_CATEGORY_BUTTONS.items()
            }
            if form["step"] == "group":
                age_group = AGE_GROUP_BUTTONS.get(value)
                if age_group is None:
                    await message.answer(
                        "Выберите возрастную группу кнопкой.",
                        keyboard=options_keyboard(*AGE_GROUP_BUTTONS.keys(), "↩️ Отмена"),
                    )
                    return
                form["age_group"] = age_group
                form["step"] = "class"
                available_classes = [
                    name
                    for name, group in await list_classes_for_removal()
                    if group == age_group
                ]
                if not available_classes:
                    form["step"] = "group"
                    await message.answer(
                        f"В группе «{AGE_GROUP_LABELS[age_group]}» пока нет активных классов. "
                        "Выберите другую группу:",
                        keyboard=options_keyboard(*AGE_GROUP_BUTTONS.keys(), "↩️ Отмена"),
                    )
                    return
                await message.answer(
                    f"Выберите класс из группы «{AGE_GROUP_LABELS[age_group]}»:",
                    keyboard=options_keyboard(
                        *available_classes,
                        "↩️ К выбору группы",
                        "↩️ Отмена",
                    ),
                )
                return

            if form["step"] == "class":
                if value == "↩️ К выбору группы":
                    form["step"] = "group"
                    await message.answer(
                        "Выберите возрастную группу:",
                        keyboard=options_keyboard(*AGE_GROUP_BUTTONS.keys(), "↩️ Отмена"),
                    )
                    return
                available_classes = {
                    name
                    for name, group in await list_classes_for_removal()
                    if group == form["age_group"]
                }
                selected_class = value if value in available_classes else None
                if selected_class is None:
                    await message.answer(
                        "Выберите класс кнопкой из этой возрастной группы.",
                        keyboard=options_keyboard(
                            *sorted(available_classes),
                            "↩️ К выбору группы",
                            "↩️ Отмена",
                        ),
                    )
                    return
                form["class"] = selected_class
                form["step"] = "category"
                await message.answer(
                    f"Выбран класс {selected_class}. Выберите показатель для бонуса:",
                    keyboard=options_keyboard(*BONUS_CATEGORY_BUTTONS.keys(), "↩️ Отмена"),
                )
                return

            if form["step"] == "category":
                selected = category_by_button.get(value)
                if selected is None:
                    await message.answer(
                        "Выберите показатель кнопкой.",
                        keyboard=options_keyboard(*BONUS_CATEGORY_BUTTONS.keys(), "↩️ Отмена"),
                    )
                    return
                category, label, _base_limit = selected
                status, penalty_count, cap, current_points = await get_monthly_bonus_status(
                    form["class"], category
                )
                if status == "period_not_open":
                    pending_admin_forms.pop(message.user_id, None)
                    await message.answer(
                        "Нельзя начислить бонус: текущий месяц ещё не открыт. "
                        "Сначала задайте ценность месяца.",
                        keyboard=context_keyboard("bonus"),
                    )
                    return
                if status != "ok":
                    pending_admin_forms.pop(message.user_id, None)
                    await message.answer(
                        "Не удалось проверить класс. Начните начисление заново.",
                        keyboard=context_keyboard("bonus"),
                    )
                    return
                form.update(
                    {
                        "category": category,
                        "category_label": label,
                        "cap": str(cap),
                        "penalty_count": str(penalty_count),
                        "current_points": str(current_points),
                        "step": "points",
                    }
                )
                penalty_hint = (
                    f"Учителя зафиксировали {penalty_count} замечаний; "
                    if category == "discipline"
                    else ""
                )
                await message.answer(
                    f"{label} для класса {form['class']}: {penalty_hint}"
                    f"максимум {cap} баллов. Сейчас начислено {current_points}.\n"
                    f"Введите итоговое количество баллов от 0 до {cap}. "
                    "Новое значение заменит прежнее.",
                    keyboard=cancel_keyboard(),
                )
                return

            if form["step"] == "points":
                cap = int(form["cap"])
                if not value.isdigit() or not 0 <= int(value) <= cap:
                    await message.answer(
                        f"Введите целое число от 0 до {cap}, учитывая замечания учителей.",
                        keyboard=cancel_keyboard(),
                    )
                    return
                form["points"] = value
                form["step"] = "confirm"
                await message.answer(
                    f"Установить бонус «{form['category_label']}» классу {form['class']} "
                    f"за текущий месяц равным {value} баллам?\n"
                    f"Ограничение: {form['cap']}; сейчас: {form['current_points']}."
                    + (
                        f" Замечаний учителей: {form['penalty_count']}."
                        if form["category"] == "discipline"
                        else ""
                    ),
                    keyboard=confirmation_keyboard("✅ Сохранить бонус"),
                )
                return

            if form["step"] == "confirm":
                if value == "✏️ Изменить":
                    form["step"] = "points"
                    await message.answer(
                        f"Введите итоговое количество баллов от 0 до {form['cap']}.",
                        keyboard=cancel_keyboard(),
                    )
                    return
                if value != "✅ Сохранить бонус":
                    await message.answer(
                        "Нажмите «✅ Сохранить бонус» или «✏️ Изменить».",
                        keyboard=confirmation_keyboard("✅ Сохранить бонус"),
                    )
                    return
                status, penalty_count, cap = await award_monthly_bonus(
                    message.user_id,
                    form["class"],
                    form["category"],
                    int(form["points"]),
                )
                pending_admin_forms.pop(message.user_id, None)
                if status == "exceeds_limit":
                    await message.answer(
                        f"Бонус не сохранён: с учётом {penalty_count} замечаний "
                        f"максимум за дисциплину составляет {cap} баллов. "
                        "Начните начисление заново.",
                        keyboard=context_keyboard("bonus"),
                    )
                    return
                if status == "period_not_open":
                    await message.answer(
                        "Бонус не сохранён: текущий месяц закрыт или ещё не открыт.",
                        keyboard=context_keyboard("bonus"),
                    )
                    return
                if status != "ok":
                    await message.answer(
                        "Не удалось сохранить бонус. Проверьте права администратора "
                        "и выбранный класс.",
                        keyboard=context_keyboard("bonus"),
                    )
                    return
                await message.answer(
                    f"Бонус «{form['category_label']}» класса {form['class']} "
                    f"сохранён: {form['points']} баллов за текущий месяц. "
                    "Месячный и годовой рейтинги пересчитаны.",
                    keyboard=context_keyboard("bonus"),
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
        for event_id, title, description, event_date, class_name, age_group in report_events:
            lines.append(
                f"\n№{event_id} · {event_date} · {title}\n"
                f"{description}\nОрганизатор: класс {class_name} "
                f"({AGE_GROUP_LABELS.get(age_group, age_group)})"
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
            if result == "wrong_group":
                await message.answer("Это мероприятие относится к другой возрастной группе.")
                return
            if result == "period_not_open":
                await message.answer(
                    "Отчёты за этот месяц ещё не принимаются: администратор ещё не ввёл "
                    "ценность месяца."
                )
                return
            if result == "period_closed":
                await message.answer(
                    "Срок сдачи отчётов за этот месяц завершён. "
                    "Новые отчёты больше не принимаются."
                )
                return
            if result != "ok":
                await message.answer(
                    "Мероприятие ещё не состоялось." if result == "event_not_finished"
                    else "Мероприятие не найдено. Откройте афишу заново."
                )
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

    if text == "👥 Пользователи":
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        pending_admin_actions.pop(message.user_id, None)
        pending_users_role.add(message.user_id)
        await message.answer(
            "👥 Выберите, каких пользователей показать:",
            keyboard=users_role_keyboard(),
        )
        return

    if text in {
        "📚 Классы",
        "🎪 Мероприятия",
        "💡 Очередь идей",
        "📋 Очередь отчётов",
        "👩‍🏫 Заявки учителей",
        "🔑 Коды учителей",
    }:
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        pending_admin_actions.pop(message.user_id, None)
        admin_events_view = text == "🎪 Мероприятия"
        text = {
            "📚 Классы": "/classes",
            "🎪 Мероприятия": "/events",
            "💡 Очередь идей": "/ideas",
            "📋 Очередь отчётов": "/reports",
            "👩‍🏫 Заявки учителей": "/teacher_applications",
            "🔑 Коды учителей": "/teacher_codes",
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
        "🗑 Удалить класс": (
            {"kind": "deleteclass", "step": "class"},
            "Выберите класс, который нужно удалить:",
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
        "✅ Одобрить заявку": (
            {"kind": "approve_teacher", "step": "id"},
            "Введите номер заявки учителя.",
        ),
        "❌ Отклонить заявку": (
            {"kind": "reject_teacher", "step": "id"},
            "Введите номер заявки учителя.",
        ),
        "🔑 Создать код учителя": (
            {"kind": "teacher_invite", "step": "class"},
            "Выберите класс, для которого создать персональный код учителя:",
        ),
        "🏅 Призовые баллы": (
            {"kind": "award", "step": "class"},
            "Введите название класса.",
        ),
        "🎁 Дополнительные баллы": (
            {"kind": "bonus", "step": "group"},
            "Выберите возрастную группу для начисления дополнительных баллов:",
        ),
        "⚙️ Лимиты": (
            {"kind": "setlimit", "step": "type"},
            "Выберите тип лимита:",
        ),
    }

    if text == "🧾 Журнал начислений":
        if not await is_admin_user(message.user_id):
            await message.answer("Раздел доступен только администраторам.")
            return
        actions = await list_recent_rating_actions(message.user_id, limit=15)
        if not actions:
            pending_admin_actions.pop(message.user_id, None)
            await message.answer(
                "Журнал пока пуст: дополнительных начислений и замечаний ещё не было.",
                keyboard=admin_keyboard(),
            )
            return

        category_labels = {
            key: label for key, label, _limit in BONUS_CATEGORY_BUTTONS.values()
        }
        lines = ["🧾 Последние начисления и замечания:"]
        for action in actions:
            status_label = (
                "↩️ уже отменена"
                if action["reversed_at"] is not None
                else ("✅ можно отменить" if action["can_undo"] else "🔒 есть более новое изменение")
            )
            period_label = action["period_month"].strftime("%Y-%m")
            if action["action_type"] == "bonus":
                old_value = (
                    str(action["previous_points"])
                    if action["had_bonus"]
                    else "не было"
                )
                description = (
                    f"Бонус «{category_labels[action['category']]}»: "
                    f"{old_value} → {action['new_points']} баллов"
                )
            else:
                description = "Замечание учителя"
                if action["had_bonus"]:
                    description += (
                        f"; дисциплина: {action['previous_points']} → "
                        f"{action['new_points']} баллов"
                    )
                if action["note"]:
                    note = action["note"]
                    if len(note) > 80:
                        note = note[:77] + "…"
                    description += f"; {note}"
            lines.append(
                f"\n№{action['id']} · {period_label} · {action['class_name']}\n"
                f"{description}\n"
                f"Автор ID {action['actor_max_user_id']} · {status_label}"
            )

        pending_admin_actions.pop(message.user_id, None)
        pending_admin_forms[message.user_id] = {
            "kind": "rating_audit_undo",
            "step": "id",
        }
        await message.answer(
            "\n".join(lines)
            + "\n\nВведите номер записи, которую нужно отменить. "
            "Отменить можно только последнее изменение показателя для этого класса и месяца.",
            keyboard=cancel_keyboard(),
        )
        return

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
        elif text in {"✅ Одобрить заявку", "❌ Отклонить заявку"}:
            keyboard = cancel_keyboard()
        elif text == "🔑 Создать код учителя":
            classes = await list_classes_with_codes()
            keyboard = options_keyboard(
                *(name for name, _age_group, _code in classes),
                "↩️ Отмена",
            )
        elif text == "🗑 Удалить класс":
            classes = await list_classes_for_removal()
            if not classes:
                pending_admin_forms.pop(message.user_id, None)
                await message.answer(
                    "Активных классов для удаления нет.",
                    keyboard=context_keyboard("class"),
                )
                return
            keyboard = options_keyboard(
                *(name for name, _age_group in classes),
                "↩️ Отмена",
            )
        elif text == "🎁 Дополнительные баллы":
            classes = await list_classes_for_removal()
            if not classes:
                pending_admin_forms.pop(message.user_id, None)
                await message.answer(
                    "Активных классов для начисления бонусов нет.",
                    keyboard=context_keyboard("bonus"),
                )
                return
            keyboard = options_keyboard(*AGE_GROUP_BUTTONS.keys(), "↩️ Отмена")
        await message.answer(
            prompt + "\n\nДля отмены нажмите «↩️ Отмена».",
            keyboard=keyboard,
        )
        return

    if text == "🎯 Ценность месяца":
        value = await get_current_month_value()
        await message.answer(
            (
                f"🎯 Ценность текущего месяца: {value}"
                if value
                else "🎯 Ценность текущего месяца ещё не введена администратором."
            ),
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
        pending_users_role.add(message.user_id)
        await message.answer(
            "👥 Выберите, каких пользователей показать:",
            keyboard=users_role_keyboard(),
        )
        return

    if text == "/teacher_codes":
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        invites = await list_teacher_invites()
        if not invites:
            await message.answer(
                "Кодов учителей пока нет. Создайте персональный код для нужного класса.",
                keyboard=context_keyboard("teacher_codes"),
            )
            return
        status_labels = {
            "active": "активен",
            "used": "использован",
            "revoked": "отозван",
        }
        lines = ["🔑 Коды учителей:"]
        lines.extend(
            f"{code} — класс {class_name} ({academic_year}) — "
            f"{status_labels.get(status, status)}"
            for code, class_name, academic_year, status in invites
        )
        await message.answer("\n".join(lines), keyboard=context_keyboard("teacher_codes"))
        return

    if text == "/teacher_applications":
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        applications = await list_pending_teacher_applications()
        if not applications:
            await message.answer(
                "Заявок учителей на проверке нет.",
                keyboard=teacher_application_actions_keyboard(),
            )
            return
        lines = ["👩‍🏫 Заявки учителей:"]
        for application_id, max_user_id, full_name, subject, class_name in applications:
            lines.append(
                f"\nЗаявка №{application_id}\n"
                f"ФИО: {full_name}\n"
                f"Предмет: {subject}\n"
                f"Преподаёт в классах: {class_name}\n"
                f"MAX ID: {max_user_id}"
            )
        await message.answer("\n".join(lines), keyboard=teacher_application_actions_keyboard())
        return

    if text == "/approve_teacher" or text.startswith("/approve_teacher "):
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Формат: /approve_teacher Номер заявки")
            return
        changed, status, applicant_id, class_name = await moderate_teacher_application(
            int(parts[1]), message.user_id, "approved"
        )
        if not changed:
            await message.answer("Заявка не найдена или уже обработана.")
            return
        await message.answer(
            f"Заявка одобрена. Учитель привязан к классам: {class_name}.",
            keyboard=teacher_application_actions_keyboard(),
        )
        if applicant_id is not None:
            await notify_user(
                applicant_id,
                f"✅ Заявка на регистрацию учителя одобрена.\n"
                f"Вы привязаны к классам: {class_name}.",
            )
        return

    if text == "/reject_teacher" or text.startswith("/reject_teacher "):
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        parts = text.split(maxsplit=2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].strip():
            await message.answer("Формат: /reject_teacher Номер Причина")
            return
        changed, status, applicant_id, class_name = await moderate_teacher_application(
            int(parts[1]), message.user_id, "rejected", parts[2].strip()
        )
        if not changed:
            await message.answer("Заявка не найдена или уже обработана.")
            return
        await message.answer(
            "Заявка отклонена. Причина сохранена.",
            keyboard=teacher_application_actions_keyboard(),
        )
        if applicant_id is not None:
            await notify_user(
                applicant_id,
                "❌ Заявка на регистрацию учителя отклонена.\n"
                f"Причина: {parts[2].strip()}\n"
                "Можно подать новую заявку.",
            )
        return

    if text == "/setrole" or text.startswith("/setrole "):
        if not await is_admin_user(message.user_id):
            await message.answer("У вас нет прав администратора.")
            return
        parts = text.split()
        allowed_roles = {"student", "teacher", "moderator", "admin"}
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in allowed_roles:
            await message.answer(
                "Формат: /setrole ID admin|moderator|teacher|student\n"
                "Пример: /setrole 123456 admin"
            )
            return
        target_id = int(parts[1])
        if not await can_change_admin_role(target_id, parts[2]):
            await message.answer(
                "Нельзя снять права: должен остаться хотя бы один администратор."
            )
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
        if not 1 <= len(value) <= 200:
            await message.answer("Формат: /setvalue Текст ценности месяца")
            return
        await ensure_user_role(message.user_id, "admin")
        status, period_month = await set_monthly_value(value)
        if status in {"holidays", "invalid"}:
            await message.answer(
                "Учебные месяцы — с сентября по май. Летние месяцы не открываются."
                if status == "holidays" else "Введите ценность длиной от 1 до 200 символов."
            )
            return
        if status == "closed":
            await message.answer(
                "Текущий календарный месяц уже закрыт. Изменить его ценность нельзя.",
                keyboard=context_keyboard("value"),
            )
            return
        await message.answer(
            f"Ценность месяца {period_month.strftime('%m.%Y')} установлена: {value}\n"
            "Отчёты за этот месяц будут учитываться в отдельном рейтинге.",
            keyboard=context_keyboard("value"),
        )
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
            await message.answer("Проверьте название класса и откройте текущий месяц, введя его ценность.")
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
                    "Категория определяется по возрастной группе указанного класса.\n"
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
        active_classes = await list_classes_for_removal()
        class_age_group = next(
            (age_group for class_name, age_group in active_classes if class_name == author_class_name),
            None,
        )
        category_text = (
            f" Категория: {rating_label(class_age_group)}."
            if class_age_group
            else ""
        )
        await message.answer(
            f"Мероприятие №{event_id} добавлено в афишу.{category_text}\n"
            "Пользователи увидят его через кнопку «🎪 Афиша».",
            keyboard=context_keyboard("event") if is_admin else main_keyboard(),
        )
        return

    if text == "🤝 Принять участие":
        events = await events_visible_to_user(message.user_id)
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
        for event_id, title, description, event_date, class_name, age_group in events:
            lines.append(
                f"\n№{event_id} · {event_date} · {title}\n"
                f"{description}\nОрганизатор: класс {class_name} "
                f"({AGE_GROUP_LABELS.get(age_group, age_group)})"
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
        events = await events_visible_to_user(message.user_id)
        is_admin = await is_admin_user(message.user_id)
        if not events:
            await message.answer(
                "🎪 Афиша пока пуста. Администратор может добавить мероприятие командой /addevent.",
                keyboard=context_keyboard("event") if admin_events_view else event_entry_keyboard(is_admin),
            )
            return
        lines = ["🎪 Афиша мероприятий:"]
        for event_id, title, description, event_date, class_name, age_group in events:
            lines.append(
                f"\n№{event_id} · {event_date} · {title}\n"
                f"{description}\n"
                f"Организатор: класс {class_name} "
                f"({AGE_GROUP_LABELS.get(age_group, age_group)})\n"
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
            "wrong_group": "Это мероприятие относится к другой возрастной группе.",
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
            "wrong_group": "Это мероприятие относится к другой возрастной группе.",
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
            "wrong_group": "Это мероприятие относится к другой возрастной группе.",
            "not_found": "Мероприятие не найдено.",
            "event_not_finished": "Мероприятие ещё не состоялось.",
            "period_not_open": "Месяц ещё не открыт: администратор должен ввести его ценность.",
            "period_closed": "Срок сдачи отчётов за этот месяц завершён (5-е число включительно).",
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
        for idea_id, title, description, target_group, class_name, age_group in ideas:
            lines.append(
                f"\n№{idea_id} · группа {AGE_GROUP_LABELS.get(age_group, age_group)} "
                f"· класс {class_name}\n"
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
        for (
            report_id,
            event_id,
            title,
            class_name,
            class_age_group,
            event_age_group,
            comment,
            media_token,
        ) in reports:
            lines.append(
                f"\nОтчёт №{report_id}, мероприятие №{event_id}: {title}\n"
                f"Класс: {class_name} ({AGE_GROUP_LABELS.get(class_age_group, class_age_group)})\n"
                f"Группа мероприятия: {AGE_GROUP_LABELS.get(event_age_group, event_age_group)}\n"
                f"{comment}"
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
            if status == "wrong_group":
                await message.answer(
                    "Отчёт нельзя одобрить: класс участника и группа мероприятия не совпадают. "
                    "Такой отчёт можно отклонить с пояснением."
                )
                return
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

    selected_age_group = next(
        (group for group, label in AGE_GROUP_LABELS.items() if text == label),
        None,
    )
    if selected_age_group is not None and message.user_id is not None:
        rating_state = pending_rating_context.get(message.user_id)
        if rating_state and rating_state.get("step") == "age_group":
            allowed_groups = await available_rating_groups(message.user_id)
            if selected_age_group not in allowed_groups:
                await begin_rating_selection(message)
                return
            requested_month = rating_state.get("month")
            rating_state = {"step": "period", "age_group": selected_age_group}
            pending_rating_context[message.user_id] = rating_state
            if requested_month:
                await show_archived_month_rating(
                    message,
                    date.fromisoformat(requested_month),
                    selected_age_group,
                )
                return
            await message.answer(
                f"Выбрана группа {rating_label(selected_age_group)}. "
                "Выберите период рейтинга:",
                keyboard=rating_keyboard(await is_admin_user(message.user_id)),
            )
            return

    if text in {"📊 Рейтинг текущего месяца", "🏅 Рейтинг учебного года", "📅 История рейтингов"}:
        if message.user_id is None:
            await message.answer("Не удалось определить пользователя MAX.")
            return
        state = pending_rating_context.get(message.user_id)
        if state is None or not state.get("age_group"):
            await begin_rating_selection(message)
            return
        age_group = state["age_group"]
        if age_group not in await available_rating_groups(message.user_id):
            await begin_rating_selection(message)
            return
        if text == "📊 Рейтинг текущего месяца":
            await show_current_month_rating(message, age_group)
        elif text == "🏅 Рейтинг учебного года":
            await show_academic_year_rating(message, age_group)
        else:
            await show_rating_history(message, age_group)
        return

    if text.startswith("📊 Месяц ") or text.startswith("/rating "):
        if message.user_id is None:
            await message.answer("Не удалось определить пользователя MAX.")
            return
        raw_month = text.removeprefix("📊 Месяц ").removeprefix("/rating ").strip()
        try:
            period_month = date.fromisoformat(raw_month + "-01")
        except ValueError:
            await message.answer("Укажите месяц в формате /rating 2026-11.")
            return
        state = pending_rating_context.get(message.user_id)
        if state is None or not state.get("age_group"):
            await begin_rating_selection(message)
            if message.user_id in pending_rating_context:
                pending_rating_context[message.user_id]["month"] = period_month.isoformat()
            return
        if state.get("age_group") not in await available_rating_groups(message.user_id):
            await begin_rating_selection(message)
            return
        await show_archived_month_rating(message, period_month, state["age_group"])
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
            f"Баллы за текущий месяц: {points}",
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
        user_class = await get_user_class(message.user_id)
        pending_ideas[message.user_id] = {"step": "title", "age_group": user_class[1]}
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


@asynccontextmanager
async def rating_maintenance():
    # Catch up after downtime before accepting updates, then check every minute.
    await close_expired_months()
    await send_deadline_reminder_if_due()

    async def check_deadlines():
        while True:
            await asyncio.sleep(60)
            try:
                await close_expired_months()
                await send_deadline_reminder_if_due()
            except Exception as error:
                logging.error(
                    "Не удалось проверить закрытие месяца или напоминание (%s), повтор через минуту",
                    type(error).__name__,
                )

    task = asyncio.create_task(check_deadlines())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def run_polling() -> None:
    async with rating_maintenance():
        await dispatcher.start_polling(bot, clear_subscriptions=True)


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
        security=CompatibleStaticSecretToken(webhook_secret),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.on_startup(app)
        await engine.set_webhook(update_types=["message_created", "bot_started"])
        print(f"Webhook подключён: {public_url}")
        try:
            async with rating_maintenance():
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
        asyncio.run(run_polling())

