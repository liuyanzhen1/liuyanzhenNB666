import asyncio
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from parsers import normalize_bet_delivery, normalize_bet_sides, result_sides
from tg_big_bet import BigBetRunner, Store, positive_decimal, random_bet_amount


class StrategyTests(unittest.TestCase):
    def test_random_amount_is_inside_inclusive_range(self):
        values = [random_bet_amount(10, 12) for _ in range(100)]
        self.assertTrue(all(10 <= value <= 12 for value in values))

    def test_equal_limits_create_fixed_amount(self):
        self.assertEqual(random_bet_amount(20, 20), 20)

    def test_invalid_range_is_rejected(self):
        with self.assertRaises(ValueError):
            random_bet_amount(30, 10)

    def test_positive_decimal(self):
        self.assertEqual(positive_decimal("5380.50"), Decimal("5380.50"))

    def test_bet_sides_are_validated_and_ordered(self):
        self.assertEqual(normalize_bet_sides(["双", "大", "大", "单"]), ["大", "单", "双"])
        self.assertEqual(normalize_bet_sides("小，双"), ["小", "双"])
        self.assertEqual(result_sides(5), ["大", "单"])
        self.assertEqual(result_sides(2), ["小", "双"])
        for invalid in ([], ["红"], None):
            with self.assertRaises(ValueError):
                normalize_bet_sides(invalid)

    def test_bet_delivery_is_validated(self):
        self.assertEqual(normalize_bet_delivery(None), "group")
        self.assertEqual(normalize_bet_delivery("private_bot"), "private_bot")
        with self.assertRaises(ValueError):
            normalize_bet_delivery("private_chat")

    def test_turnover_counts_only_successful_bets_for_each_mode(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            try:
                store.ensure_round(
                    "LIVE_OK", live_mode=1, bet_amount=10, send_status="accepted"
                )
                store.ensure_round(
                    "LIVE_REJECTED", live_mode=1, bet_amount=20, send_status="rejected"
                )
                store.ensure_round(
                    "DRY_OK", live_mode=0, bet_amount=30, send_status="dry_run"
                )
                self.assertEqual(store.total_turnover(True), Decimal("10"))
                self.assertEqual(store.total_turnover(False), Decimal("30"))
            finally:
                store.conn.close()


class RunnerStrategyTests(unittest.IsolatedAsyncioTestCase):
    def runner(self, store, directory, sides, target=1000):
        return BigBetRunner(None, None, -1, 1, store, min_amount=10, max_amount=10,
                            stop_turnover=Decimal(target), start_balance=Decimal(100),
                            live=False, ack_timeout=10, report_dir=Path(directory) / "reports",
                            bet_sides=sides)

    async def test_combination_uses_per_option_amount_and_total_turnover(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "combo.sqlite3")
            try:
                runner = self.runner(store, directory, ["大", "单"])
                await runner.handle_open("SL5", 1, "")
                row = store.get("SL5")
                self.assertEqual(row["command"], "大10 单10")
                self.assertEqual(row["unit_amount"], 10)
                self.assertEqual(row["bet_amount"], 20)
                self.assertEqual(store.total_turnover(False), 20)
                await runner.handle_result("SL5", 5, 2, "")
                row = store.get("SL5")
                self.assertEqual(row["result_side"], "大,单")
                self.assertEqual(row["outcome"], "win")
                self.assertEqual(row["net"], "20")
                self.assertEqual(row["estimated_balance_after_result"], "120")
            finally:
                store.conn.close()

    async def test_mixed_combination_is_tie_and_all_four_always_total_four_stakes(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "combo.sqlite3")
            try:
                runner = self.runner(store, directory, ["大", "单"])
                await runner.handle_open("SL6", 1, "")
                await runner.handle_result("SL6", 6, 2, "")
                self.assertEqual(store.get("SL6")["outcome"], "tie")
                self.assertEqual(store.get("SL6")["net"], "0")
                await runner.dashboard_action("set_sides", {"bet_sides": ["大", "小", "单", "双"]})
                await runner.handle_open("SLALL", 3, "")
                await runner.handle_result("SLALL", 1, 4, "")
                row = store.get("SLALL")
                self.assertEqual(row["command"], "大10 小10 单10 双10")
                self.assertEqual(row["bet_amount"], 40)
                self.assertEqual(row["outcome"], "tie")
                self.assertEqual(store.total_turnover(False), 60)
                state = await runner.dashboard_state()
                self.assertEqual(state["summary"]["ties"], 2)
            finally:
                store.conn.close()

    async def test_side_selection_persists_and_empty_selection_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "combo.sqlite3"
            store = Store(path)
            runner = self.runner(store, directory, ["大"])
            await runner.dashboard_action("set_sides", {"bet_sides": ["小", "双"]})
            self.assertEqual(runner.bet_sides, ["小", "双"])
            with self.assertRaises(ValueError):
                await runner.dashboard_action("set_sides", {"bet_sides": []})
            store.conn.close()
            store = Store(path)
            try:
                restored = self.runner(store, directory, ["大"])
                self.assertEqual(restored.bet_sides, ["小", "双"])
            finally:
                store.conn.close()

    async def test_instant_interval_is_validated_and_persisted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "interval.sqlite3"
            store = Store(path)
            runner = self.runner(store, directory, ["大"])
            runner.paused = True
            await runner.dashboard_action(
                "set_game_mode",
                {"game_mode": "instant_dice", "instant_interval": 17},
            )
            self.assertEqual(runner.instant_interval, 17)
            with self.assertRaises(ValueError):
                await runner.dashboard_action(
                    "set_game_mode",
                    {"game_mode": "instant_dice", "instant_interval": 0},
                )
            store.conn.close()
            store = Store(path)
            try:
                restored = self.runner(store, directory, ["大"])
                self.assertEqual(restored.game_mode, "instant_dice")
                self.assertEqual(restored.instant_interval, 17)
            finally:
                store.conn.close()

    async def test_scheduled_mode_reads_group_but_sends_bet_to_bot_private_chat(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "private-bet.sqlite3"
            store = Store(path)
            bot = object()
            client = SimpleNamespace(
                get_entity=AsyncMock(return_value=bot),
                send_message=AsyncMock(return_value=SimpleNamespace(id=88)),
            )
            runner = BigBetRunner(
                client=client,
                group=object(),
                group_id=-7,
                bot_id=99,
                store=store,
                min_amount=10,
                max_amount=10,
                stop_turnover=Decimal("100"),
                start_balance=Decimal("100"),
                live=True,
                ack_timeout=10,
                report_dir=Path(directory) / "reports",
                bet_delivery="private_bot",
            )
            try:
                await runner.handle_open("SLPRIVATE1", 1, "")
                client.get_entity.assert_awaited_once_with(99)
                client.send_message.assert_awaited_once_with(bot, "大10")
                self.assertEqual(store.get("SLPRIVATE1")["send_status"], "sent")
                state = await runner.dashboard_state()
                self.assertEqual(state["bet_delivery"], "private_bot")
                self.assertEqual(state["bet_delivery_label"], "机器人私聊")
            finally:
                for task in runner.timeout_tasks.values():
                    task.cancel()
                await asyncio.gather(*runner.timeout_tasks.values(), return_exceptions=True)
                store.conn.close()

    async def test_dry_run_uses_one_random_amount_and_stops_at_turnover(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            try:
                runner = BigBetRunner(
                    client=None,
                    group=None,
                    group_id=-1,
                    bot_id=1,
                    store=store,
                    min_amount=10,
                    max_amount=10,
                    stop_turnover=Decimal("10"),
                    start_balance=Decimal("100"),
                    live=False,
                    ack_timeout=10,
                    report_dir=Path(directory) / "reports",
                )
                await runner.handle_open("SLTEST1", 1, "期号: SLTEST1")
                await runner.handle_result("SLTEST1", 6, 2, "骰子为: 6")

                row = store.get("SLTEST1")
                self.assertEqual(row["command"], "大10")
                self.assertEqual(row["bet_amount"], 10)
                self.assertEqual(row["estimated_balance_after_result"], "110")
                self.assertEqual(runner.current_turnover, Decimal("10"))
                self.assertTrue(runner.turnover_reached)
                self.assertTrue(runner.paused)
                report = (Path(directory) / "reports" / "SLTEST1.txt").read_text(
                    encoding="utf-8-sig"
                )
                self.assertIn("发送：大10", report)
                self.assertIn("结果：中了", report)
                self.assertIn("本局计入流水：10", report)
                self.assertIn("累计流水：10", report)
                self.assertIn("余额：110", report)
            finally:
                store.conn.close()

    async def test_instant_mode_sends_one_bet_then_native_dice_settles_it(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "instant.sqlite3")
            client = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(id=88))
            )
            runner = BigBetRunner(
                client=client,
                group=object(),
                group_id=-1888,
                bot_id=99,
                store=store,
                min_amount=10,
                max_amount=10,
                stop_turnover=Decimal("10"),
                start_balance=Decimal("100"),
                live=True,
                ack_timeout=2,
                report_dir=Path(directory) / "reports",
                game_mode="instant_dice",
            )
            runner.paused = True
            try:
                await runner.dashboard_action("resume", {})
                await runner.dashboard_action("resume", {})
                client.send_message.assert_awaited_once()
                row = store.recent(1)[0]
                self.assertEqual(row["game_mode"], "instant_dice")
                self.assertEqual(row["send_status"], "sent")
                self.assertEqual(runner.instant_active_issue, row["issue"])

                event = SimpleNamespace(
                    sender_id=99,
                    raw_text="",
                    message=SimpleNamespace(
                        id=89,
                        media=SimpleNamespace(value=6, emoticon="🎲"),
                        reply_to=SimpleNamespace(reply_to_msg_id=88),
                    ),
                )
                await runner.handle_message(event)
                await runner.handle_message(event)
                row = store.get(row["issue"])
                self.assertEqual(row["send_status"], "accepted")
                self.assertEqual(row["dice"], 6)
                self.assertEqual(row["outcome"], "win")
                self.assertEqual(store.total_turnover(True), Decimal("10"))
                self.assertTrue(runner.paused)
                self.assertIsNone(runner.instant_active_issue)
                client.send_message.assert_awaited_once()
            finally:
                for task in runner.timeout_tasks.values():
                    task.cancel()
                store.conn.close()


if __name__ == "__main__":
    unittest.main()
