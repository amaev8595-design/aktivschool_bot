import csv
import json
import os
import secrets
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg


MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def moscow_today() -> date:
    return datetime.now(MOSCOW_TZ).date()


def month_start(value: date) -> date:
    return value.replace(day=1)


def next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def report_deadline(period_month: date) -> date:
    """Последний день приёма отчётов за месяц — 5-е число следующего месяца."""
    return next_month(period_month).replace(day=5)


def academic_year_start(value: date) -> date:
    """Учебный год начинается в сентябре и состоит из периодов сентябрь–май."""
    return date(value.year if value.month >= 9 else value.year - 1, 9, 1)


def academic_year_label(value: date) -> str:
    start_year = academic_year_start(value).year
    return f"{start_year}/{start_year + 1}"


def is_school_month(value: date) -> bool:
    return value.month not in (6, 7, 8)


async def lock_ratings(connection: asyncpg.Connection) -> None:
    # All point writes and month closures use the same transaction lock.
    await connection.execute("SELECT pg_advisory_xact_lock(62149023)")


def created_month(value: datetime) -> date:
    if value.tzinfo is not None:
        value = value.astimezone(MOSCOW_TZ)
    return month_start(value.date())


def get_admin_ids() -> set[int]:
    admin_ids: set[int] = set()
    for value in os.getenv("ADMIN_IDS", "").split(","):
        value = value.strip()
        if value.isdigit():
            admin_ids.add(int(value))
    return admin_ids


def get_moderator_ids() -> set[int]:
    moderator_ids: set[int] = set()
    for value in os.getenv("MODERATOR_IDS", "").split(","):
        value = value.strip()
        if value.isdigit():
            moderator_ids.add(int(value))
    return moderator_ids


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Переменная DATABASE_URL не заполнена в файле .env")
    return database_url


async def seed_classes(connection: asyncpg.Connection) -> None:
    path = Path(__file__).parent / "data" / "classes.csv"
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            await connection.execute(
                """
                INSERT INTO classes (name, age_group, invite_code)
                VALUES ($1, $2, $3)
                ON CONFLICT (name) DO NOTHING
                """,
                row["name"].strip(),
                row["age_group"].strip(),
                row["invite_code"].strip().upper(),
            )


async def initialize_database() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(get_database_url(), min_size=1, max_size=3)
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

    async with pool.acquire() as connection:
        await connection.execute(schema)
        await seed_classes(connection)

    return pool


async def get_user_class(max_user_id: int) -> tuple[str, str] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            """
            SELECT classes.name, classes.age_group
            FROM users
            JOIN classes ON classes.id = users.class_id
            WHERE users.max_user_id = $1
            """,
            max_user_id,
        )
        if row is None:
            return None
        return row["name"], row["age_group"]
    finally:
        await connection.close()


async def list_teacher_assigned_classes(
    teacher_max_user_id: int,
) -> list[tuple[str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT classes.name, classes.age_group
            FROM teacher_class_assignments
            JOIN classes ON classes.id = teacher_class_assignments.class_id
            WHERE teacher_class_assignments.teacher_max_user_id = $1
              AND classes.archived = FALSE
            ORDER BY classes.age_group, classes.name
            """,
            teacher_max_user_id,
        )
        return [(row["name"], row["age_group"]) for row in rows]
    finally:
        await connection.close()


async def bind_user_to_class(max_user_id: int, invite_code: str) -> tuple[str, str] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            """
            SELECT id, name, age_group
            FROM classes
            WHERE invite_code = $1 AND archived = FALSE
            """,
            invite_code.strip().upper(),
        )
        if row is None:
            return None

        await connection.execute(
            """
            INSERT INTO users (max_user_id, class_id)
            VALUES ($1, $2)
            ON CONFLICT (max_user_id)
            DO UPDATE SET class_id = EXCLUDED.class_id
            """,
            max_user_id,
            row["id"],
        )
        return row["name"], row["age_group"]
    finally:
        await connection.close()


async def ensure_user_role(max_user_id: int, role: str) -> None:
    connection = await asyncpg.connect(get_database_url())
    try:
        await connection.execute(
            """
            INSERT INTO users (max_user_id, role)
            VALUES ($1, $2)
            ON CONFLICT (max_user_id)
            DO UPDATE SET role = EXCLUDED.role
            """,
            max_user_id,
            role,
        )
    finally:
        await connection.close()


async def get_user_role(max_user_id: int) -> str | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            "SELECT role FROM users WHERE max_user_id = $1",
            max_user_id,
        )
        return row["role"] if row is not None else None
    finally:
        await connection.close()


async def set_user_role(max_user_id: int, role: str) -> None:
    connection = await asyncpg.connect(get_database_url())
    try:
        await connection.execute(
            """
            INSERT INTO users (max_user_id, role)
            VALUES ($1, $2)
            ON CONFLICT (max_user_id)
            DO UPDATE SET role = EXCLUDED.role
            """,
            max_user_id,
            role,
        )
        if role == "teacher":
            await connection.execute(
                """
                INSERT INTO teacher_class_assignments (teacher_max_user_id, class_id)
                SELECT max_user_id, class_id
                FROM users
                WHERE max_user_id = $1 AND class_id IS NOT NULL
                ON CONFLICT DO NOTHING
                """,
                max_user_id,
            )
        else:
            await connection.execute(
                "DELETE FROM teacher_class_assignments WHERE teacher_max_user_id = $1",
                max_user_id,
            )
    finally:
        await connection.close()


async def list_users() -> list[tuple[int, str, str | None]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT users.max_user_id, users.role, classes.name AS class_name
            FROM users
            LEFT JOIN classes ON classes.id = users.class_id
            ORDER BY users.created_at, users.max_user_id
            """
        )
        return [
            (row["max_user_id"], row["role"], row["class_name"])
            for row in rows
        ]
    finally:
        await connection.close()


async def list_users_with_details() -> list[tuple[int, str, str | None, str | None, str | None]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT users.max_user_id,
                   users.role,
                   COALESCE(
                       (
                           SELECT string_agg(assigned_classes.name, ', '
                                             ORDER BY assigned_classes.name)
                           FROM teacher_class_assignments
                           JOIN classes AS assigned_classes
                             ON assigned_classes.id = teacher_class_assignments.class_id
                           WHERE teacher_class_assignments.teacher_max_user_id = users.max_user_id
                             AND assigned_classes.archived = FALSE
                       ),
                       classes.name
                   ) AS class_name,
                   teacher_applications.full_name,
                   teacher_applications.subject
            FROM users
            LEFT JOIN classes ON classes.id = users.class_id
            LEFT JOIN teacher_applications
              ON teacher_applications.max_user_id = users.max_user_id
             AND teacher_applications.status = 'approved'
            ORDER BY users.created_at, users.max_user_id
            """
        )
        return [
            (
                int(row["max_user_id"]),
                row["role"],
                row["class_name"],
                row["full_name"],
                row["subject"],
            )
            for row in rows
        ]
    finally:
        await connection.close()


async def list_user_ids_by_roles(roles: set[str]) -> list[int]:
    if not roles:
        return []
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            "SELECT max_user_id FROM users WHERE role = ANY($1::text[])",
            list(roles),
        )
        return [row["max_user_id"] for row in rows]
    finally:
        await connection.close()


async def create_teacher_invite(class_name: str) -> tuple[str, str] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        class_row = await connection.fetchrow(
            "SELECT id, name FROM classes WHERE name = $1 AND archived = FALSE",
            class_name.strip(),
        )
        if class_row is None:
            return None

        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        for _ in range(10):
            code = "AKTIV-T-" + "".join(secrets.choice(alphabet) for _ in range(6))
            try:
                await connection.execute(
                    """
                    INSERT INTO teacher_invites (code, class_id)
                    VALUES ($1, $2)
                    """,
                    code,
                    class_row["id"],
                )
                return code, class_row["name"]
            except asyncpg.UniqueViolationError:
                continue
        raise RuntimeError("Не удалось сгенерировать уникальный код учителя")
    finally:
        await connection.close()


async def list_teacher_invites() -> list[tuple[str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT teacher_invites.code, classes.name AS class_name,
                   teacher_invites.status
            FROM teacher_invites
            JOIN classes ON classes.id = teacher_invites.class_id
            ORDER BY teacher_invites.created_at DESC, teacher_invites.id DESC
            """
        )
        return [
            (row["code"], row["class_name"], row["status"])
            for row in rows
        ]
    finally:
        await connection.close()


async def get_teacher_invite(code: str) -> tuple[int, int, str] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            """
            SELECT teacher_invites.id, teacher_invites.class_id,
                   classes.name AS class_name
            FROM teacher_invites
            JOIN classes ON classes.id = teacher_invites.class_id
            WHERE teacher_invites.code = $1
              AND teacher_invites.status = 'active'
              AND classes.archived = FALSE
            """,
            code.strip().upper(),
        )
        if row is None:
            return None
        return int(row["id"]), int(row["class_id"]), row["class_name"]
    finally:
        await connection.close()


