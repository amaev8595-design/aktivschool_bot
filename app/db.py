import csv
import json
import os
import secrets
from datetime import date
from pathlib import Path

import asyncpg


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


async def bind_user_to_class(max_user_id: int, invite_code: str) -> tuple[str, str] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            "SELECT id, name, age_group FROM classes WHERE invite_code = $1",
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
            "SELECT id, name FROM classes WHERE name = $1",
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
) -> tuple[str, int | None, str | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        async with connection.transaction():
            invite = await connection.fetchrow(
                """
                SELECT teacher_invites.id, classes.name AS class_name
                FROM teacher_invites
                JOIN classes ON classes.id = teacher_invites.class_id
                WHERE teacher_invites.code = $1
                  AND teacher_invites.status = 'active'
                FOR UPDATE
                """,
                invite_code.strip().upper(),
            )
            if invite is None:
                return "invalid_code", None, None

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
                   classes.name AS class_name
            FROM teacher_applications
            JOIN teacher_invites ON teacher_invites.id = teacher_applications.invite_id
            JOIN classes ON classes.id = teacher_invites.class_id
            WHERE teacher_applications.status = 'pending'
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
                return True, "approved", int(row["max_user_id"]), row["class_name"]

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
            ORDER BY age_group, name
            """
        )
        return [(row["name"], row["age_group"], row["invite_code"]) for row in rows]
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
            "SELECT id FROM classes WHERE name = $1",
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


async def list_events() -> list[tuple[int, str, str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT events.id, events.title, events.description,
                   events.event_date, classes.name AS class_name
            FROM events
            JOIN classes ON classes.id = events.author_class_id
            WHERE events.status = 'approved'
            ORDER BY events.event_date, events.id
            """
        )
        return [
            (
                row["id"],
                row["title"],
                row["description"],
                row["event_date"].isoformat(),
                row["class_name"],
            )
            for row in rows
        ]
    finally:
        await connection.close()


async def list_signed_up_events(max_user_id: int) -> list[tuple[int, str, str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT events.id, events.title, events.description,
                   events.event_date, classes.name AS class_name
            FROM users
            JOIN event_signups ON event_signups.class_id = users.class_id
            JOIN events ON events.id = event_signups.event_id
            JOIN classes ON classes.id = events.author_class_id
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
            )
            for row in rows
        ]
    finally:
        await connection.close()


async def signup_for_event(max_user_id: int, event_id: int) -> str:
    connection = await asyncpg.connect(get_database_url())
    try:
        user = await connection.fetchrow(
            "SELECT class_id FROM users WHERE max_user_id = $1",
            max_user_id,
        )
        if user is None or user["class_id"] is None:
            return "not_bound"

        event = await connection.fetchrow(
            "SELECT id FROM events WHERE id = $1 AND status = 'approved'",
            event_id,
        )
        if event is None:
            return "not_found"

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


async def create_report(
    max_user_id: int,
    event_id: int,
    comment: str | None,
    media_token: str | None = None,
) -> tuple[str, int | None]:
    connection = await asyncpg.connect(get_database_url())
    try:
        user = await connection.fetchrow(
            "SELECT class_id FROM users WHERE max_user_id = $1",
            max_user_id,
        )
        if user is None or user["class_id"] is None:
            return "not_bound", None

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
            "SELECT class_id FROM users WHERE max_user_id = $1",
            max_user_id,
        )
        if user is None or user["class_id"] is None:
            return "not_bound", None

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


async def list_pending_reports() -> list[tuple[int, int, str, str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT reports.id, events.id AS event_id, events.title,
                   classes.name AS class_name,
                   COALESCE(reports.comment, '') AS comment,
                   COALESCE(reports.media_token, '') AS media_token
            FROM reports
            JOIN events ON events.id = reports.event_id
            JOIN classes ON classes.id = reports.class_id
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
            row = await connection.fetchrow(
                """
                SELECT id, class_id, status
                FROM reports
                WHERE id = $1
                FOR UPDATE
                """,
                report_id,
            )
            if row is None or row["status"] != "pending":
                return False, None, None

            if decision == "approved":
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
                    INSERT INTO points_log (class_id, reason, points, confirmed_by)
                    VALUES ($1, $2, $4, (
                        SELECT id FROM users WHERE max_user_id = $3
                    ))
                    """,
                    row["class_id"],
                    f"Отчёт №{report_id} подтверждён",
                    moderator_max_user_id,
                    points,
                )
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


async def list_rating() -> list[tuple[str, str, int]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT name, age_group, points
            FROM classes
            ORDER BY points DESC, age_group, name
            """
        )
        return [(row["name"], row["age_group"], row["points"]) for row in rows]
    finally:
        await connection.close()


async def get_class_place(max_user_id: int) -> tuple[int, str, str, int] | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            """
            WITH ranked_classes AS (
                SELECT id, name, age_group, points,
                       RANK() OVER (ORDER BY points DESC) AS place
                FROM classes
            )
            SELECT ranked_classes.place, ranked_classes.name,
                   ranked_classes.age_group, ranked_classes.points
            FROM users
            JOIN ranked_classes ON ranked_classes.id = users.class_id
            WHERE users.max_user_id = $1
            """,
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
                  AND created_at >= date_trunc('month', NOW())
                """,
                user["class_id"],
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


async def list_pending_ideas() -> list[tuple[int, str, str, str, str]]:
    connection = await asyncpg.connect(get_database_url())
    try:
        rows = await connection.fetch(
            """
            SELECT ideas.id, ideas.title, ideas.description,
                   ideas.target_group, classes.name AS class_name
            FROM ideas
            JOIN classes ON classes.id = ideas.author_class_id
            WHERE ideas.status = 'pending'
            ORDER BY ideas.created_at, ideas.id
            """
        )
        return [
            (row["id"], row["title"], row["description"], row["target_group"], row["class_name"])
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
            row = await connection.fetchrow(
                """
                SELECT id, author_class_id, status
                       , title, description, target_group, author_user_id
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
                      AND created_at >= date_trunc('month', NOW())
                    """,
                    row["author_class_id"],
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
                    INSERT INTO points_log (class_id, reason, points, confirmed_by)
                    VALUES ($1, $2, $4, (
                        SELECT id FROM users WHERE max_user_id = $3
                    ))
                    """,
                    row["author_class_id"],
                    f"Идея №{idea_id} прошла модерацию",
                    moderator_id,
                    points,
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
                INSERT INTO points_log (class_id, reason, points, confirmed_by)
                VALUES ($1, $2, $3, (
                    SELECT id FROM users WHERE max_user_id = $4
                ))
                """,
                class_row["id"],
                f"Призовое место: {place}",
                points,
                confirmer_id,
            )
            return True, points
    finally:
        await connection.close()

