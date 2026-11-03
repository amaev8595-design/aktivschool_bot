import os
from pathlib import Path

import asyncpg


async def initialize_database() -> asyncpg.Pool:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("Переменная DATABASE_URL не заполнена в файле .env")

    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

    async with pool.acquire() as connection:
        await connection.execute(schema)

    return pool

