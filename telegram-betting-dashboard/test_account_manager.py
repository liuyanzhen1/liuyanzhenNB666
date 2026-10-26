import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from account_manager import (
    AccountRegistry,
    parse_api_credentials,
    parse_api_fields,
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


class AccountRegistryTests(unittest.IsolatedAsyncioTestCase):
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
