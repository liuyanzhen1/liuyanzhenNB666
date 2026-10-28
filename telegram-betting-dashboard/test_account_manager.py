import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from account_manager import (
    AccountRegistry,
    parse_api_credentials,
    parse_api_fields,
    next_half_hour,
    protect_secret,
    unprotect_secret,
)
from multi_group import MultiGroupManager
from test_multi_group import FakeClient
from tg_big_bet import BigBetRunner, Store


class AccountHelpersTests(unittest.TestCase):
    def test_pasted_api_formats_and_encrypted_roundtrip(self):
        api_id, api_hash = parse_api_credentials(
            "App api_id: 12345678\nApp api_hash: 0123456789abcdef0123456789abcdef"
        )
        self.assertEqual(api_id, 12345678)
        self.assertEqual(api_hash, "0123456789abcdef0123456789abcdef")
        self.assertEqual(
            parse_api_credentials("12345678 0123456789abcdef0123456789abcdef"),
            (api_id, api_hash),
        )
        self.assertEqual(parse_api_fields("12345678", api_hash), (api_id, api_hash))
        for bad_id, bad_hash in (("App api_id: 123", api_hash), ("12345678", "short")):
            with self.assertRaises(ValueError):
                parse_api_fields(bad_id, bad_hash)
        with TemporaryDirectory() as directory:
            key = Path(directory) / "secret.key"
            protected = protect_secret(api_hash, key)
            self.assertNotIn(api_hash, protected)
            self.assertEqual(unprotect_secret(protected, key), api_hash)

    def test_next_half_hour_uses_clock_boundaries(self):
        zone = timezone.utc
        self.assertEqual(
            next_half_hour(datetime(2026, 10, 28, 12, 0, 1, tzinfo=zone)),
            datetime(2026, 10, 28, 12, 30, tzinfo=zone),
        )
        self.assertEqual(
            next_half_hour(datetime(2026, 10, 28, 12, 30, 0, tzinfo=zone)),
            datetime(2026, 10, 28, 12, 30, tzinfo=zone),
        )


class AccountRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_half_hour_report_uses_selected_username_and_contains_totals(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient()
            primary = BigBetRunner(
                client, client.groups[-1], -1, 99, Store(root / "primary.db"),
                min_amount=10, max_amount=10, stop_turnover=Decimal(100),
                start_balance=Decimal(500), live=False, ack_timeout=10,
                report_dir=root / "reports", group_name="汇报测试群",
            )
            manager = MultiGroupManager(client, primary, BigBetRunner, Store)
            registry = AccountRegistry(
                client, manager, BigBetRunner, Store,
                accounts_root=root / "accounts", live=False, ack_timeout=10,
                proxy=None, shared_api_id=12345678,
                shared_api_hash="0123456789abcdef0123456789abcdef",
                default_label="汇报账号", default_username="aghaststar",
                report_sender_username="@aghaststar", report_chat_selector="-3",
            )
            try:
                self.assertTrue(await registry.send_half_hour_report())
                sent_target, sent_text = client.send_message.await_args.args
                self.assertEqual(sent_target, client.groups[-3])
                self.assertIn("半小时整体数据汇报", sent_text)
                self.assertIn("已知群余额合计：500", sent_text)
                self.assertNotIn("各群明细", sent_text)
                self.assertNotIn("汇报测试群", sent_text)
                self.assertIsNotNone(registry.last_report_at)
                state = await registry.dashboard_action(
                    "save_report_settings",
                    {
                        "report_enabled": False,
                        "report_include_details": True,
                        "report_sender_username": "@aghaststar",
                        "report_chat_selector": "-3",
                    },
                )
                self.assertFalse(state["report_enabled"])
                self.assertTrue(state["report_include_details"])
                self.assertEqual(state["report_status"], "定时汇报已关闭")
                self.assertEqual(
                    manager.control_store.setting("half_hour_report_enabled"), "0"
                )
                self.assertEqual(
                    manager.control_store.setting("half_hour_report_include_details"), "1"
                )
                self.assertTrue(await registry.send_half_hour_report())
                _, detailed_text = client.send_message.await_args.args
                self.assertIn("各群明细", detailed_text)
                self.assertIn("汇报测试群", detailed_text)
            finally:
                await registry.close()

    async def test_default_account_wraps_existing_groups_and_empty_account_can_add_first_group(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient()
            primary = BigBetRunner(
                client, client.groups[-1], -1, 99, Store(root / "primary.db"),
                min_amount=10, max_amount=10, stop_turnover=Decimal(100),
                start_balance=None, live=False, ack_timeout=10,
                report_dir=root / "reports",
            )
            manager = MultiGroupManager(client, primary, BigBetRunner, Store)
            registry = AccountRegistry(
                client, manager, BigBetRunner, Store,
                accounts_root=root / "accounts", live=False, ack_timeout=10,
                proxy=None, shared_api_id=12345678,
                shared_api_hash="0123456789abcdef0123456789abcdef",
                default_label="默认测试账号",
            )
            try:
                state = await registry.dashboard_state()
                self.assertEqual(state["accounts"][0]["account_id"], "default")
                self.assertEqual(len(state["accounts"][0]["groups"]), 1)
                choices = await registry.dashboard_action(
                    "list_groups", {"account_id": "default"}
                )
                self.assertIsInstance(choices["groups"], list)
                self.assertEqual(len(choices["groups"]), 1)
                self.assertEqual(len(choices["available_groups"]), 3)
                await registry.dashboard_action(
                    "pause", {"account_id": "default", "group_id": -1}
                )
                self.assertTrue(primary.paused)

                empty = MultiGroupManager(
                    client, None, BigBetRunner, Store,
                    root_dir=root / "empty", live=False, ack_timeout=10,
                )
                self.assertEqual((await empty.dashboard_state())["groups"], [])
                await empty.dashboard_action("add_group", {
                    "group_id": -2, "bot_id": 99, "min_amount": 10,
                    "max_amount": 20, "target_turnover": "100",
                    "bet_sides": ["大"], "game_mode": "scheduled",
                    "instant_interval": 5,
                })
                self.assertIn(-2, empty.runners)
                await empty.close()
            finally:
                await registry.close()


if __name__ == "__main__":
    unittest.main()
