import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from tg_big_bet import BigBetRunner, Store


class TurnoverResetTests(unittest.IsolatedAsyncioTestCase):
    def runner(self, store, directory):
        return BigBetRunner(None, None, 1, 2, store, min_amount=10, max_amount=10,
                            stop_turnover=Decimal(100), start_balance=None,
                            live=False, ack_timeout=10, report_dir=Path(directory) / "reports")

    async def test_reset_restart_and_new_target_stop_without_deleting_history(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            store = Store(path)
            try:
                runner = self.runner(store, directory)
                await runner.handle_open("SL1", 1, "")
                self.assertEqual(store.cycle_turnover(False), 10)
                state = await runner.dashboard_action("reset_turnover", {"target_turnover": "20"})
                self.assertEqual(state["turnover"], "0")
                self.assertEqual(state["lifetime_turnover"], "10")
                self.assertTrue(state["paused"])
                self.assertIsNotNone(store.get("SL1"))
                self.assertEqual(store.total_turnover(False), 10)
            finally:
                store.conn.close()
            store = Store(path)
            try:
                runner = self.runner(store, directory)
                self.assertEqual(runner.current_turnover, 0)
                self.assertEqual(runner.stop_turnover, 20)
                await runner.handle_open("SL2", 2, "")
                self.assertFalse(runner.turnover_reached)
                await runner.handle_open("SL3", 3, "")
                self.assertTrue(runner.turnover_reached)
                self.assertEqual(store.total_turnover(False), 30)
                await runner.handle_open("SL4", 4, "")
                self.assertEqual(store.get("SL4")["send_status"], "skipped_target")
                with self.assertRaises(ValueError):
                    await runner.dashboard_action("resume", {})
                with self.assertRaises(ValueError):
                    await runner.dashboard_action("reset_turnover", {"target_turnover": "0"})
                self.assertEqual(store.cycle_turnover(False), 20)
            finally:
                store.conn.close()

    def test_late_ack_and_other_mode_do_not_leak_into_reset_cycle(self):
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.db")
            try:
                store.ensure_round("OLD", live_mode=1, bet_amount=25, send_status="sent")
                store.ensure_round("SIM", live_mode=0, bet_amount=30, send_status="dry_run")
                store.reset_turnover(True, Decimal(50))
                store.update("OLD", send_status="accepted")
                self.assertEqual(store.total_turnover(True), 25)
                self.assertEqual(store.cycle_turnover(True), 0)
                self.assertEqual(store.cycle_turnover(False), 30)
                store.ensure_round("NEW", live_mode=1, bet_amount=15, send_status="accepted")
                self.assertEqual(store.cycle_turnover(True), 15)
                store.reset_turnover(True, Decimal(100))
                self.assertEqual(store.cycle_turnover(True), 0)
                self.assertEqual(len(store.all_rounds()), 3)
            finally:
                store.conn.close()
