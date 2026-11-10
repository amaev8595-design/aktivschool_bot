import asyncio
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
with patch.dict(os.environ, {"MAX_BOT_TOKEN": "test-token-no-network", "BOT_MODE": "polling"}):
    import main


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_closes_on_startup_and_in_background(self):
        checked_again = asyncio.Event()
        real_sleep = asyncio.sleep
        calls = 0

        async def close():
            nonlocal calls
            calls += 1
            if calls >= 2:
                checked_again.set()

        async def sleep(_seconds):
            await real_sleep(0.001)

        with patch.object(main, "close_expired_months", close), patch.object(main.asyncio, "sleep", sleep):
            async with main.rating_maintenance():
                self.assertEqual(calls, 1)
                await asyncio.wait_for(checked_again.wait(), timeout=1)
            calls_at_exit = calls
            await real_sleep(0.01)
            self.assertEqual(calls, calls_at_exit)

    async def test_polling_uses_maintenance(self):
        with patch.object(main, "close_expired_months", new_callable=AsyncMock) as close, patch.object(main.dispatcher, "start_polling", new_callable=AsyncMock) as polling:
            await main.run_polling()
            close.assert_awaited_once()
            polling.assert_awaited_once_with(main.bot, clear_subscriptions=True)

    async def test_full_ranking_is_split_without_dropping_rows(self):
        message = SimpleNamespace(answer=AsyncMock())
        lines = [f"{i}. Класс — 15 баллов" for i in range(300)]
        await main.answer_lines(message, lines, None)
        sent = [call.args[0] for call in message.answer.await_args_list]
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(text) <= 3500 for text in sent))
        self.assertEqual("\n".join(sent), "\n".join(lines))

    async def test_bot_started_uses_sdk_attachment(self):
        update = SimpleNamespace(user=SimpleNamespace(user_id=100))
        with patch.object(main, "start_screen", new_callable=AsyncMock, return_value=("Привет", main.main_keyboard())), patch.object(main.bot, "send_message", new_callable=AsyncMock) as send:
            await main.bot_started_handler(update)
            self.assertNotIn("keyboard", send.await_args.kwargs)
            self.assertEqual(len(send.await_args.kwargs["attachments"]), 1)


if __name__ == "__main__":
    unittest.main()