async def get_teacher_application(
    max_user_id: int,
) -> tuple[int, str, str, str, str | None] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            """
            SELECT teacher_applications.id, teacher_applications.status,
                   teacher_applications.full_name,
                   teacher_applications.subject,
                   teacher_applications.moderator_comment
            FROM teacher_applications
            WHERE teacher_applications.max_user_id = $1
            """,
            max_user_id,
        )
        if row is None:
            return None
        return (
            int(row["id"]),
            row["status"],
            row["full_name"],
            row["subject"],
            row["moderator_comment"],
        )
    finally:
        await connection.close()


async def create_teacher_application(
    max_user_id: int,
    full_name: str,
    subject: str,
    invite_code: str,
    class_names: list[str] | None = None,
) -> tuple[str, int | None, str | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            invite = await connection.fetchrow(
                """
                SELECT teacher_invites.id, teacher_invites.class_id,
                       classes.name AS class_name
                FROM teacher_invites
                JOIN classes ON classes.id = teacher_invites.class_id
                WHERE teacher_invites.code = $1
                  AND teacher_invites.status = 'active'
              AND classes.archived = FALSE
                FOR UPDATE
                """,
                invite_code.strip().upper(),
            )
            if invite is None:
                return "invalid_code", None, None

            selected_names = set(class_names or [invite["class_name"]])
            selected_names.add(invite["class_name"])
            selected_classes = await connection.fetch(
                """
                SELECT id, name
                FROM classes
                WHERE name = ANY($1::text[]) AND archived = FALSE
                ORDER BY age_group, name
                """,
                list(selected_names),
            )
            if (
                len(selected_classes) != len(selected_names)
                or int(invite["class_id"])
                not in {int(row["id"]) for row in selected_classes}
            ):
                return "invalid_classes", None, invite["class_name"]

            existing = await connection.fetchrow(
                """
                SELECT id, status
                FROM teacher_applications
                WHERE max_user_id = $1
                FOR UPDATE
                """,
                max_user_id,
            )
            if existing is not None and existing["status"] == "pending":
                return "already_pending", int(existing["id"]), invite["class_name"]
            if existing is not None and existing["status"] == "approved":
                return "already_approved", int(existing["id"]), invite["class_name"]

            invite_in_use = await connection.fetchrow(
                """
                SELECT id
                FROM teacher_applications
                WHERE invite_id = $1
                  AND status = 'pending'
                  AND max_user_id <> $2
                LIMIT 1
                """,
                invite["id"],
                max_user_id,
            )
            if invite_in_use is not None:
                return "invite_in_use", None, invite["class_name"]

            if existing is None:
                application = await connection.fetchrow(
                    """
                    INSERT INTO teacher_applications (
                        max_user_id, full_name, subject, invite_id
                    )
                    VALUES ($1, $2, $3, $4)
                    RETURNING id
                    """,
                    max_user_id,
                    full_name,
                    subject,
                    invite["id"],
                )
            else:
                application = await connection.fetchrow(
                    """
                    UPDATE teacher_applications
                    SET full_name = $2,
                        subject = $3,
                        invite_id = $4,
                        status = 'pending',
                        moderator_comment = NULL,
                        updated_at = NOW()
                    WHERE max_user_id = $1
                    RETURNING id
                    """,
                    max_user_id,
                    full_name,
                    subject,
                    invite["id"],
                )
            await connection.execute(
                "DELETE FROM teacher_application_classes WHERE application_id = $1",
                application["id"],
            )
            await connection.executemany(
                """
                INSERT INTO teacher_application_classes (application_id, class_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                [(application["id"], row["id"]) for row in selected_classes],
            )
            return "ok", int(application["id"]), invite["class_name"]
    finally:
        await connection.close()


async def list_pending_teacher_applications() -> list[tuple[int, int, str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT teacher_applications.id, teacher_applications.max_user_id,
                   teacher_applications.full_name, teacher_applications.subject,
                   COALESCE(
                       string_agg(DISTINCT assigned_classes.name, ', '
                                  ORDER BY assigned_classes.name),
                       classes.name
                   ) AS class_name
            FROM teacher_applications
            JOIN teacher_invites ON teacher_invites.id = teacher_applications.invite_id
            JOIN classes ON classes.id = teacher_invites.class_id
            LEFT JOIN teacher_application_classes
                ON teacher_application_classes.application_id = teacher_applications.id
            LEFT JOIN classes AS assigned_classes
                ON assigned_classes.id = teacher_application_classes.class_id
            WHERE teacher_applications.status = 'pending'
            GROUP BY teacher_applications.id, teacher_applications.max_user_id,
                     teacher_applications.full_name, teacher_applications.subject,
                     classes.name
            ORDER BY teacher_applications.created_at, teacher_applications.id
            """
        )
        return [
            (
                int(row["id"]),
                int(row["max_user_id"]),
                row["full_name"],
                row["subject"],
                row["class_name"],
            )
            for row in rows
        ]
    finally:
        await connection.close()


async def moderate_teacher_application(
    application_id: int,
    moderator_max_user_id: int,
    decision: str,
    comment: str | None = None,
) -> tuple[bool, str | None, int | None, str | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT teacher_applications.id,
                       teacher_applications.max_user_id,
                       teacher_applications.status,
                       teacher_invites.id AS invite_id,
                       teacher_invites.class_id,
                       classes.name AS class_name
                FROM teacher_applications
                JOIN teacher_invites ON teacher_invites.id = teacher_applications.invite_id
                JOIN classes ON classes.id = teacher_invites.class_id
                WHERE teacher_applications.id = $1
                FOR UPDATE
                """,
                application_id,
            )
            if row is None or row["status"] != "pending":
                return False, None, None, None

            if decision == "approved":
                await connection.execute(
                    """
                    INSERT INTO users (max_user_id, role, class_id)
                    VALUES ($1, 'teacher', $2)
                    ON CONFLICT (max_user_id)
                    DO UPDATE SET role = 'teacher', class_id = EXCLUDED.class_id
                    """,
                    row["max_user_id"],
                    row["class_id"],
                )
                await connection.execute(
                    "DELETE FROM teacher_class_assignments WHERE teacher_max_user_id = $1",
                    row["max_user_id"],
                )
                await connection.execute(
                    """
                    INSERT INTO teacher_class_assignments (teacher_max_user_id, class_id)
                    SELECT $1, class_id
                    FROM teacher_application_classes
                    WHERE application_id = $2
                    UNION
                    SELECT $1, $3
                    ON CONFLICT DO NOTHING
                    """,
                    row["max_user_id"],
                    application_id,
                    row["class_id"],
                )
                await connection.execute(
                    """
                    UPDATE teacher_applications
                    SET status = 'approved', moderator_comment = $2, updated_at = NOW()
                    WHERE id = $1
                    """,
                    application_id,
                    comment,
                )
                await connection.execute(
                    """
                    UPDATE teacher_invites
                    SET status = 'used', used_by_max_user_id = $2, used_at = NOW()
                    WHERE id = $1
                    """,
                    row["invite_id"],
                    row["max_user_id"],
                )
                assigned_names = await connection.fetchval(
                    """
                    SELECT string_agg(classes.name, ', ' ORDER BY classes.name)
                    FROM teacher_class_assignments
                    JOIN classes ON classes.id = teacher_class_assignments.class_id
                    WHERE teacher_class_assignments.teacher_max_user_id = $1
                    """,
                    row["max_user_id"],
                )
                return (
                    True,
                    "approved",
                    int(row["max_user_id"]),
                    assigned_names or row["class_name"],
                )

            await connection.execute(
                """
                UPDATE teacher_applications
                SET status = 'rejected', moderator_comment = $2, updated_at = NOW()
                WHERE id = $1
                """,
                application_id,
                comment,
            )
            return True, "rejected", int(row["max_user_id"]), row["class_name"]
    finally:
        await connection.close()


async def get_setting_text(key: str, default: str) -> str:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow("SELECT value FROM settings WHERE key = $1", key)
        if row is None:
            return default
        value = row["value"]
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                return str(decoded)
            except json.JSONDecodeError:
                return value
        return str(value)
    finally:
        await connection.close()


async def set_setting(key: str, value: str) -> None:
    connection = await asyncpg.connect(get_database_url())
    try:
        await connection.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            key,
            json.dumps(value, ensure_ascii=False),
        )
    finally:
        await connection.close()


async def create_class(name: str, age_group: str, invite_code: str) -> bool:
    connection = await asyncpg.connect(get_database_url())
    try:
        try:
            await connection.execute(
                """
                INSERT INTO classes (name, age_group, invite_code)
                VALUES ($1, $2, $3)
                """,
                name,
                age_group,
                invite_code.upper(),
            )
        except asyncpg.UniqueViolationError:
            return False
        return True
    finally:
        await connection.close()


async def list_classes_with_codes() -> list[tuple[str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT name, age_group, invite_code
            FROM classes
            WHERE archived = FALSE
            ORDER BY age_group, name
            """
        )
        return [(row["name"], row["age_group"], row["invite_code"]) for row in rows]
    finally:
        await connection.close()


async def list_classes_for_removal() -> list[tuple[str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT name, age_group
            FROM classes
            WHERE archived = FALSE
            ORDER BY age_group, name
            """
        )
        return [(row["name"], row["age_group"]) for row in rows]
    finally:
        await connection.close()


async def get_class_usage(
    class_name: str,
) -> tuple[int, int, int, int, int, int, int, int, int, int] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            """
            SELECT classes.id,
                   (SELECT COUNT(*) FROM users WHERE users.class_id = classes.id) AS users_count,
                   (SELECT COUNT(*) FROM teacher_invites WHERE teacher_invites.class_id = classes.id) AS invites_count,
                   (SELECT COUNT(*) FROM events WHERE events.author_class_id = classes.id) AS events_count,
                   (SELECT COUNT(*) FROM event_signups WHERE event_signups.class_id = classes.id) AS signups_count,
                   (SELECT COUNT(*) FROM reports WHERE reports.class_id = classes.id) AS reports_count,
                   (SELECT COUNT(*) FROM ideas WHERE ideas.author_class_id = classes.id) AS ideas_count,
                   (SELECT COUNT(*) FROM points_log WHERE points_log.class_id = classes.id) AS points_count,
                   (SELECT COUNT(*) FROM teacher_class_assignments
                    WHERE teacher_class_assignments.class_id = classes.id)
                   + (SELECT COUNT(*) FROM teacher_application_classes
                      WHERE teacher_application_classes.class_id = classes.id)
                       AS teacher_links_count,
                   (SELECT COUNT(*) FROM discipline_penalties
                    WHERE discipline_penalties.class_id = classes.id)
                       AS penalties_count
            FROM classes
            WHERE classes.name = $1 AND classes.archived = FALSE
            """,
            class_name,
        )
        if row is None:
            return None
        return tuple(
            int(row[key])
            for key in (
                "id",
                "users_count",
                "invites_count",
                "events_count",
                "signups_count",
                "reports_count",
                "ideas_count",
                "points_count",
                "teacher_links_count",
                "penalties_count",
            )
        )  # type: ignore[return-value]
    finally:
        await connection.close()


async def delete_class_if_empty(class_name: str) -> bool:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT classes.id
                FROM classes
                WHERE classes.name = $1 AND classes.archived = FALSE
                FOR UPDATE
                """,
                class_name,
            )
            if row is None:
                return False
            usage = await connection.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM users WHERE users.class_id = $1) AS users_count,
                    (SELECT COUNT(*) FROM teacher_invites WHERE teacher_invites.class_id = $1) AS invites_count,
                    (SELECT COUNT(*) FROM events WHERE events.author_class_id = $1) AS events_count,
                    (SELECT COUNT(*) FROM event_signups WHERE event_signups.class_id = $1) AS signups_count,
                    (SELECT COUNT(*) FROM reports WHERE reports.class_id = $1) AS reports_count,
                    (SELECT COUNT(*) FROM ideas WHERE ideas.author_class_id = $1) AS ideas_count,
                    (SELECT COUNT(*) FROM points_log WHERE points_log.class_id = $1) AS points_count,
                    (SELECT COUNT(*) FROM teacher_class_assignments
                     WHERE teacher_class_assignments.class_id = $1)
                    + (SELECT COUNT(*) FROM teacher_application_classes
                       WHERE teacher_application_classes.class_id = $1)
                        AS teacher_links_count,
                    (SELECT COUNT(*) FROM discipline_penalties
                     WHERE discipline_penalties.class_id = $1)
                        AS penalties_count
                """,
                row["id"],
            )
            if usage is None or any(int(usage[key]) for key in usage.keys()):
                return False
            await connection.execute("DELETE FROM classes WHERE id = $1", row["id"])
            return True
    finally:
        await connection.close()


async def archive_class(class_name: str) -> bool:
    connection = await asyncpg.connect(get_database_url())
    try:
        result = await connection.execute(
            "UPDATE classes SET archived = TRUE WHERE name = $1 AND archived = FALSE",
            class_name,
        )
        return result == "UPDATE 1"
    finally:
        await connection.close()


async def create_event(
    title: str,
    description: str,
    event_date: date,
    author_class_name: str,
) -> int | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            "SELECT id FROM classes WHERE name = $1 AND archived = FALSE",
            author_class_name,
        )
        if row is None:
            return None

        event = await connection.fetchrow(
            """
            INSERT INTO events (title, description, event_date, author_class_id, status)
            VALUES ($1, $2, $3, $4, 'approved')
            RETURNING id
            """,
            title,
            description,
            event_date,
            row["id"],
        )
        return int(event["id"])
    finally:
        await connection.close()


async def list_events(
    age_group: str | None = None,
) -> list[tuple[int, str, str, str, str, str]]:
    """Список одобренных мероприятий для указанной возрастной группы.

    Если группа не задана, возвращает все мероприятия (для администраторов и модераторов).
    """
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT events.id, events.title, events.description,
                   events.event_date, classes.name AS class_name,
                   classes.age_group
            FROM events
            JOIN classes ON classes.id = events.author_class_id
            WHERE events.status = 'approved'
              AND ($1::text IS NULL OR classes.age_group = $1)
            ORDER BY events.event_date, events.id
            """,
            age_group,
        )
        return [
            (
                row["id"],
                row["title"],
                row["description"],
                row["event_date"].isoformat(),
                row["class_name"],
                row["age_group"],
            )
            for row in rows
        ]
    finally:
        await connection.close()


