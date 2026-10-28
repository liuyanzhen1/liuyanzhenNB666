import asyncio
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from parsers import command_matches, parse_ack
from tg_big_bet import BigBetRunner, Store


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_runner(self, root: Path, *, live=False, target="100"):
        return BigBetRunner(
            client=None,
            group=None,
            group_id=-100,
            bot_id=99,
            store=Store(root / "rounds.sqlite3"),
            min_amount=10,
            max_amount=10,
            stop_turnover=Decimal(target),
            start_balance=Decimal("100"),
            live=live,
            ack_timeout=1,
            report_dir=root / "reports",
        )

    async def test_idle_automatic_pause_recovers_but_manual_pause_does_not(self):
        with TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            try:
                runner.set_run_requested(True)
                runner.automatic_pause("group_error")
                runner.last_bet_activity_at = 0
                self.assertTrue(await runner.recover_if_idle(now=181))
                self.assertFalse(runner.paused)
                self.assertEqual(runner.recovery_count, 1)

                runner.set_run_requested(False)
                runner.automatic_pause("manual")
                runner.last_bet_activity_at = 0
                self.assertFalse(await runner.recover_if_idle(now=400))
                self.assertTrue(runner.paused)
                self.assertEqual(runner.recovery_count, 1)
            finally:
                runner.store.conn.close()

    async def test_late_ack_recalculates_result_that_arrived_first(self):
        with TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory), live=True)
            try:
                runner.store.ensure_round(
                    "SL1",
                    game_mode="scheduled",
                    bet_delivery="group",
                    live_mode=1,
                    bet_side="大",
                    unit_amount=10,
                    bet_amount=10,
                    command="大10",
                    send_status="ack_timeout",
                    sent_message_id=88,
                )
                await runner.handle_result("SL1", 6, 90, "第 SL1 期输赢 骰子为: 6")
                self.assertEqual(runner.store.get("SL1")["outcome"], "unknown")
                event = SimpleNamespace(
                    chat_id=-100,
                    sender_id=99,
                    raw_text="大10 下注成功 余额:90",
                    message=SimpleNamespace(
                        id=91,
                        reply_to=SimpleNamespace(reply_to_msg_id=88),
                    ),
                )
                await runner.handle_ack(event, parse_ack(event.raw_text), event.raw_text)
                row = runner.store.get("SL1")
                self.assertEqual(row["send_status"], "accepted")
                self.assertEqual(row["outcome"], "win")
                self.assertEqual(row["net"], "10")
                self.assertEqual(runner.store.total_turnover(True), Decimal("10"))
            finally:
                runner.store.conn.close()

    async def test_watchdog_skips_stalled_instant_order_after_three_minutes(self):
        with TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory), live=True)
            try:
                runner.game_mode = "instant_dice"
                runner.store.ensure_round(
                    "IN1",
                    game_mode="instant_dice",
                    bet_delivery="group",
                    live_mode=1,
                    bet_side="大",
                    unit_amount=10,
                    bet_amount=10,
                    command="大10",
                    send_status="result_timeout",
                )
                runner.instant_active_issue = "IN1"
                runner.set_run_requested(True)
                runner.automatic_pause("result_timeout")
                runner.last_bet_activity_at = 0
                self.assertTrue(await runner.recover_if_idle(now=181))
                self.assertFalse(runner.paused)
                self.assertEqual(len(runner.store.all_rounds()), 1)
                self.assertEqual(runner.store.get("IN1")["send_status"], "abandoned")
                self.assertEqual(runner.recovery_count, 1)
            finally:
                runner.cancel_scheduled_instant_round()
                runner.store.conn.close()

    async def test_resume_button_skips_stalled_order_and_starts_next_one(self):
        with TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory), live=True)
            runner.client = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(id=88))
            )
            runner.group = object()
            try:
                runner.game_mode = "instant_dice"
                runner.store.ensure_round(
                    "IN_OLD",
                    game_mode="instant_dice",
                    bet_delivery="group",
                    live_mode=1,
                    bet_side="大",
                    unit_amount=10,
                    bet_amount=10,
                    command="大10",
                    send_status="result_timeout",
                    sent_message_id=77,
                )
                runner.instant_active_issue = "IN_OLD"
                runner.automatic_pause("result_timeout")
                await runner.dashboard_action("resume", {})

                self.assertEqual(runner.store.get("IN_OLD")["send_status"], "abandoned")
                current = [row for row in runner.store.all_rounds() if row["issue"] != "IN_OLD"]
                self.assertEqual(len(current), 1)
                self.assertEqual(current[0]["send_status"], "sent")
                runner.client.send_message.assert_awaited_once()

                late = SimpleNamespace(
                    chat_id=-100,
                    sender_id=99,
                    raw_text="",
                    message=SimpleNamespace(
                        id=90,
                        media=SimpleNamespace(value=6, emoticon="🎲"),
                        reply_to=SimpleNamespace(reply_to_msg_id=77),
                    ),
                )
                await runner.handle_instant_dice(late, 6)
                self.assertIsNone(runner.store.get("IN_OLD")["dice"])
                self.assertEqual(runner.store.total_turnover(True), Decimal("0"))
            finally:
                for task in list(runner.timeout_tasks.values()):
                    task.cancel()
                await asyncio.gather(
                    *runner.timeout_tasks.values(), return_exceptions=True
                )
                runner.store.conn.close()

    async def test_fresh_open_immediately_recovers_an_automatic_pause(self):
        with TemporaryDirectory() as directory:
            runner = self.make_runner(Path(directory))
            try:
                runner.set_run_requested(True)
                runner.automatic_pause("ack_timeout")
                await runner.handle_open("SL2", 200, "期号: SL2")
                self.assertFalse(runner.paused)
                self.assertEqual(runner.store.get("SL2")["send_status"], "dry_run")
            finally:
                runner.store.conn.close()


class CommandMatchTests(unittest.TestCase):
    def test_amount_prefix_is_not_treated_as_same_order(self):
        self.assertTrue(command_matches("大10 下注成功", "大10"))
        self.assertTrue(command_matches("大10\n单10 下注成功", "大10 单10"))
        self.assertFalse(command_matches("大100 下注成功", "大10"))
        self.assertFalse(command_matches("大10 单100 下注成功", "大10 单10"))


if __name__ == "__main__":
    unittest.main()
