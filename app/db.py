import csv
import json
import os
from pathlib import Path

import asyncpg


def get_admin_ids() -> set[int]:
    admin_ids: set[int] = set()
    for value in os.getenv("ADMIN_IDS", "").split(","):
        value = value.strip()
        if value.isdigit():
            admin_ids.add(int(value))
    return admin_ids


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


async def create_idea(
    max_user_id: int,
    title: str,
    description: str,
    target_group: str,
) -> int | None:
    connection = await asyncpg.connect(get_database_url())
    try:
        row = await connection.fetchrow(
            """
            INSERT INTO ideas (title, description, target_group, author_class_id)
            SELECT $1, $2, $3, class_id
            FROM users
            WHERE max_user_id = $4 AND class_id IS NOT NULL
            RETURNING id
            """,
            title,
            description,
            target_group,
            max_user_id,
        )
        return int(row["id"]) if row is not None else None
    finally:
        await connection.close()