async def list_signed_up_events(
    max_user_id: int,
) -> list[tuple[int, str, str, str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT events.id, events.title, events.description,
                   events.event_date, event_classes.name AS class_name,
                   event_classes.age_group
            FROM users
            JOIN event_signups ON event_signups.class_id = users.class_id
            JOIN events ON events.id = event_signups.event_id
            JOIN classes AS user_classes ON user_classes.id = users.class_id
            JOIN classes AS event_classes
              ON event_classes.id = events.author_class_id
             AND event_classes.age_group = user_classes.age_group
            WHERE users.max_user_id = $1
              AND events.status = 'approved'
            ORDER BY events.event_date, events.id
            """,
            max_user_id,
        )
        return [
            (
                row["id"],
                row["title"],
                row["description"],
                row["event_date"].isoformat(),
                row["class_name"],
                row["age_group"],
            )
            for row in rows
        ]
    finally:
        await connection.close()


async def signup_for_event(max_user_id: int, event_id: int) -> str:
    connection = await asyncpg.connect(get_database_url())
    try:
        user = await connection.fetchrow(
            """
            SELECT users.class_id, classes.age_group
            FROM users
            JOIN classes ON classes.id = users.class_id
            WHERE users.max_user_id = $1
            """,
            max_user_id,
        )
        if user is None or user["class_id"] is None:
            return "not_bound"

        event = await connection.fetchrow(
            """
            SELECT events.id, classes.age_group
            FROM events
            JOIN classes ON classes.id = events.author_class_id
            WHERE events.id = $1 AND events.status = 'approved'
            """,
            event_id,
        )
        if event is None:
            return "not_found"
        if event["age_group"] != user["age_group"]:
            return "wrong_group"

        try:
            await connection.execute(
                """
                INSERT INTO event_signups (event_id, class_id)
                VALUES ($1, $2)
                """,
                event_id,
                user["class_id"],
            )
        except asyncpg.UniqueViolationError:
            return "already"
        return "ok"
    finally:
        await connection.close()


async def _report_period_status(
    connection: asyncpg.Connection,
    event_date: date,
) -> str:
    """Проверяет, открыт ли календарный период для отправки отчёта."""
    await close_due_rating_periods(connection)
    if event_date > moscow_today():
        return "event_not_finished"
    period_month = month_start(event_date)
    row = await connection.fetchrow(
        "SELECT status FROM rating_periods WHERE period_month = $1",
        period_month,
    )
    if row is None:
        return "period_not_open"
    if row["status"] != "open" or moscow_today() > report_deadline(period_month):
        return "period_closed"
    return "ok"


async def create_report(
    max_user_id: int,
    event_id: int,
    comment: str | None,
    media_token: str | None = None,
) -> tuple[str, int | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        user = await connection.fetchrow(
            """
            SELECT users.class_id, classes.age_group
            FROM users
            JOIN classes ON classes.id = users.class_id
            WHERE users.max_user_id = $1
            """,
            max_user_id,
        )
        if user is None or user["class_id"] is None:
            return "not_bound", None

        event = await connection.fetchrow(
            """
            SELECT events.event_date, classes.age_group
            FROM events
            JOIN classes ON classes.id = events.author_class_id
            WHERE events.id = $1 AND events.status = 'approved'
            """,
            event_id,
        )
        if event is None:
            return "not_found", None
        if event["age_group"] != user["age_group"]:
            return "wrong_group", None

        signup = await connection.fetchrow(
            """
            SELECT event_signups.id
            FROM event_signups
            JOIN events ON events.id = event_signups.event_id
            WHERE event_signups.event_id = $1
              AND event_signups.class_id = $2
              AND events.status = 'approved'
            """,
            event_id,
            user["class_id"],
        )
        if signup is None:
            return "not_signed", None

        period_status = await _report_period_status(connection, event["event_date"])
        if period_status != "ok":
            return period_status, None

        existing = await connection.fetchrow(
            """
            SELECT id, status
            FROM reports
            WHERE event_id = $1 AND class_id = $2
            ORDER BY id
            LIMIT 1
            FOR UPDATE
            """,
            event_id,
            user["class_id"],
        )
        if existing is not None:
            if existing["status"] == "rejected":
                return "rejected_exists", int(existing["id"])
            return "class_already_reported", int(existing["id"])

        report = await connection.fetchrow(
            """
            INSERT INTO reports (
                event_id, class_id, submitted_by_max_user_id, media_token, comment
            )
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            event_id,
            user["class_id"],
            max_user_id,
            media_token,
            comment,
        )
        return "ok", int(report["id"])
    except asyncpg.UniqueViolationError:
        return "class_already_reported", None
    finally:
        await connection.close()


async def get_class_report(
    max_user_id: int,
    event_id: int,
) -> tuple[int, str, int | None, str] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            """
            SELECT reports.id, reports.status,
                   reports.submitted_by_max_user_id,
                   COALESCE(reports.moderator_comment, '') AS moderator_comment
            FROM reports
            JOIN users ON users.class_id = reports.class_id
            JOIN classes AS user_classes ON user_classes.id = users.class_id
            JOIN events ON events.id = reports.event_id
            JOIN classes AS event_classes
              ON event_classes.id = events.author_class_id
             AND event_classes.age_group = user_classes.age_group
            WHERE users.max_user_id = $1
              AND reports.event_id = $2
            ORDER BY reports.id
            LIMIT 1
            """,
            max_user_id,
            event_id,
        )
        if row is None:
            return None
        return (
            int(row["id"]),
            row["status"],
            int(row["submitted_by_max_user_id"])
            if row["submitted_by_max_user_id"] is not None
            else None,
            row["moderator_comment"],
        )
    finally:
        await connection.close()


