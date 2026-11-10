"""Run with python -m unittest discover -s tests -v.

Database tests require TEST_DATABASE_URL pointing to a disposable PostgreSQL.
Each test owns a separate schema; application credentials are never loaded.
"""
import asyncio
import os
from pathlib import Path
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import asyncpg
import db


class CalendarTests(unittest.TestCase):
    def test_deadline_and_new_year(self):
        self.assertEqual(db.report_deadline(date(2026, 12, 1)), date(2027, 1, 5))
        self.assertEqual(db.report_deadline(date(2028, 2, 1)), date(2028, 3, 5))
        self.assertEqual(db.academic_year_label(date(2027, 8, 31)), "2026/2027")
        self.assertEqual(db.academic_year_label(date(2027, 9, 1)), "2027/2028")
        self.assertFalse(db.is_school_month(date(2027, 6, 1)))

    def test_moscow_midnight(self):
        self.assertEqual(db.created_month(datetime(2026, 10, 31, 21, tzinfo=timezone.utc)), date(2026, 11, 1))
        self.assertEqual(db.created_month(datetime(2026, 10, 31, 20, 59, tzinfo=timezone.utc)), date(2026, 10, 1))


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class RatingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        self.schema_name = "rating_test_" + uuid4().hex
        original_connect = asyncpg.connect
        self.control = await original_connect(self.url)
        await self.control.execute(f'CREATE SCHEMA "{self.schema_name}"')

        async def connect(*args, **kwargs):
            return await original_connect(self.url, server_settings={"search_path": self.schema_name})

        self.connect_patch = patch.object(db.asyncpg, "connect", connect)
        self.connect_patch.start()
        self.env_patch = patch.dict(os.environ, {"DATABASE_URL": self.url})
        self.env_patch.start()
        self.today_patch = patch.object(db, "moscow_today", return_value=date(2026, 11, 3))
        self.today = self.today_patch.start()
        self.conn = await connect()
        self.schema_sql = (Path(db.__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        await self.conn.execute(self.schema_sql)
        await self.conn.execute("INSERT INTO classes (id,name,age_group,invite_code) VALUES (1,'6А','5-8','A'),(2,'6Б','5-8','B'),(3,'6В','5-8','C')")
        await self.conn.execute("INSERT INTO users (max_user_id,role,class_id) VALUES (100,'teacher',1),(200,'teacher',2),(999,'admin',NULL)")
        await self.conn.execute("INSERT INTO rating_periods (period_month,monthly_value) VALUES ('2026-10-01','Дружба'),('2026-11-01','Доброта')")
        await self.conn.execute("INSERT INTO events (id,title,description,event_date,author_class_id) VALUES (1,'Октябрь','Тест','2026-10-20',1),(2,'Ноябрь','Тест','2026-11-02',1),(3,'Будущее','Тест','2026-11-20',1)")
        await self.conn.execute("INSERT INTO event_signups (event_id,class_id) VALUES (1,1),(1,2),(2,1),(3,1)")

    async def asyncTearDown(self):
        await self.conn.close()
        self.today_patch.stop()
        self.env_patch.stop()
        self.connect_patch.stop()
        # Only the random schema created by this test is removed.
        await self.control.execute(f'DROP SCHEMA "{self.schema_name}" CASCADE')
        await self.control.close()

    async def test_deadline_and_rejected_revision(self):
        self.today.return_value = date(2026, 11, 5)
        status, report_id = await db.create_report(100, 1, "Отчёт")
        self.assertEqual(status, "ok")
        await db.moderate_report(report_id, 999, "rejected", "Исправить")
        self.today.return_value = date(2026, 11, 6)
        self.assertEqual((await db.revise_report(100, 1, "Исправлено"))[0], "period_closed")
        self.assertEqual((await db.create_report(200, 1, "Поздно"))[0], "period_closed")
        self.assertEqual((await db.create_report(100, 3, "Заранее"))[0], "event_not_finished")

    async def test_previous_month_and_duplicate_approval(self):
        status, report_id = await db.create_report(100, 1, "Отчёт")
        self.assertEqual(status, "ok")
        self.today.return_value = date(2026, 11, 6)
        await db.close_expired_months()
        results = await asyncio.gather(db.moderate_report(report_id, 999, "approved"), db.moderate_report(report_id, 999, "approved"))
        self.assertEqual(sum(result[0] for result in results), 1)
        self.assertEqual(await self.conn.fetchval("SELECT SUM(points) FROM points_log"), 5)
        self.assertEqual(await self.conn.fetchval("SELECT period_month FROM points_log"), date(2026, 10, 1))
        month, _, rating = await db.list_current_month_rating()
        self.assertEqual(month, date(2026, 11, 1))
        self.assertTrue(all(row[2] == 0 for row in rating))
        self.assertEqual((await db.get_month_rating(date(2026, 10, 1)))[2][0], ("6А", "5-8", 5, True))
        self.assertEqual((await db.list_academic_year_rating())[3][0][2], 5)

    async def test_tied_winners_recalculated_after_late_review(self):
        _, first = await db.create_report(100, 1, "Первый")
        _, second = await db.create_report(200, 1, "Второй")
        await db.moderate_report(first, 999, "approved")
        self.today.return_value = date(2026, 11, 6)
        await asyncio.gather(db.close_expired_months(), db.close_expired_months())
        self.assertEqual(await self.conn.fetchval("SELECT COUNT(*) FROM rating_winners"), 1)
        await db.moderate_report(second, 999, "approved")
        rating = (await db.get_month_rating(date(2026, 10, 1)))[2]
        self.assertEqual([r[0] for r in rating if r[3]], ["6А", "6Б"])
        self.assertEqual(len(rating), 3)
        await db.close_expired_months()
        self.assertEqual(await self.conn.fetchval("SELECT COUNT(*) FROM rating_winners"), 2)

    async def test_open_month_and_year_boundaries(self):
        self.today.return_value = date(2026, 12, 1)
        self.assertEqual((await db.create_idea(100, "Идея", "Описание", "Все"))[0], "period_not_open")
        self.assertFalse((await db.award_prize("6А", 1, 999))[0])
        self.assertEqual((await db.set_monthly_value("Помощь"))[0], "ok")
        self.assertEqual((await db.set_monthly_value("Взаимопомощь"))[0], "ok")
        self.assertTrue((await db.award_prize("6А", 1, 999))[0])
        await self.conn.execute("INSERT INTO points_log (class_id,reason,points,period_month) VALUES (1,'Приз',15,'2026-09-01'),(2,'Приз',30,'2027-05-01'),(3,'Старый год',500,'2025-09-01'),(3,'Лето',500,'2027-06-01')")
        self.today.return_value = date(2027, 6, 5)
        self.assertFalse((await db.list_academic_year_rating())[2])
        self.today.return_value = date(2027, 6, 6)
        year, _, complete, rating = await db.list_academic_year_rating()
        self.assertEqual(year, "2026/2027")
        self.assertTrue(complete)
        self.assertEqual([r[0] for r in rating if r[3]], ["6А", "6Б"])
        self.assertEqual((await db.set_monthly_value("Лето"))[0], "holidays")
        self.today.return_value = date(2027, 9, 1)
        self.assertTrue(all(row[2] == 0 for row in (await db.list_academic_year_rating())[3]))

    async def test_migration_is_idempotent_and_preserves_activity_month(self):
        await self.conn.execute("ALTER TABLE points_log ALTER COLUMN period_month DROP NOT NULL")
        report_id = await self.conn.fetchval("INSERT INTO reports (event_id,class_id,comment,status) VALUES (1,1,'Старый','approved') RETURNING id")
        await self.conn.execute("INSERT INTO points_log (class_id,reason,points,period_month,created_at) VALUES (1,$1,5,NULL,'2026-11-03')", f"Отчёт №{report_id} подтверждён")
        await self.conn.execute("UPDATE classes SET points=5 WHERE id=1")
        await self.conn.execute(self.schema_sql)
        await self.conn.execute(self.schema_sql)
        self.assertEqual(await self.conn.fetchval("SELECT period_month FROM points_log"), date(2026, 10, 1))
        self.assertEqual(await self.conn.fetchval("SELECT COUNT(*) FROM points_log"), 1)
        self.assertEqual(await self.conn.fetchval("SELECT points FROM classes WHERE id=1"), 5)


if __name__ == "__main__":
    unittest.main()
