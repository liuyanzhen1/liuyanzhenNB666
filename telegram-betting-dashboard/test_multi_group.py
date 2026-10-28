import asyncio
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.types import Chat, User
from multi_group import MultiGroupManager
from tg_big_bet import BigBetRunner, Store


class FakeClient:
    def __init__(self):
        self.groups = {gid: Chat(id=-gid, title=f"测试群 {-gid}", photo=None,
                                 participants_count=10, date=None, version=1)
                       for gid in (-1, -2, -3)}
        self.send_message = AsyncMock(return_value=SimpleNamespace(id=10))

    async def get_entity(self, key):
        if key in (99, "@example_bot"):
            return User(id=99, bot=True, first_name="测试机器人", username="example_bot")
        if key == "@person":
            return User(id=100, bot=False, first_name="普通账号")
        if key in self.groups:
            return self.groups[key]
        raise ValueError("不可访问")

    async def iter_dialogs(self):
        for gid, entity in self.groups.items():
            yield SimpleNamespace(entity=entity, name=entity.title, is_group=True)

    async def iter_messages(self, group, limit):
        yield SimpleNamespace(raw_text="期号: SL654928300500962\n发包金额: 1u\n倍数: 2.0",
                              sender_id=99, get_sender=AsyncMock(return_value=await self.get_entity(99)))


class MultiGroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = FakeClient()
        self.manager = self.make_manager()

    def make_manager(self):
        primary = BigBetRunner(self.client, self.client.groups[-1], -1, 99,
                               Store(self.root / "primary.db"), min_amount=10, max_amount=10,
                               stop_turnover=Decimal(100), start_balance=Decimal(100), live=False,
                               ack_timeout=10, report_dir=self.root / "每局结果")
        return MultiGroupManager(self.client, primary, BigBetRunner, Store)

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    async def add(self, gid=-2):
        return await self.manager.dashboard_action("add_group", {
            "group_id": gid, "bot_id": 99, "min_amount": 20,
            "max_amount": 20, "target_turnover": "200"})

    async def test_username_resolves_to_stable_id_and_survives_restart(self):
        for gid, value in ((-2, "  @example_bot  "), (-3, "example_bot")):
            await self.manager.dashboard_action("add_group", {"group_id": gid, "bot_id": value,
                "min_amount": 10, "max_amount": 20, "target_turnover": "100",
                "game_mode": "instant_dice" if gid == -2 else "scheduled",
                "instant_interval": 12 if gid == -2 else 5,
                "bet_delivery": "private_bot" if gid == -2 else "group"})
            self.assertEqual(self.manager.runners[gid].bot_id, 99)
            self.assertTrue(self.manager.runners[gid].paused)
        profiles = json.loads(self.manager.primary.store.setting("group_profiles_v1"))
        self.assertTrue(all(p["bot_id"] == 99 for p in profiles))
        await self.manager.close()
        self.manager = self.make_manager()
        await self.manager.load()
        self.assertEqual(self.manager.runners[-2].bot_id, 99)
        self.assertEqual(self.manager.runners[-2].game_mode, "instant_dice")
        self.assertEqual(self.manager.runners[-2].instant_interval, 12)
        self.assertEqual(self.manager.runners[-2].bet_delivery, "private_bot")
        self.client.send_message.assert_not_awaited()

    async def test_bad_username_and_non_bot_do_not_add_group(self):
        for value in ("", "机器人昵称", "@", "@missing_bot", "@person", "0", "-99"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    await self.manager.dashboard_action("add_group", {"group_id": -2, "bot_id": value,
                        "min_amount": 10, "max_amount": 20, "target_turnover": "100"})
                self.assertEqual(len(self.manager.runners), 1)
                self.assertEqual(self.manager.profiles, [])

    @staticmethod
    def event(gid, mid=1, sender=99):
        return SimpleNamespace(chat_id=gid, sender_id=sender,
                               raw_text="第SL123456789期 开始投注", message=SimpleNamespace(id=mid))

    async def test_independent_same_issue_pause_reset_export_and_restart(self):
        state = await self.add()
        self.assertTrue(state["groups"][1]["paused"])
        await self.manager.dashboard_action("resume", {"group_id": -2})
        one, two = self.manager.runners.values()
        await one.handle_open("SL1", 1, "")
        await two.handle_open("SL1", 1, "")
        await one.handle_result("SL1", 6, 2, "")
        await two.handle_result("SL1", 1, 2, "")
        self.assertEqual(one.store.get("SL1")["outcome"], "win")
        self.assertEqual(two.store.get("SL1")["outcome"], "loss")
        self.assertEqual(one.store.total_turnover(False), 10)
        self.assertEqual(two.store.total_turnover(False), 20)
        self.assertNotEqual(one.report_dir, two.report_dir)
        for runner in (one, two):
            self.assertTrue((runner.report_dir / "SL1.txt").exists())
            await self.manager.dashboard_action("export", {"group_id": runner.group_id})
            self.assertTrue((runner.store.path.parent / "rounds.csv").exists())
        await self.manager.dashboard_action("reset_turnover", {"group_id": -2, "target_turnover": "60"})
        self.assertFalse(one.paused)
        self.assertTrue(two.paused)
        self.assertEqual(one.store.cycle_turnover(False), 10)
        self.assertEqual(two.store.cycle_turnover(False), 0)
        await self.manager.dashboard_action("set_amount", {"group_id": -2, "min_amount": 30, "max_amount": 35})
        await self.manager.dashboard_action("set_sides", {"group_id": -2, "bet_sides": ["小", "双"]})
        self.assertEqual(one.bet_sides, ["大"])
        self.assertEqual(two.bet_sides, ["小", "双"])
        await self.manager.close()
        self.manager = self.make_manager()
        await self.manager.load()
        restored = self.manager.runners[-2]
        self.assertEqual((restored.min_amount, restored.max_amount), (30, 35))
        self.assertEqual(restored.bet_sides, ["小", "双"])
        self.assertEqual(restored.stop_turnover, 60)
        self.assertTrue(restored.paused)
        self.assertIsNotNone(restored.store.get("SL1"))
        await self.manager.dashboard_action("pause_all", {})
        self.assertTrue(all(r.paused for r in self.manager.runners.values()))

    async def test_watchdog_checks_and_recovers_every_running_group(self):
        await self.add()
        await self.manager.dashboard_action("resume", {"group_id": -2})
        for runner in self.manager.runners.values():
            runner.set_run_requested(True)
            runner.automatic_pause("group_error")
            runner.last_bet_activity_at = 0
        results = await self.manager.watchdog_once(now=181)
        self.assertEqual(results, [True, True])
        self.assertTrue(all(not runner.paused for runner in self.manager.runners.values()))
        self.assertTrue(all(runner.recovery_count == 1 for runner in self.manager.runners.values()))

    async def test_routing_ignores_other_groups_and_senders(self):
        await self.add()
        one, two = self.manager.runners.values()
        one.handle_message = AsyncMock()
        two.handle_message = AsyncMock()
        await self.manager.handle_message(self.event(-1))
        one.handle_message.assert_awaited_once()
        two.handle_message.assert_not_awaited()
        await self.manager.handle_message(self.event(-999))
        two.handle_message.assert_not_awaited()
        # Exercise the actual sender filter separately.
        await BigBetRunner.handle_message(two, self.event(-2, sender=100))
        self.assertEqual(two.store.all_rounds(), [])

    async def test_private_bot_messages_route_to_group_configured_for_private_bets(self):
        await self.manager.dashboard_action("add_group", {
            "group_id": -2, "bot_id": 99, "min_amount": 20,
            "max_amount": 20, "target_turnover": "200",
            "bet_delivery": "private_bot",
        })
        one, two = self.manager.runners.values()
        one.handle_message = AsyncMock()
        two.handle_message = AsyncMock()
        private_event = SimpleNamespace(
            chat_id=99,
            sender_id=99,
            is_private=True,
            raw_text="下注成功 余额: 90",
            message=SimpleNamespace(id=31, reply_to=None),
        )
        await self.manager.handle_message(private_event)
        one.handle_message.assert_not_awaited()
        two.handle_message.assert_awaited_once_with(private_event)

        two.handle_message.reset_mock()
        await self.manager.handle_message(self.event(-2))
        two.handle_message.assert_awaited_once()

        await self.manager.dashboard_action("set_delivery", {
            "group_id": -2, "bet_delivery": "group"
        })
        self.assertEqual(two.bet_delivery, "group")
        profiles = json.loads(self.manager.primary.store.setting("group_profiles_v1"))
        self.assertEqual(profiles[0]["bet_delivery"], "group")

    async def test_discovery_and_real_routing_with_no_network(self):
        state = await self.manager.dashboard_action("list_groups", {})
        self.assertEqual(len(state["available_groups"]), 3)
        state = await self.manager.dashboard_action("inspect_group", {"group_id": -2})
        self.assertEqual(state["bot_candidates"][0]["bot_id"], 99)
        await self.add()
        await self.manager.dashboard_action("resume", {"group_id": -2})
        for gid in (-1, -2):
            event = self.event(gid)
            event.raw_text = "期号: SL654928300500962\n发包金额: 1u\n倍数: 2.0"
            await self.manager.handle_message(event)
            await self.manager.handle_message(event)
        self.assertEqual(self.manager.runners[-1].store.total_turnover(False), 10)
        self.assertEqual(self.manager.runners[-2].store.total_turnover(False), 20)
        self.client.send_message.assert_not_awaited()

    async def test_error_in_one_group_does_not_stop_other_groups(self):
        await self.add()
        self.manager.runners[-1].handle_message = AsyncMock(side_effect=RuntimeError("测试异常"))
        self.manager.runners[-2].handle_message = AsyncMock()
        await self.manager.handle_message(self.event(-1))
        await self.manager.handle_message(self.event(-2))
        self.assertTrue(self.manager.runners[-1].paused)
        self.manager.runners[-2].handle_message.assert_awaited_once()

    async def test_groups_process_concurrently_but_each_group_is_serial(self):
        await self.add()
        release, started = asyncio.Event(), asyncio.Event()
        async def slow(event):
            started.set()
            await release.wait()
        self.manager.runners[-1].handle_message = slow
        self.manager.runners[-2].handle_message = AsyncMock()
        task = asyncio.create_task(self.manager.handle_message(self.event(-1)))
        await started.wait()
        await asyncio.wait_for(self.manager.handle_message(self.event(-2)), 1)
        self.manager.runners[-2].handle_message.assert_awaited_once()
        release.set()
        await task

    async def test_invalid_duplicate_and_missing_group_rejected(self):
        await self.add()
        with self.assertRaises(ValueError):
            await self.add()
        with self.assertRaises(ValueError):
            await self.manager.dashboard_action("pause", {})
        with self.assertRaises(ValueError):
            await self.manager.dashboard_action("resume", {"group_id": -999})
        with self.assertRaises(ValueError):
            await self.manager.dashboard_action("add_group", {"group_id": -3, "bot_id": 99,
                "min_amount": 30, "max_amount": 10, "target_turnover": "100"})
        self.assertEqual(len(self.manager.runners), 2)
        self.client.send_message.assert_not_awaited()

    async def test_remove_group_stops_it_archives_data_and_prevents_restart(self):
        await self.add()
        runner = self.manager.runners[-2]
        await runner.handle_open("SL_REMOVE", 1, "")
        original_folder = runner.store.path.parent
        self.assertTrue(original_folder.exists())

        state = await self.manager.dashboard_action(
            "remove_group", {"group_id": -2}
        )
        self.assertNotIn(-2, self.manager.runners)
        self.assertEqual(self.manager.profiles, [])
        self.assertEqual(state["removed_group_id"], -2)
        archived = Path(state["archived_group_path"])
        self.assertTrue(archived.exists())
        self.assertTrue((archived / "rounds.sqlite3").exists())
        self.assertFalse(original_folder.exists())

        default_state = await self.manager.dashboard_action(
            "remove_group", {"group_id": -1}
        )
        self.assertEqual(default_state["groups"], [])
        default_archive = Path(default_state["archived_group_path"])
        self.assertTrue((default_archive / "rounds.sqlite3").exists())

        await self.manager.close()
        self.manager = self.make_manager()
        await self.manager.load()
        self.assertEqual(self.manager.runners, {})

    async def test_unavailable_saved_group_is_reported_and_can_retry(self):
        await self.add()
        await self.manager.close()
        group = self.client.groups.pop(-2)
        self.manager = self.make_manager()
        await self.manager.load()
        state = await self.manager.dashboard_state()
        self.assertEqual(len(state["groups"]), 1)
        self.assertEqual(state["errors"][0]["group_id"], -2)
        self.client.groups[-2] = group
        await self.manager.dashboard_action("retry_group", {"group_id": -2})
        self.assertEqual(len(self.manager.runners), 2)
        self.assertFalse(self.manager.errors)


if __name__ == "__main__":
    unittest.main()