async def revise_report(
    max_user_id: int,
    event_id: int,
    comment: str | None,
    media_token: str | None = None,
) -> tuple[str, int | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        user = await connection.fetchrow(
            """
            SELECT users.class_id, classes.age_group
            FROM users
            JOIN classes ON classes.id = users.class_id
            WHERE users.max_user_id = $1
            """,
            max_user_id,
        )
        if user is None or user["class_id"] is None:
            return "not_bound", None

        event = await connection.fetchrow(
            """
            SELECT events.event_date, classes.age_group
            FROM events
            JOIN classes ON classes.id = events.author_class_id
            WHERE events.id = $1 AND events.status = 'approved'
            """,
            event_id,
        )
        if event is None:
            return "not_found", None
        if event["age_group"] != user["age_group"]:
            return "wrong_group", None

        signup = await connection.fetchrow(
            """
            SELECT event_signups.id
            FROM event_signups
            JOIN events ON events.id = event_signups.event_id
            WHERE event_signups.event_id = $1
              AND event_signups.class_id = $2
              AND events.status = 'approved'
            """,
            event_id,
            user["class_id"],
        )
        if signup is None:
            return "not_signed", None

        period_status = await _report_period_status(connection, event["event_date"])
        if period_status != "ok":
            return period_status, None

        report = await connection.fetchrow(
            """
            SELECT id, status, submitted_by_max_user_id
            FROM reports
            WHERE event_id = $1 AND class_id = $2
            ORDER BY id
            LIMIT 1
            FOR UPDATE
            """,
            event_id,
            user["class_id"],
        )
        if report is None:
            return "not_found", None
        if report["status"] != "rejected":
            return "class_already_reported", int(report["id"])
        if report["submitted_by_max_user_id"] != max_user_id:
            return "not_report_author", int(report["id"])

        await connection.execute(
            """
            UPDATE reports
            SET media_token = $2,
                comment = $3,
                status = 'pending',
                checked_by = NULL,
                checked_at = NULL,
                moderator_comment = NULL
            WHERE id = $1
            """,
            report["id"],
            media_token,
            comment,
        )
        return "ok", int(report["id"])
    finally:
        await connection.close()


async def get_report_submitter_id(report_id: int) -> int | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            "SELECT submitted_by_max_user_id FROM reports WHERE id = $1",
            report_id,
        )
        if row is None or row["submitted_by_max_user_id"] is None:
            return None
        return int(row["submitted_by_max_user_id"])
    finally:
        await connection.close()


