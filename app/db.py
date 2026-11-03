import csv
import os
from pathlib import Path

import asyncpg


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

