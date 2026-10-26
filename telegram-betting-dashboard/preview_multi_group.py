"""Offline UI fixture. Never logs into Telegram or sends messages."""
import asyncio
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from account_manager import AccountContext, AccountRegistry
from dashboard import DashboardServer
from multi_group import MultiGroupManager
from tg_big_bet import BigBetRunner, Store
from test_multi_group import FakeClient


async def main():
    with TemporaryDirectory() as folder:
        root = Path(folder)
        client = FakeClient()
        primary = BigBetRunner(client, client.groups[-1], -1, 99, Store(root / "primary.db"),
            min_amount=10, max_amount=20, stop_turnover=Decimal(1000), start_balance=Decimal(500),
            live=False, ack_timeout=10, report_dir=root / "每局结果", group_name="离线演示 · 一号群")
        await primary.dashboard_action("set_sides", {"bet_sides": ["大", "单"]})
        manager = MultiGroupManager(client, primary, BigBetRunner, Store)
        await manager.dashboard_action("add_group", {"group_id": -2, "bot_id": 99,
            "min_amount": 20, "max_amount": 30, "target_turnover": "2000",
            "bet_sides": ["小", "双"], "game_mode": "instant_dice",
            "instant_interval": 12})
        await primary.handle_open("SL654928300501070", 1, "")
        await primary.handle_result("SL654928300501070", 5, 2, "")
        accounts = AccountRegistry(client, manager, BigBetRunner, Store,
            accounts_root=root / "accounts", live=False, ack_timeout=10, proxy=None,
            shared_api_id=12345678,
            shared_api_hash="0123456789abcdef0123456789abcdef",
            default_label="演示账号 A", default_username="demo_a", default_user_id=1001)
        empty_manager = MultiGroupManager(client, None, BigBetRunner, Store,
            root_dir=root / "account-b", live=False, ack_timeout=10)
        accounts.accounts["demo_b"] = AccountContext(
            account_id="demo_b", label="演示账号 B", display_name="第二个账号",
            username="demo_b", phone_mask="***8899", telegram_user_id=1002,
            client=None, manager=empty_manager, can_delete=True)
        server = DashboardServer(accounts, asyncio.get_running_loop(), Path(__file__).parent / "web", port=8876)
        print(server.start(), flush=True)
        try:
            await asyncio.sleep(1800)
        finally:
            server.stop()
            await accounts.close()


if __name__ == "__main__":
    asyncio.run(main())