async def list_pending_reports() -> list[tuple[int, int, str, str, str, str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT reports.id, events.id AS event_id, events.title,
                   classes.name AS class_name,
                   classes.age_group AS class_age_group,
                   event_classes.age_group AS event_age_group,
                   COALESCE(reports.comment, '') AS comment,
                   COALESCE(reports.media_token, '') AS media_token
            FROM reports
            JOIN events ON events.id = reports.event_id
            JOIN classes ON classes.id = reports.class_id
            JOIN classes AS event_classes ON event_classes.id = events.author_class_id
            WHERE reports.status = 'pending'
            ORDER BY reports.created_at, reports.id
            """
        )
        return [
            (
                row["id"],
                row["event_id"],
                row["title"],
                row["class_name"],
                row["class_age_group"],
                row["event_age_group"],
                row["comment"],
                row["media_token"],
            )
            for row in rows
        ]
    finally:
        await connection.close()


async def moderate_report(
    report_id: int,
    moderator_max_user_id: int,
    decision: str,
    comment: str | None = None,
) -> tuple[bool, str | None, int | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            await lock_ratings(connection)
            row = await connection.fetchrow(
                """
                SELECT reports.id, reports.class_id, reports.status,
                       events.event_date,
                       report_classes.age_group AS report_age_group,
                       event_classes.age_group AS event_age_group
                FROM reports
                JOIN events ON events.id = reports.event_id
                JOIN classes AS report_classes ON report_classes.id = reports.class_id
                JOIN classes AS event_classes ON event_classes.id = events.author_class_id
                WHERE reports.id = $1
                FOR UPDATE OF reports
                """,
                report_id,
            )
            if row is None or row["status"] != "pending":
                return False, None, None

            if decision == "approved":
                if row["report_age_group"] != row["event_age_group"]:
                    return False, "wrong_group", None
                points_row = await connection.fetchrow(
                    """
                    SELECT COALESCE((
                        SELECT (value->>'participation')::int
                        FROM settings WHERE key = 'points_scale'
                    ), 5) AS points
                    """
                )
                points = points_row["points"]
                await connection.execute(
                    """
                    UPDATE reports
                    SET status = 'approved', checked_by = (
                            SELECT id FROM users WHERE max_user_id = $2
                        ),
                        checked_at = NOW(),
                        moderator_comment = COALESCE($3, moderator_comment)
                    WHERE id = $1
                    """,
                    report_id,
                    moderator_max_user_id,
                    comment,
                )
                await connection.execute(
                    "UPDATE classes SET points = points + $2 WHERE id = $1",
                    row["class_id"],
                    points,
                )
                await connection.execute(
                    """
                    INSERT INTO points_log (
                        class_id, reason, points, period_month, confirmed_by
                    )
                    VALUES ($1, $2, $4, $5, (
                        SELECT id FROM users WHERE max_user_id = $3
                    ))
                    """,
                    row["class_id"],
                    f"Отчёт №{report_id} подтверждён",
                    moderator_max_user_id,
                    points,
                    month_start(row["event_date"]),
                )
                await refresh_period_winners(connection, month_start(row["event_date"]))
                return True, "approved", points

            await connection.execute(
                """
                UPDATE reports
                SET status = 'rejected', checked_by = (
                        SELECT id FROM users WHERE max_user_id = $2
                    ),
                    checked_at = NOW(),
                    moderator_comment = $3
                WHERE id = $1
                """,
                report_id,
                moderator_max_user_id,
                comment,
            )
            return True, "rejected", None
    finally:
        await connection.close()


async def refresh_period_winners(
    connection: asyncpg.Connection,
    period_month: date,
) -> None:
    """Пересчитывает победителей закрытого месяца, включая поздние одобрения."""
    period = await connection.fetchrow(
        """
        SELECT id, status
        FROM rating_periods
        WHERE period_month = $1
        """,
        period_month,
    )
    if period is None or period["status"] != "closed":
        return

    await connection.execute(
        "DELETE FROM rating_winners WHERE period_id = $1",
        period["id"],
    )
    await connection.execute(
        """
        WITH totals AS (
            SELECT points_log.class_id, classes.age_group,
                   SUM(points_log.points)::int AS points
            FROM points_log
            JOIN classes ON classes.id = points_log.class_id
            WHERE points_log.period_month = $2
            GROUP BY points_log.class_id, classes.age_group
        ), ranked AS (
            SELECT class_id, age_group, points,
                   MAX(points) OVER (PARTITION BY age_group) AS group_max
            FROM totals
        )
        INSERT INTO rating_winners (period_id, class_id, points)
        SELECT $1, class_id, points
        FROM ranked
        WHERE points = group_max AND points > 0
        ON CONFLICT (period_id, class_id)
        DO UPDATE SET points = EXCLUDED.points
        """,
        period["id"],
        period_month,
    )


async def close_due_rating_periods(
    connection: asyncpg.Connection,
) -> None:
    """Закрывает месяцы после 5-го числа следующего месяца."""
    async with connection.transaction():
        await lock_ratings(connection)
        today = moscow_today()
        rows = await connection.fetch(
            """
            SELECT period_month, status FROM rating_periods
            """
        )
        for row in rows:
            period_month = row["period_month"]
            if row["status"] == "closed":
                # Перестраивает победителей по трём возрастным группам,
                # включая месяцы, закрытые до обновления бота.
                await refresh_period_winners(connection, period_month)
                continue
            if today <= report_deadline(period_month):
                continue
            await connection.execute(
                """
                UPDATE rating_periods
                SET status = 'closed', closed_at = NOW()
                WHERE period_month = $1 AND status = 'open'
                """,
                period_month,
            )
            await refresh_period_winners(connection, period_month)


async def close_expired_months() -> None:
    connection = await asyncpg.connect(get_database_url())
    try:
        await close_due_rating_periods(connection)
    finally:
        await connection.close()


async def set_monthly_value(value: str) -> tuple[str, date]:
    """Открывает текущий календарный месяц и сохраняет его ценность."""
    period_month = month_start(moscow_today())
    if not is_school_month(period_month):
        return "holidays", period_month
    value = value.strip()
    if not 1 <= len(value) <= 200:
        return "invalid", period_month
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            await close_due_rating_periods(connection)
            period_month = month_start(moscow_today())
            existing = await connection.fetchrow(
                """
                SELECT status
                FROM rating_periods
                WHERE period_month = $1
                FOR UPDATE
                """,
                period_month,
            )
            if existing is not None and existing["status"] == "closed":
                return "closed", period_month

            await connection.execute(
                """
                INSERT INTO rating_periods (period_month, monthly_value, status)
                VALUES ($1, $2, 'open')
                ON CONFLICT (period_month)
                DO UPDATE SET monthly_value = EXCLUDED.monthly_value,
                              status = 'open'
                """,
                period_month,
                value,
            )
            await connection.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES ('monthly_value', $1::jsonb, NOW())
                ON CONFLICT (key)
                DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """,
                json.dumps(value, ensure_ascii=False),
            )
            return "ok", period_month
    finally:
        await connection.close()


async def get_current_month_value() -> str | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        await close_due_rating_periods(connection)
        period_month = month_start(moscow_today())
        row = await connection.fetchrow(
            """
            SELECT monthly_value
            FROM rating_periods
            WHERE period_month = $1
            """,
            period_month,
        )
        if row is not None:
            return row["monthly_value"]
        return None
    finally:
        await connection.close()


async def _list_period_rating(
    connection: asyncpg.Connection,
    period_month: date,
    age_group: str | None = None,
) -> list[tuple[str, str, int, bool]]:
    rows = await connection.fetch(
        """
        SELECT classes.name, classes.age_group,
               COALESCE(totals.points, 0)::int AS points,
               (rating_winners.class_id IS NOT NULL) AS is_winner
        FROM classes
        LEFT JOIN (
            SELECT class_id, SUM(points)::int AS points
            FROM points_log
            WHERE period_month = $1
            GROUP BY class_id
        ) totals ON totals.class_id = classes.id
        LEFT JOIN rating_periods
            ON rating_periods.period_month = $1
        LEFT JOIN rating_winners
            ON rating_winners.period_id = rating_periods.id
           AND rating_winners.class_id = classes.id
        WHERE ($2::text IS NULL OR classes.age_group = $2)
        ORDER BY points DESC, classes.age_group, classes.name
        """,
        period_month,
        age_group,
    )
    return [
        (row["name"], row["age_group"], row["points"], row["is_winner"])
        for row in rows
    ]


async def list_current_month_rating(
    age_group: str | None = None,
) -> tuple[date, str | None, list[tuple[str, str, int, bool]]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        await close_due_rating_periods(connection)
        period_month = month_start(moscow_today())
        value = await connection.fetchval(
            "SELECT monthly_value FROM rating_periods WHERE period_month = $1",
            period_month,
        )
        return period_month, value, await _list_period_rating(connection, period_month, age_group)
    finally:
        await connection.close()


async def list_academic_year_rating(
    age_group: str | None = None,
) -> tuple[str, int, bool, list[tuple[str, str, int, bool]]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        await close_due_rating_periods(connection)
        today = moscow_today()
        start = academic_year_start(today)
        end = date(start.year + 1, 5, 1)
        closed_count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM rating_periods
            WHERE period_month BETWEEN $1 AND $2
              AND status = 'closed'
            """,
            start,
            end,
        )
        # A school that starts using the bot mid-year must still get year winners.
        complete = today > report_deadline(end)
        rows = await connection.fetch(
            """
            SELECT classes.name, classes.age_group,
                   COALESCE(totals.points, 0)::int AS points,
                   FALSE AS is_winner
            FROM classes
            LEFT JOIN (
                SELECT class_id, SUM(points)::int AS points
                FROM points_log
                WHERE period_month BETWEEN $1 AND $2
                GROUP BY class_id
            ) totals ON totals.class_id = classes.id
            WHERE ($3::text IS NULL OR classes.age_group = $3)
            ORDER BY points DESC, classes.age_group, classes.name
            """,
            start,
            end,
            age_group,
        )
        rating = [
            (row["name"], row["age_group"], row["points"], False)
            for row in rows
        ]
        if complete and rating:
            maxima: dict[str, int] = {}
            for _name, group, points, _winner in rating:
                maxima[group] = max(maxima.get(group, 0), points)
            rating = [
                (name, age_group, points, points == maxima[age_group] and points > 0)
                for name, age_group, points, _ in rating
            ]
        return academic_year_label(today), int(closed_count), complete, rating
    finally:
        await connection.close()


async def list_rating_history(
    age_group: str | None = None,
) -> list[tuple[date, str, list[str], int | None]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        await close_due_rating_periods(connection)
        rows = await connection.fetch(
            """
            SELECT rating_periods.period_month,
                   rating_periods.monthly_value,
                   ARRAY_REMOVE(ARRAY_AGG(classes.name ORDER BY classes.name), NULL)
                       AS winner_names,
                   MAX(rating_winners.points) AS winner_points
            FROM rating_periods
            LEFT JOIN rating_winners
                ON rating_winners.period_id = rating_periods.id
               AND (
                   $1::text IS NULL
                   OR rating_winners.class_id IN (
                       SELECT id FROM classes WHERE age_group = $1
                   )
               )
            LEFT JOIN classes
                ON classes.id = rating_winners.class_id
            GROUP BY rating_periods.id, rating_periods.period_month,
                     rating_periods.monthly_value
            ORDER BY rating_periods.period_month DESC
            """,
            age_group,
        )
        return [
            (
                row["period_month"],
                row["monthly_value"],
                list(row["winner_names"] or []),
                row["winner_points"],
            )
            for row in rows
        ]
    finally:
        await connection.close()


async def get_month_rating(period_month: date, age_group: str | None = None):
    connection = await asyncpg.connect(get_database_url())
    try:
        await close_due_rating_periods(connection)
        period = await connection.fetchrow(
            "SELECT monthly_value, status FROM rating_periods WHERE period_month = $1",
            period_month,
        )
        if period is None:
            return None
        return (
            period["monthly_value"],
            period["status"],
            await _list_period_rating(connection, period_month, age_group),
        )
    finally:
        await connection.close()


async def list_rating() -> list[tuple[str, str, int]]:
    """Совместимость со старым вызовом: теперь это рейтинг текущего месяца."""
    _period_month, _value, rating = await list_current_month_rating()
    return [(name, age_group, points) for name, age_group, points, _ in rating]


async def get_class_place(max_user_id: int) -> tuple[int, str, str, int] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        await close_due_rating_periods(connection)
        period_month = month_start(moscow_today())
        row = await connection.fetchrow(
            """
            WITH ranked_classes AS (
                SELECT classes.id, classes.name, classes.age_group,
                       COALESCE(SUM(points_log.points), 0)::int AS points,
                       RANK() OVER (
                           PARTITION BY classes.age_group
                           ORDER BY COALESCE(SUM(points_log.points), 0) DESC
                       ) AS place
                FROM classes
                LEFT JOIN points_log
                    ON points_log.class_id = classes.id
                   AND points_log.period_month = $1
                GROUP BY classes.id, classes.name, classes.age_group
            )
            SELECT ranked_classes.place, ranked_classes.name,
                   ranked_classes.age_group, ranked_classes.points
            FROM users
            JOIN ranked_classes ON ranked_classes.id = users.class_id
            WHERE users.max_user_id = $2
            """,
            period_month,
            max_user_id,
        )
        if row is None:
            return None
        return row["place"], row["name"], row["age_group"], row["points"]
    finally:
        await connection.close()


async def get_class_team(max_user_id: int) -> tuple[str, str, int] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            """
            SELECT classes.name, classes.age_group, COUNT(users.id) AS member_count
            FROM users AS selected_user
            JOIN classes ON classes.id = selected_user.class_id
            LEFT JOIN users ON users.class_id = classes.id
            WHERE selected_user.max_user_id = $1
            GROUP BY classes.id, classes.name, classes.age_group
            """,
            max_user_id,
        )
        if row is None:
            return None
        return row["name"], row["age_group"], row["member_count"]
    finally:
        await connection.close()


async def get_idea_author_id(idea_id: int) -> int | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            "SELECT author_user_id FROM ideas WHERE id = $1",
            idea_id,
        )
        return row["author_user_id"] if row is not None else None
    finally:
        await connection.close()


async def create_idea(
    max_user_id: int,
    title: str,
    description: str,
    target_group: str,
) -> tuple[str, int | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            await lock_ratings(connection)
            current_month = month_start(moscow_today())
            opened = await connection.fetchval(
                "SELECT status = 'open' FROM rating_periods WHERE period_month = $1",
                current_month,
            )
            if not opened or not is_school_month(current_month):
                return "period_not_open", None
            user = await connection.fetchrow(
                "SELECT class_id FROM users WHERE max_user_id = $1",
                max_user_id,
            )
            if user is None or user["class_id"] is None:
                return "not_bound", None

            limit_row = await connection.fetchrow(
                """
                SELECT COUNT(*) AS idea_count,
                       COALESCE((
                           SELECT (value #>> '{}')::int
                           FROM settings WHERE key = 'class_idea_limit'
                       ), 3) AS idea_limit
                FROM ideas
                WHERE author_class_id = $1
                  AND (created_at AT TIME ZONE 'Europe/Moscow')::date >= $2
                  AND (created_at AT TIME ZONE 'Europe/Moscow')::date < $3
                """,
                user["class_id"],
                current_month,
                next_month(current_month),
            )
            if limit_row["idea_count"] >= limit_row["idea_limit"]:
                return "limit", None

            row = await connection.fetchrow(
                """
                INSERT INTO ideas (title, description, target_group, author_class_id, author_user_id)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                title,
                description,
                target_group,
                user["class_id"],
                max_user_id,
            )
            return "ok", int(row["id"])
    finally:
        await connection.close()


async def list_pending_ideas() -> list[tuple[int, str, str, str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT ideas.id, ideas.title, ideas.description,
                   ideas.target_group, classes.name AS class_name,
                   classes.age_group
            FROM ideas
            JOIN classes ON classes.id = ideas.author_class_id
            WHERE ideas.status = 'pending'
            ORDER BY ideas.created_at, ideas.id
            """
        )
        return [
            (
                row["id"],
                row["title"],
                row["description"],
                row["target_group"],
                row["class_name"],
                row["age_group"],
            )
            for row in rows
        ]
    finally:
        await connection.close()


async def moderate_idea(
    idea_id: int,
    moderator_id: int,
    decision: str,
    comment: str | None = None,
) -> tuple[bool, str | None, int | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            await lock_ratings(connection)
            row = await connection.fetchrow(
                """
                SELECT id, author_class_id, status
                       , title, description, target_group, author_user_id,
                       created_at
                FROM ideas
                WHERE id = $1
                FOR UPDATE
                """,
                idea_id,
            )
            if row is None or row["status"] != "pending":
                return False, None, None

            if decision == "approved":
                limit_row = await connection.fetchrow(
                    """
                    SELECT COUNT(*) AS approved_count,
                           COALESCE((
                               SELECT (value #>> '{}')::int
                               FROM settings WHERE key = 'monthly_approval_limit'
                           ), 5) AS approval_limit
                    FROM ideas
                    WHERE author_class_id = $1
                      AND status = 'approved'
                      AND (created_at AT TIME ZONE 'Europe/Moscow')::date >= $2
                      AND (created_at AT TIME ZONE 'Europe/Moscow')::date < $3
                    """,
                    row["author_class_id"],
                    created_month(row["created_at"]),
                    next_month(created_month(row["created_at"])),
                )
                if limit_row["approved_count"] >= limit_row["approval_limit"]:
                    return False, "limit", None

                points_row = await connection.fetchrow(
                    """
                    SELECT COALESCE((
                        SELECT (value->>'approved_idea')::int
                        FROM settings WHERE key = 'points_scale'
                    ), 10) AS points
                    """
                )
                points = points_row["points"]
                await connection.execute(
                    """
                    UPDATE ideas
                    SET status = 'approved', moderator_comment = $2, updated_at = NOW()
                    WHERE id = $1
                    """,
                    idea_id,
                    comment,
                )
                await connection.execute(
                    "UPDATE classes SET points = points + $2 WHERE id = $1",
                    row["author_class_id"],
                    points,
                )
                await connection.execute(
                    """
                    INSERT INTO points_log (
                        class_id, reason, points, period_month, confirmed_by
                    )
                    VALUES ($1, $2, $4, $5, (
                        SELECT id FROM users WHERE max_user_id = $3
                    ))
                    """,
                    row["author_class_id"],
                    f"Идея №{idea_id} прошла модерацию",
                    moderator_id,
                    points,
                    created_month(row["created_at"]),
                )
                await refresh_period_winners(
                    connection, created_month(row["created_at"])
                )
                event = await connection.fetchrow(
                    """
                    INSERT INTO events (title, description, event_date, author_class_id, status)
                    VALUES ($1, $2, CURRENT_DATE + 7, $3, 'approved')
                    RETURNING id
                    """,
                    row["title"],
                    f"{row['description']}\nЦелевая группа: {row['target_group']}",
                    row["author_class_id"],
                )
                return True, "approved", int(event["id"])

            await connection.execute(
                """
                UPDATE ideas
                SET status = 'rejected', moderator_comment = $2, updated_at = NOW()
                WHERE id = $1
                """,
                idea_id,
                comment,
            )
            return True, "rejected", None
    finally:
        await connection.close()


async def award_prize(class_name: str, place: int, confirmer_id: int) -> tuple[bool, int | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            await lock_ratings(connection)
            current_month = month_start(moscow_today())
            opened = await connection.fetchval(
                "SELECT status = 'open' FROM rating_periods WHERE period_month = $1",
                current_month,
            )
            if not opened or not is_school_month(current_month):
                return False, None
            class_row = await connection.fetchrow(
                "SELECT id FROM classes WHERE name = $1",
                class_name,
            )
            if class_row is None:
                return False, None
            points_row = await connection.fetchrow(
                """
                SELECT COALESCE((
                    SELECT (value->>'prize_place')::int
                    FROM settings WHERE key = 'points_scale'
                ), 15) AS points
                """
            )
            points = points_row["points"]
            await connection.execute(
                "UPDATE classes SET points = points + $2 WHERE id = $1",
                class_row["id"],
                points,
            )
            await connection.execute(
                """
                INSERT INTO points_log (
                    class_id, reason, points, period_month, confirmed_by
                )
                VALUES ($1, $2, $3, $5, (
                    SELECT id FROM users WHERE max_user_id = $4
                ))
                """,
                class_row["id"],
                f"Призовое место: {place}",
                points,
                confirmer_id,
                month_start(moscow_today()),
            )
            await refresh_period_winners(connection, month_start(moscow_today()))
            return True, points
    finally:
        await connection.close()


MONTHLY_BONUS_LIMITS = {
    "punctuality": 15,
    "attendance": 15,
    "discipline": 25,
    "tidiness": 15,
}

MONTHLY_BONUS_LABELS = {
    "punctuality": "Пунктуальность",
    "attendance": "Посещаемость",
    "discipline": "Дисциплина",
    "tidiness": "Опрятность и форма",
}


async def get_monthly_bonus_status(
    class_name: str,
    category: str,
) -> tuple[str, int, int, int]:
    """Returns status, monthly penalty count, effective cap, current bonus."""
    if category not in MONTHLY_BONUS_LIMITS:
        return "invalid_category", 0, 0, 0

    current_month = month_start(moscow_today())
    connection = await asyncpg.connect(get_database_url())
    try:
        class_row = await connection.fetchrow(
            "SELECT id FROM classes WHERE name = $1 AND archived = FALSE",
            class_name,
        )
        if class_row is None:
            return "class_not_found", 0, 0, 0

        period_open = await connection.fetchval(
            "SELECT status = 'open' FROM rating_periods WHERE period_month = $1",
            current_month,
        )
        if not period_open or not is_school_month(current_month):
            return "period_not_open", 0, 0, 0

        penalty_count = 0
        if category == "discipline":
            penalty_count = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM discipline_penalties
                WHERE class_id = $1 AND period_month = $2
                """,
                class_row["id"],
                current_month,
            )
        current_points = await connection.fetchval(
            """
            SELECT points
            FROM monthly_class_bonuses
            WHERE class_id = $1 AND period_month = $2 AND category = $3
            """,
            class_row["id"],
            current_month,
            category,
        )
        cap = MONTHLY_BONUS_LIMITS[category]
        if category == "discipline":
            cap = max(0, cap - int(penalty_count))
        return "ok", int(penalty_count), cap, int(current_points or 0)
    finally:
        await connection.close()


async def award_monthly_bonus(
    admin_max_user_id: int,
    class_name: str,
    category: str,
    points: int,
) -> tuple[str, int, int]:
    """Set a class's monthly bonus; updating it adjusts all ratings by delta."""
    if category not in MONTHLY_BONUS_LIMITS:
        return "invalid_category", 0, 0
    if points < 0:
        return "invalid_points", 0, 0
    if (
        admin_max_user_id not in get_admin_ids()
        and await get_user_role(admin_max_user_id) != "admin"
    ):
        return "not_admin", 0, 0

    current_month = month_start(moscow_today())
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            await lock_ratings(connection)
            class_row = await connection.fetchrow(
                """
                SELECT id FROM classes
                WHERE name = $1 AND archived = FALSE
                FOR UPDATE
                """,
                class_name,
            )
            if class_row is None:
                return "class_not_found", 0, 0
            period_open = await connection.fetchval(
                "SELECT status = 'open' FROM rating_periods WHERE period_month = $1",
                current_month,
            )
            if not period_open or not is_school_month(current_month):
                return "period_not_open", 0, 0

            penalty_count = 0
            if category == "discipline":
                penalty_count = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM discipline_penalties
                    WHERE class_id = $1 AND period_month = $2
                    """,
                    class_row["id"],
                    current_month,
                )
            cap = MONTHLY_BONUS_LIMITS[category]
            if category == "discipline":
                cap = max(0, cap - int(penalty_count))
            if points > cap:
                return "exceeds_limit", int(penalty_count), cap

            existing = await connection.fetchrow(
                """
                SELECT monthly_class_bonuses.points,
                       monthly_class_bonuses.points_log_id
                FROM monthly_class_bonuses
                WHERE class_id = $1 AND period_month = $2 AND category = $3
                FOR UPDATE
                """,
                class_row["id"],
                current_month,
                category,
            )
            previous_points = int(existing["points"]) if existing else 0
            delta = points - previous_points
            label = MONTHLY_BONUS_LABELS[category]
            reason = f"Ежемесячный бонус: {label}"
            if existing is None:
                points_log_id = await connection.fetchval(
                    """
                    INSERT INTO points_log (class_id, reason, points, period_month, confirmed_by)
                    VALUES ($1, $2, $3, $4, (
                        SELECT id FROM users WHERE max_user_id = $5
                    ))
                    RETURNING id
                    """,
                    class_row["id"],
                    reason,
                    points,
                    current_month,
                    admin_max_user_id,
                )
                await connection.execute(
                    """
                    INSERT INTO monthly_class_bonuses (
                        class_id, period_month, category, points,
                        points_log_id, updated_by_max_user_id
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    class_row["id"],
                    current_month,
                    category,
                    points,
                    points_log_id,
                    admin_max_user_id,
                )
            else:
                points_log_id = existing["points_log_id"]
                await connection.execute(
                    """
                    UPDATE points_log
                    SET points = $2, reason = $3,
                        confirmed_by = (SELECT id FROM users WHERE max_user_id = $4)
                    WHERE id = $1
                    """,
                    points_log_id,
                    points,
                    reason,
                    admin_max_user_id,
                )
                await connection.execute(
                    """
                    UPDATE monthly_class_bonuses
                    SET points = $2, updated_by_max_user_id = $3, updated_at = NOW()
                    WHERE class_id = $1 AND period_month = $4 AND category = $5
                    """,
                    class_row["id"],
                    points,
                    admin_max_user_id,
                    current_month,
                    category,
                )

            if delta:
                await connection.execute(
                    "UPDATE classes SET points = points + $2 WHERE id = $1",
                    class_row["id"],
                    delta,
                )
            await refresh_period_winners(connection, current_month)
            return "ok", int(penalty_count), cap
    finally:
        await connection.close()


async def add_discipline_penalty(
    teacher_max_user_id: int,
    class_name: str,
    note: str | None = None,
) -> tuple[str, int, int, int]:
    """Record a teacher remark and enforce the reduced discipline bonus cap."""
    current_month = month_start(moscow_today())
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            await lock_ratings(connection)
            role = await connection.fetchval(
                "SELECT role FROM users WHERE max_user_id = $1",
                teacher_max_user_id,
            )
            if role != "teacher":
                return "not_teacher", 0, 0, 0

            class_row = await connection.fetchrow(
                """
                SELECT classes.id
                FROM classes
                JOIN teacher_class_assignments assigned ON assigned.class_id = classes.id
                WHERE assigned.teacher_max_user_id = $1
                  AND classes.name = $2 AND classes.archived = FALSE
                FOR UPDATE OF classes
                """,
                teacher_max_user_id,
                class_name,
            )
            if class_row is None:
                return "class_not_assigned", 0, 0, 0

            period_open = await connection.fetchval(
                "SELECT status = 'open' FROM rating_periods WHERE period_month = $1",
                current_month,
            )
            if not period_open or not is_school_month(current_month):
                return "period_not_open", 0, 0, 0

            await connection.execute(
                """
                INSERT INTO discipline_penalties (
                    class_id, period_month, teacher_max_user_id, note
                ) VALUES ($1, $2, $3, $4)
                """,
                class_row["id"],
                current_month,
                teacher_max_user_id,
                note,
            )
            penalty_count = int(await connection.fetchval(
                """
                SELECT COUNT(*) FROM discipline_penalties
                WHERE class_id = $1 AND period_month = $2
                """,
                class_row["id"],
                current_month,
            ))
            cap = max(0, MONTHLY_BONUS_LIMITS["discipline"] - penalty_count)
            bonus = await connection.fetchrow(
                """
                SELECT points, points_log_id
                FROM monthly_class_bonuses
                WHERE class_id = $1 AND period_month = $2 AND category = 'discipline'
                FOR UPDATE
                """,
                class_row["id"],
                current_month,
            )
            deducted = 0
            if bonus is not None and int(bonus["points"]) > cap:
                previous_points = int(bonus["points"])
                deducted = previous_points - cap
                await connection.execute(
                    """
                    UPDATE monthly_class_bonuses
                    SET points = $2, updated_by_max_user_id = $3, updated_at = NOW()
                    WHERE class_id = $1 AND period_month = $4 AND category = 'discipline'
                    """,
                    class_row["id"],
                    cap,
                    teacher_max_user_id,
                    current_month,
                )
                await connection.execute(
                    """
                    UPDATE points_log
                    SET points = $2,
                        reason = 'Ежемесячный бонус: Дисциплина (пересчёт после замечаний)'
                    WHERE id = $1
                    """,
                    bonus["points_log_id"],
                    cap,
                )
                await connection.execute(
                    "UPDATE classes SET points = points - $2 WHERE id = $1",
                    class_row["id"],
                    deducted,
                )
            await refresh_period_winners(connection, current_month)
            return "ok", penalty_count, cap, deducted
    finally:
        await connection.close()

