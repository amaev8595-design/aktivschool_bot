import csv
import hashlib
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
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
    active_year = await connection.fetchval(
        "SELECT value #>> '{}' FROM settings WHERE key = 'active_school_year'"
    )
    existing = await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM classes WHERE academic_year = $1 AND archived = FALSE)",
        active_year,
    )
    if existing:
        return
    path = Path(__file__).parent / "data" / "classes.csv"
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            await connection.execute(
                """
                INSERT INTO classes (name, age_group, invite_code, academic_year)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT DO NOTHING
                """,
                row["name"].strip(),
                row["age_group"].strip(),
                row["invite_code"].strip().upper(),
                active_year,
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


async def list_teacher_invites() -> list[tuple[str, str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT teacher_invites.code, classes.name AS class_name,
                   classes.academic_year,
                   teacher_invites.status
            FROM teacher_invites
            JOIN classes ON classes.id = teacher_invites.class_id
            ORDER BY teacher_invites.created_at DESC, teacher_invites.id DESC
            """
        )
        return [
            (row["code"], row["class_name"], row["academic_year"], row["status"])
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
                INSERT INTO classes (name, age_group, invite_code, academic_year)
                VALUES (
                    $1, $2, $3,
                    (SELECT value #>> '{}' FROM settings WHERE key = 'active_school_year')
                )
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


def _age_group_for_grade(grade: int) -> str:
    if grade <= 4:
        return "1-4"
    if grade <= 8:
        return "5-8"
    return "9-11"


def _next_grade_class_name(class_name: str) -> tuple[int, str] | None:
    match = re.fullmatch(r"\s*(\d{1,2})\s*(.*?)\s*", class_name)
    if match is None:
        return None
    grade = int(match.group(1))
    if not 1 <= grade <= 11:
        return None
    return grade, f"{grade + 1}{match.group(2).strip()}"


async def _school_year_rollover_preview(
    connection: asyncpg.Connection,
    source_year: str,
) -> dict[str, object]:
    source_start = int(source_year.split("/", 1)[0])
    target_year = f"{source_start + 1}/{source_start + 2}"
    prior = await connection.fetchrow(
        "SELECT to_year FROM school_year_rollovers WHERE from_year = $1",
        source_year,
    )
    rows = await connection.fetch(
        """
        SELECT id, name, age_group
        FROM classes
        WHERE academic_year = $1 AND archived = FALSE
        ORDER BY name
        """,
        source_year,
    )
    errors: list[str] = []
    promote: list[tuple[int, str, str, str]] = []
    graduate: list[tuple[int, str]] = []
    for row in rows:
        parsed = _next_grade_class_name(row["name"])
        if parsed is None:
            errors.append(f"Не удалось определить параллель класса «{row['name']}».")
            continue
        grade, next_name = parsed
        if grade == 11:
            graduate.append((int(row["id"]), row["name"]))
        else:
            promote.append(
                (int(row["id"]), row["name"], next_name, _age_group_for_grade(grade + 1))
            )

    next_names = [row[2] for row in promote]
    collisions = await connection.fetch(
        """
        SELECT name, academic_year FROM classes
        WHERE archived = FALSE AND academic_year <> $1 AND name = ANY($2::text[])
        """,
        source_year,
        next_names,
    ) if next_names else []
    for row in collisions:
        errors.append(
            f"Активный класс «{row['name']}» уже есть в учебном году {row['academic_year']}."
        )

    user_counts = await connection.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE class_id = ANY($1::bigint[]))::int AS all_users,
               COUNT(*) FILTER (WHERE class_id = ANY($2::bigint[]))::int AS graduate_users
        FROM users
        """,
        [item[0] for item in promote],
        [item[0] for item in graduate],
    )
    promoted_ids = [item[0] for item in promote]
    graduating_ids = [item[0] for item in graduate]
    source_ids = promoted_ids + graduating_ids
    counts = await connection.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM teacher_class_assignments
             WHERE class_id = ANY($1::bigint[]))::int AS teacher_assignments,
            (SELECT COUNT(DISTINCT applications.id)
             FROM teacher_applications AS applications
             LEFT JOIN teacher_invites AS invites ON invites.id = applications.invite_id
             LEFT JOIN teacher_application_classes AS selected
                 ON selected.application_id = applications.id
             WHERE applications.status = 'pending'
               AND (invites.class_id = ANY($1::bigint[])
                    OR selected.class_id = ANY($1::bigint[])))::int
                AS pending_teacher_applications
        """,
        source_ids,
    )
    user_snapshot = await connection.fetch(
        """
        SELECT max_user_id, class_id FROM users
        WHERE class_id = ANY($1::bigint[])
        ORDER BY class_id, max_user_id
        """,
        source_ids,
    )
    teacher_snapshot = await connection.fetch(
        """
        SELECT teacher_max_user_id, class_id FROM teacher_class_assignments
        WHERE class_id = ANY($1::bigint[])
        ORDER BY class_id, teacher_max_user_id
        """,
        source_ids,
    )
    pending_snapshot = await connection.fetch(
        """
        SELECT DISTINCT applications.id, applications.max_user_id,
               invites.class_id AS primary_class_id,
               selected.class_id AS selected_class_id
        FROM teacher_applications AS applications
        LEFT JOIN teacher_invites AS invites ON invites.id = applications.invite_id
        LEFT JOIN teacher_application_classes AS selected
            ON selected.application_id = applications.id
        WHERE applications.status = 'pending'
          AND (invites.class_id = ANY($1::bigint[])
               OR selected.class_id = ANY($1::bigint[]))
        ORDER BY applications.id, selected.class_id
        """,
        source_ids,
    )
    token_payload = json.dumps(
        {
            "from_year": source_year,
            "promote": promote,
            "graduate": graduate,
            "errors": errors,
            "moved_users": int(user_counts["all_users"] or 0),
            "unassigned_users": int(user_counts["graduate_users"] or 0),
            "teacher_assignments": int(counts["teacher_assignments"] or 0),
            "pending_teacher_applications": int(counts["pending_teacher_applications"] or 0),
            "users": [(int(row["max_user_id"]), int(row["class_id"])) for row in user_snapshot],
            "teacher_assignments_snapshot": [
                (int(row["teacher_max_user_id"]), int(row["class_id"]))
                for row in teacher_snapshot
            ],
            "pending_applications_snapshot": [
                (
                    int(row["id"]),
                    int(row["max_user_id"]),
                    int(row["primary_class_id"]) if row["primary_class_id"] is not None else None,
                    int(row["selected_class_id"]) if row["selected_class_id"] is not None else None,
                )
                for row in pending_snapshot
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    application_classes: dict[int, dict[str, object]] = {}
    for row in pending_snapshot:
        app = application_classes.setdefault(
            int(row["id"]),
            {
                "user_id": int(row["max_user_id"]),
                "primary_class_id": row["primary_class_id"],
                "selected_ids": set(),
            },
        )
        if row["selected_class_id"] is not None:
            app["selected_ids"].add(int(row["selected_class_id"]))
    promoted_source_ids = {item[0] for item in promote}
    graduating_source_ids = {item[0] for item in graduate}
    rejected_applicants = [
        int(app["user_id"])
        for app in application_classes.values()
        if app["primary_class_id"] in graduating_source_ids
        and not app["selected_ids"].intersection(promoted_source_ids)
    ]
    return {
        "from_year": source_year,
        "to_year": target_year,
        "promote": promote,
        "graduate": graduate,
        "errors": errors,
        "already_done": prior is not None,
        "moved_users": int(user_counts["all_users"] or 0),
        "unassigned_users": int(user_counts["graduate_users"] or 0),
        "teacher_assignments": int(counts["teacher_assignments"] or 0),
        "pending_teacher_applications": int(counts["pending_teacher_applications"] or 0),
        "rejected_applicants": rejected_applicants,
        "preview_token": hashlib.sha256(token_payload.encode("utf-8")).hexdigest(),
    }


async def get_school_year_rollover_preview() -> dict[str, object]:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            source_year = await connection.fetchval(
                "SELECT value #>> '{}' FROM settings WHERE key = 'active_school_year'"
            )
            if not source_year:
                raise RuntimeError("Не задан текущий учебный год")
            return await _school_year_rollover_preview(connection, source_year)
    finally:
        await connection.close()


async def rollover_school_year(
    expected_source_year: str,
    expected_preview_token: str,
    performed_by: int,
) -> tuple[str, dict[str, object]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            current_year = await connection.fetchval(
                """
                SELECT value #>> '{}' FROM settings
                WHERE key = 'active_school_year' FOR UPDATE
                """
            )
            if current_year != expected_source_year:
                return "year_changed", {"from_year": current_year}
            preview = await _school_year_rollover_preview(connection, current_year)
            if preview["already_done"]:
                return "already_done", preview
            if preview["preview_token"] != expected_preview_token:
                return "preview_changed", preview
            if not preview["promote"] and not preview["graduate"]:
                return "no_classes", preview
            if preview["errors"]:
                return "conflict", preview

            old_ids = [item[0] for item in preview["promote"]] + [
                item[0] for item in preview["graduate"]
            ]
            graduate_ids = [item[0] for item in preview["graduate"]]
            await connection.execute(
                "UPDATE classes SET archived = TRUE WHERE id = ANY($1::bigint[])",
                old_ids,
            )
            class_id_map: dict[int, int] = {}
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            for old_id, _old_name, new_name, age_group in preview["promote"]:
                new_class_id = None
                for _attempt in range(20):
                    invite_code = "AKTIV-" + "".join(
                        secrets.choice(alphabet) for _ in range(8)
                    )
                    inserted = await connection.fetchrow(
                        """
                        INSERT INTO classes (name, age_group, invite_code, academic_year)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT DO NOTHING
                        RETURNING id
                        """,
                        new_name,
                        age_group,
                        invite_code,
                        preview["to_year"],
                    )
                    if inserted is not None:
                        new_class_id = int(inserted["id"])
                        break
                if new_class_id is None:
                    raise RuntimeError("Не удалось создать уникальный код класса")
                class_id_map[old_id] = new_class_id

                for _attempt in range(20):
                    teacher_code = "AKTIV-T-" + "".join(
                        secrets.choice(alphabet) for _ in range(6)
                    )
                    result = await connection.execute(
                        """
                        INSERT INTO teacher_invites (code, class_id)
                        VALUES ($1, $2) ON CONFLICT (code) DO NOTHING
                        """,
                        teacher_code,
                        new_class_id,
                    )
                    if result == "INSERT 0 1":
                        break
                else:
                    raise RuntimeError("Не удалось создать уникальный код учителя")

            for old_id, new_id in class_id_map.items():
                await connection.execute(
                    """
                    UPDATE users SET class_id = $2
                    WHERE class_id = $1
                    """,
                    old_id,
                    new_id,
                )
                await connection.execute(
                    """
                    INSERT INTO teacher_class_assignments (teacher_max_user_id, class_id)
                    SELECT teacher_max_user_id, $2
                    FROM teacher_class_assignments WHERE class_id = $1
                    ON CONFLICT DO NOTHING
                    """,
                    old_id,
                    new_id,
                )
                await connection.execute(
                    """
                    UPDATE teacher_application_classes
                    SET class_id = $2 WHERE class_id = $1
                    """,
                    old_id,
                    new_id,
                )
                await connection.execute(
                    """
                    UPDATE teacher_invites AS invites
                    SET class_id = $2
                    FROM teacher_applications AS applications
                    WHERE applications.invite_id = invites.id
                      AND applications.status = 'pending'
                      AND invites.class_id = $1
                    """,
                    old_id,
                    new_id,
                )

            if graduate_ids:
                await connection.execute(
                    "UPDATE users SET class_id = NULL WHERE class_id = ANY($1::bigint[])",
                    graduate_ids,
                )
                await connection.execute(
                    "DELETE FROM teacher_application_classes WHERE class_id = ANY($1::bigint[])",
                    graduate_ids,
                )
                await connection.execute(
                    """
                    UPDATE teacher_invites AS invites
                    SET class_id = (
                        SELECT selected.class_id
                        FROM teacher_applications AS applications
                        JOIN teacher_application_classes AS selected
                            ON selected.application_id = applications.id
                        JOIN classes ON classes.id = selected.class_id
                        WHERE applications.invite_id = invites.id
                          AND applications.status = 'pending'
                          AND classes.academic_year = $2
                          AND classes.archived = FALSE
                        ORDER BY selected.class_id
                        LIMIT 1
                    )
                    WHERE invites.class_id = ANY($1::bigint[])
                      AND EXISTS (
                          SELECT 1
                          FROM teacher_applications AS applications
                          JOIN teacher_application_classes AS selected
                              ON selected.application_id = applications.id
                          JOIN classes ON classes.id = selected.class_id
                          WHERE applications.invite_id = invites.id
                            AND applications.status = 'pending'
                            AND classes.academic_year = $2
                            AND classes.archived = FALSE
                      )
                    """,
                    graduate_ids,
                    preview["to_year"],
                )
                await connection.execute(
                    """
                    UPDATE teacher_applications
                    SET status = 'rejected',
                        moderator_comment = 'Заявка закрыта при архивировании выпускного класса.',
                        updated_at = NOW()
                    WHERE status = 'pending' AND invite_id IN (
                        SELECT id FROM teacher_invites WHERE class_id = ANY($1::bigint[])
                    )
                    """,
                    graduate_ids,
                )

            await connection.execute(
                "UPDATE events SET status = 'archived' WHERE author_class_id = ANY($1::bigint[])",
                old_ids,
            )
            await connection.execute(
                """
                UPDATE teacher_invites SET status = 'revoked'
                WHERE class_id = ANY($1::bigint[]) AND status = 'active'
                """,
                old_ids,
            )
            await connection.execute(
                """
                UPDATE settings SET value = $2::jsonb, updated_at = NOW()
                WHERE key = 'active_school_year'
                """,
                "active_school_year",
                json.dumps(preview["to_year"], ensure_ascii=False),
            )
            await connection.execute(
                """
                INSERT INTO school_year_rollovers (
                    from_year, to_year, performed_by, promoted_classes,
                    graduated_classes, moved_users, unassigned_users
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                preview["from_year"],
                preview["to_year"],
                performed_by,
                len(preview["promote"]),
                len(preview["graduate"]),
                preview["moved_users"],
                preview["unassigned_users"],
            )
            return "ok", preview
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


async def get_admin_month_dashboard() -> dict[str, object]:
    connection = await asyncpg.connect(get_database_url())
    try:
        await close_due_rating_periods(connection)
        today = moscow_today()
        current_month = month_start(today)
        previous_month = month_start(today - timedelta(days=1))
        report_month = (
            month_start(today - timedelta(days=1))
            if today.day <= 5
            else current_month
        )
        current = await connection.fetchrow(
            """
            SELECT monthly_value, status, closed_at
            FROM rating_periods WHERE period_month = $1
            """,
            current_month,
        )
        report_period = await connection.fetchrow(
            "SELECT status FROM rating_periods WHERE period_month = $1",
            report_month,
        )
        previous_period = await connection.fetchrow(
            "SELECT status FROM rating_periods WHERE period_month = $1",
            previous_month,
        )
        queues = await connection.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM teacher_applications WHERE status = 'pending')::int
                    AS teacher_applications,
                (SELECT COUNT(*) FROM reports WHERE status = 'pending')::int AS reports,
                (SELECT COUNT(*) FROM ideas WHERE status = 'pending')::int AS ideas
            """
        )
        return {
            "today": today,
            "current_month": current_month,
            "current_value": current["monthly_value"] if current else None,
            "current_status": current["status"] if current else "not_open",
            "current_closed_at": current["closed_at"] if current else None,
            "previous_month": previous_month,
            "previous_status": previous_period["status"] if previous_period else "not_open",
            "report_month": report_month,
            "report_deadline": report_deadline(report_month),
            "report_status": report_period["status"] if report_period else "not_open",
            "pending_teacher_applications": int(queues["teacher_applications"]),
            "pending_reports": int(queues["reports"]),
            "pending_ideas": int(queues["ideas"]),
        }
    finally:
        await connection.close()


async def get_deadline_reminder_info(
    reminder_date: date,
) -> tuple[date, int, set[int]] | None:
    if reminder_date.day not in {1, 3, 5}:
        return None
    period_month = month_start(reminder_date - timedelta(days=1))
    if not is_school_month(period_month):
        return None
    connection = await asyncpg.connect(get_database_url())
    try:
        period = await connection.fetchrow(
            "SELECT status FROM rating_periods WHERE period_month = $1",
            period_month,
        )
        if period is None or period["status"] != "open":
            return None
        sent_to = await connection.fetch(
            "SELECT admin_max_user_id FROM admin_deadline_reminders WHERE reminder_date = $1",
            reminder_date,
        )
        pending_reports = await connection.fetchval(
            """
            SELECT COUNT(*) FROM reports
            JOIN events ON events.id = reports.event_id
            WHERE reports.status = 'pending'
              AND events.event_date >= $1
              AND events.event_date < $2
            """,
            period_month,
            next_month(period_month),
        )
        return period_month, int(pending_reports), {
            int(row["admin_max_user_id"]) for row in sent_to
        }
    finally:
        await connection.close()


async def mark_deadline_reminder_sent(reminder_date: date, admin_max_user_id: int) -> None:
    connection = await asyncpg.connect(get_database_url())
    try:
        await connection.execute(
            """
            INSERT INTO admin_deadline_reminders (reminder_date, admin_max_user_id)
            VALUES ($1, $2) ON CONFLICT (reminder_date, admin_max_user_id) DO NOTHING
            """,
            reminder_date,
            admin_max_user_id,
        )
    finally:
        await connection.close()


async def _list_period_rating(
    connection: asyncpg.Connection,
    period_month: date,
    age_group: str | None = None,
) -> list[tuple[str, str, int, bool]]:
    year_label = academic_year_label(period_month)
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
          AND (
              classes.academic_year = $3
              OR totals.class_id IS NOT NULL
          )
        ORDER BY points DESC, classes.age_group, classes.name
        """,
        period_month,
        age_group,
        year_label,
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
        active_year = await connection.fetchval(
            "SELECT value #>> '{}' FROM settings WHERE key = 'active_school_year'"
        )
        start_year = int(active_year.split("/", 1)[0])
        start = date(start_year, 9, 1)
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
              AND classes.academic_year = $4
            ORDER BY points DESC, classes.age_group, classes.name
            """,
            start,
            end,
            age_group,
            active_year,
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
        return active_year, int(closed_count), complete, rating
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


async def get_class_rating_breakdown(
    class_name: str,
    age_group: str,
    academic_year: str,
    period_start: date,
    period_end: date,
) -> dict[str, object] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        class_row = await connection.fetchrow(
            """
            SELECT id, name, age_group
            FROM classes
            WHERE name = $1 AND age_group = $2
              AND (
                  academic_year = $3
                  OR EXISTS (
                      SELECT 1 FROM points_log
                      WHERE points_log.class_id = classes.id
                        AND points_log.period_month BETWEEN $4 AND $5
                  )
              )
            ORDER BY (academic_year = $3) DESC
            LIMIT 1
            """,
            class_name,
            age_group,
            academic_year,
            period_start,
            period_end,
        )
        if class_row is None:
            return None
        point_rows = await connection.fetch(
            """
            SELECT reason, points, period_month
            FROM points_log
            WHERE class_id = $1 AND period_month BETWEEN $2 AND $3
            ORDER BY period_month, id
            """,
            class_row["id"],
            period_start,
            period_end,
        )
        categories: dict[str, dict[str, object]] = {
            "reports": {"label": "Отчёты", "points": 0, "count": 0, "entries": []},
            "ideas": {"label": "Идеи", "points": 0, "count": 0, "entries": []},
            "prizes": {"label": "Призовые места", "points": 0, "count": 0, "entries": []},
            "bonuses": {"label": "Дополнительные баллы", "points": 0, "count": 0, "entries": []},
            "other": {"label": "Другие начисления", "points": 0, "count": 0, "entries": []},
        }
        total = 0
        for row in point_rows:
            reason = row["reason"]
            if reason.startswith("Отчёт №"):
                key = "reports"
            elif reason.startswith("Идея №"):
                key = "ideas"
            elif reason.startswith("Призовое место:"):
                key = "prizes"
            elif reason.startswith("Ежемесячный бонус:"):
                key = "bonuses"
            else:
                key = "other"
            bucket = categories[key]
            points = int(row["points"])
            bucket["points"] = int(bucket["points"]) + points
            bucket["count"] = int(bucket["count"]) + 1
            entries = bucket["entries"]
            entries.append({
                "reason": reason,
                "points": points,
                "month": row["period_month"],
            })
            total += points

        penalties = await connection.fetch(
            """
            SELECT period_month, teacher_max_user_id, note, created_at
            FROM discipline_penalties
            WHERE class_id = $1 AND period_month BETWEEN $2 AND $3
            ORDER BY period_month, id
            """,
            class_row["id"],
            period_start,
            period_end,
        )
        return {
            "class_name": class_row["name"],
            "age_group": class_row["age_group"],
            "academic_year": academic_year,
            "period_start": period_start,
            "period_end": period_end,
            "total": total,
            "categories": categories,
            "penalties": [dict(row) for row in penalties],
        }
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
                WHERE classes.academic_year = (
                    SELECT value #>> '{}' FROM settings WHERE key = 'active_school_year'
                ) AND classes.archived = FALSE
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
                """
                SELECT id FROM classes
                WHERE name = $1 AND archived = FALSE
                  AND academic_year = (
                      SELECT value #>> '{}' FROM settings WHERE key = 'active_school_year'
                  )
                """,
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
            await connection.execute(
                """
                INSERT INTO rating_action_audit (
                    action_type, class_id, class_name, period_month, category,
                    actor_max_user_id, event_ref_id, had_bonus,
                    previous_points, new_points
                ) VALUES ('bonus', $1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                class_row["id"],
                class_name,
                current_month,
                category,
                admin_max_user_id,
                points_log_id,
                existing is not None,
                previous_points if existing is not None else None,
                points,
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

            penalty_id = await connection.fetchval(
                """
                INSERT INTO discipline_penalties (
                    class_id, period_month, teacher_max_user_id, note
                ) VALUES ($1, $2, $3, $4)
                RETURNING id
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
            previous_points = int(bonus["points"]) if bonus is not None else None
            new_points = previous_points
            if bonus is not None and int(bonus["points"]) > cap:
                previous_points = int(bonus["points"])
                deducted = previous_points - cap
                new_points = cap
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
            await connection.execute(
                """
                INSERT INTO rating_action_audit (
                    action_type, class_id, class_name, period_month, category,
                    actor_max_user_id, event_ref_id, had_bonus,
                    previous_points, new_points, note
                ) VALUES ('penalty', $1, $2, $3, 'discipline', $4, $5, $6, $7, $8, $9)
                """,
                class_row["id"],
                class_name,
                current_month,
                teacher_max_user_id,
                penalty_id,
                bonus is not None,
                previous_points,
                new_points,
                note,
            )
            await refresh_period_winners(connection, current_month)
            return "ok", penalty_count, cap, deducted
    finally:
        await connection.close()


async def list_recent_rating_actions(
    admin_max_user_id: int,
    limit: int = 20,
) -> list[asyncpg.Record]:
    """List bonus and penalty changes with a safe-to-undo indicator."""
    if (
        admin_max_user_id not in get_admin_ids()
        and await get_user_role(admin_max_user_id) != "admin"
    ):
        return []

    limit = max(1, min(limit, 50))
    connection = await asyncpg.connect(get_database_url())
    try:
        return await connection.fetch(
            """
            SELECT audit.id, audit.action_type, audit.class_name,
                   audit.period_month, audit.category,
                   audit.actor_max_user_id, audit.had_bonus,
                   audit.previous_points, audit.new_points, audit.note,
                   audit.created_at, audit.reversed_at,
                   (
                       audit.reversed_at IS NULL
                       AND audit.class_id IS NOT NULL
                       AND NOT EXISTS (
                           SELECT 1
                           FROM rating_action_audit newer
                           WHERE newer.class_id = audit.class_id
                             AND newer.period_month = audit.period_month
                             AND newer.category = audit.category
                             AND newer.id > audit.id
                             AND newer.reversed_at IS NULL
                       )
                   ) AS can_undo
            FROM rating_action_audit audit
            ORDER BY audit.id DESC
            LIMIT $1
            """,
            limit,
        )
    finally:
        await connection.close()


async def undo_rating_action(
    admin_max_user_id: int,
    action_id: int,
) -> tuple[str, str | None, str | None, int | None, int | None]:
    """Undo the latest change for a class/month/category and preserve its audit."""
    if (
        admin_max_user_id not in get_admin_ids()
        and await get_user_role(admin_max_user_id) != "admin"
    ):
        return "not_admin", None, None, None, None

    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            await lock_ratings(connection)
            action = await connection.fetchrow(
                """
                SELECT * FROM rating_action_audit
                WHERE id = $1
                FOR UPDATE
                """,
                action_id,
            )
            if action is None:
                return "not_found", None, None, None, None
            if action["reversed_at"] is not None:
                return "already_undone", action["class_name"], action["category"], None, None
            if action["class_id"] is None:
                return "class_missing", action["class_name"], action["category"], None, None

            newer_action = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM rating_action_audit
                    WHERE class_id = $1 AND period_month = $2 AND category = $3
                      AND id > $4 AND reversed_at IS NULL
                )
                """,
                action["class_id"],
                action["period_month"],
                action["category"],
                action_id,
            )
            if newer_action:
                return "newer_action_exists", action["class_name"], action["category"], None, None

            class_id = action["class_id"]
            previous_points = action["previous_points"]
            new_points = action["new_points"]
            if action["action_type"] == "bonus":
                bonus = await connection.fetchrow(
                    """
                    SELECT points, points_log_id
                    FROM monthly_class_bonuses
                    WHERE class_id = $1 AND period_month = $2 AND category = $3
                    FOR UPDATE
                    """,
                    class_id,
                    action["period_month"],
                    action["category"],
                )
                if bonus is None or int(bonus["points"]) != int(new_points):
                    return "state_changed", action["class_name"], action["category"], None, None

                if action["had_bonus"]:
                    restored_points = int(previous_points)
                    await connection.execute(
                        """
                        UPDATE monthly_class_bonuses
                        SET points = $4, updated_by_max_user_id = $5, updated_at = NOW()
                        WHERE class_id = $1 AND period_month = $2 AND category = $3
                        """,
                        class_id,
                        action["period_month"],
                        action["category"],
                        restored_points,
                        admin_max_user_id,
                    )
                    await connection.execute(
                        """
                        UPDATE points_log
                        SET points = $2, reason = $3,
                            confirmed_by = (SELECT id FROM users WHERE max_user_id = $4)
                        WHERE id = $1
                        """,
                        bonus["points_log_id"],
                        restored_points,
                        f"Ежемесячный бонус: {MONTHLY_BONUS_LABELS[action['category']]}",
                        admin_max_user_id,
                    )
                else:
                    restored_points = 0
                    await connection.execute(
                        """
                        DELETE FROM monthly_class_bonuses
                        WHERE class_id = $1 AND period_month = $2 AND category = $3
                        """,
                        class_id,
                        action["period_month"],
                        action["category"],
                    )

                delta = restored_points - int(new_points)
                if delta:
                    await connection.execute(
                        "UPDATE classes SET points = points + $2 WHERE id = $1",
                        class_id,
                        delta,
                    )
            else:
                penalty_exists = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM discipline_penalties
                        WHERE id = $1 AND class_id = $2 AND period_month = $3
                    )
                    """,
                    action["event_ref_id"],
                    class_id,
                    action["period_month"],
                )
                if not penalty_exists:
                    return "state_changed", action["class_name"], action["category"], None, None

                if action["had_bonus"]:
                    bonus = await connection.fetchrow(
                        """
                        SELECT points, points_log_id
                        FROM monthly_class_bonuses
                        WHERE class_id = $1 AND period_month = $2 AND category = 'discipline'
                        FOR UPDATE
                        """,
                        class_id,
                        action["period_month"],
                    )
                    if bonus is None or int(bonus["points"]) != int(new_points):
                        return "state_changed", action["class_name"], action["category"], None, None

                await connection.execute(
                    "DELETE FROM discipline_penalties WHERE id = $1",
                    action["event_ref_id"],
                )
                if action["had_bonus"]:
                    restored_points = int(previous_points)
                    await connection.execute(
                        """
                        UPDATE monthly_class_bonuses
                        SET points = $3, updated_by_max_user_id = $4, updated_at = NOW()
                        WHERE class_id = $1 AND period_month = $2 AND category = 'discipline'
                        """,
                        class_id,
                        action["period_month"],
                        restored_points,
                        admin_max_user_id,
                    )
                    await connection.execute(
                        """
                        UPDATE points_log
                        SET points = $2,
                            reason = 'Ежемесячный бонус: Дисциплина (после отмены замечания)',
                            confirmed_by = (SELECT id FROM users WHERE max_user_id = $3)
                        WHERE id = $1
                        """,
                        bonus["points_log_id"],
                        restored_points,
                        admin_max_user_id,
                    )
                    delta = restored_points - int(new_points)
                    if delta:
                        await connection.execute(
                            "UPDATE classes SET points = points + $2 WHERE id = $1",
                            class_id,
                            delta,
                        )

            await connection.execute(
                """
                UPDATE rating_action_audit
                SET reversed_at = NOW(), reversed_by_max_user_id = $2
                WHERE id = $1
                """,
                action_id,
                admin_max_user_id,
            )
            await refresh_period_winners(connection, action["period_month"])
            return (
                "ok",
                action["class_name"],
                action["category"],
                int(previous_points) if previous_points is not None else None,
                int(new_points) if new_points is not None else None,
            )
    finally:
        await connection.close()

