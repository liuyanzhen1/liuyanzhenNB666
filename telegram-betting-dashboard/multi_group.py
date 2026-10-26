"""One Telegram connection, isolated stores and controls for each group."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from telethon import utils
from telethon.errors import UsernameInvalidError, UsernameNotOccupiedError
from parsers import (
    normalize_bet_sides,
    normalize_game_mode,
    parse_open,
    parse_result,
    telegram_dice_value,
)


class MultiGroupManager:
    def __init__(
        self,
        client,
        primary,
        runner_factory,
        store_factory,
        *,
        root_dir: Path | None = None,
        live: bool | None = None,
        ack_timeout: int | None = None,
    ):
        self.client = client
        self.primary = primary
        self.runner_factory = runner_factory
        self.store_factory = store_factory
        if primary is None:
            if root_dir is None or live is None or ack_timeout is None:
                raise ValueError("空账号管理器缺少数据目录或运行参数")
            self.root_dir = Path(root_dir)
            self.root_dir.mkdir(parents=True, exist_ok=True)
            self.control_store = store_factory(self.root_dir / "account.sqlite3")
            self.owns_control_store = True
            self.live = bool(live)
            self.ack_timeout = int(ack_timeout)
        else:
            self.root_dir = primary.store.path.parent
            self.control_store = primary.store
            self.owns_control_store = False
            self.live = primary.live
            self.ack_timeout = primary.ack_timeout
        self.runners = {primary.group_id: primary} if primary is not None else {}
        self.errors = {}
        self.locks = {primary.group_id: asyncio.Lock()} if primary is not None else {}
        self.management_lock = asyncio.Lock()
        self.profiles = json.loads(self.control_store.setting("group_profiles_v1", "[]"))
        if primary is not None:
            self.restore_settings(primary)

    @staticmethod
    def restore_settings(runner):
        store = runner.store
        runner.min_amount = int(store.setting("min_amount", str(runner.min_amount)))
        runner.max_amount = int(store.setting("max_amount", str(runner.max_amount)))
        runner.paused = store.setting(f"paused_{int(runner.live)}", "0") == "1"
        runner.bet_sides = normalize_bet_sides(store.setting("bet_sides") or runner.bet_sides)
        runner.game_mode = normalize_game_mode(store.setting("game_mode") or runner.game_mode)
        runner.instant_interval = int(
            store.setting("instant_interval", str(runner.instant_interval))
        )
        if runner.game_mode == "instant_dice" and runner.instant_active_issue:
            runner.paused = True
            store.save_setting(f"paused_{int(runner.live)}", "1")
            store.log_event(
                "restart_pending",
                issue=runner.instant_active_issue,
                text="程序启动时发现上一单仍未开奖，已安全暂停，请先核对群消息",
            )
        runner.pause_if_turnover_reached()

    @staticmethod
    def save_settings(runner):
        values = {"min_amount": str(runner.min_amount), "max_amount": str(runner.max_amount),
                  "bet_sides": ",".join(runner.bet_sides),
                   "game_mode": runner.game_mode,
                   "instant_interval": str(runner.instant_interval),
                  f"paused_{int(runner.live)}": "1" if runner.paused else "0"}
        for key, value in values.items():
            if runner.store.setting(key) != value:
                runner.store.save_setting(key, value)

    async def load(self):
        for profile in self.profiles:
            gid = int(profile["group_id"])
            if self.primary is not None and gid == self.primary.group_id:
                continue
            try:
                group = await asyncio.wait_for(self.client.get_entity(gid), 20)
                self.create_runner(profile, group)
            except Exception as exc:
                self.errors[gid] = {**profile, "error": str(exc)}
                logging.exception("群 %s 加载失败，其他群继续运行", gid)

    def create_runner(self, profile, group):
        gid = int(profile["group_id"])
        folder = self.root_dir / "groups" / str(gid)
        store = self.store_factory(folder / "rounds.sqlite3")
        try:
            runner = self.runner_factory(
                self.client, group, gid, int(profile["bot_id"]), store,
                min_amount=int(profile["min_amount"]), max_amount=int(profile["max_amount"]),
                stop_turnover=Decimal(profile["target_turnover"]), start_balance=None,
                live=self.live, ack_timeout=self.ack_timeout,
                report_dir=folder / "每局结果", group_name=utils.get_display_name(group),
                bet_sides=profile.get("bet_sides", ["大"]),
                game_mode=profile.get("game_mode", "scheduled"),
                instant_interval=profile.get("instant_interval", 5),
            )
            # A new group never starts sending merely because it was added.
            if not store.setting(f"paused_{int(runner.live)}"):
                store.save_setting(f"paused_{int(runner.live)}", "1")
            self.restore_settings(runner)
        except Exception:
            store.conn.close()
            raise
        self.runners[gid] = runner
        self.locks[gid] = asyncio.Lock()
        self.errors.pop(gid, None)
        return runner

    async def handle_message(self, event):
        runner = self.runners.get(event.chat_id)
        if runner is None or event.sender_id != runner.bot_id:
            return
        async with self.locks[runner.group_id]:
            try:
                await runner.handle_message(event)
            except Exception as exc:
                runner.paused = True
                runner.store.log_event("group_error", text=str(exc))
                logging.exception("群 %s 处理失败，已暂停该群", runner.group_id)
            finally:
                self.save_settings(runner)

    async def start_running_instant_groups(self):
        for runner in self.runners.values():
            if runner.game_mode == "instant_dice" and not runner.paused:
                await runner.start_instant_round()
                self.save_settings(runner)

    async def dashboard_state(self):
        groups = []
        for runner in self.runners.values():
            state = await runner.dashboard_state()
            state["data_directory"] = str(runner.store.path.parent)
            groups.append(state)
        return {"groups": groups, "errors": list(self.errors.values()),
                "mode": "live" if self.live else "simulation"}

    async def available_groups(self):
        choices = []
        async for dialog in self.client.iter_dialogs():
            if dialog.is_group:
                choices.append({"group_id": utils.get_peer_id(dialog.entity),
                                "name": dialog.name, "entity": dialog.entity})
        return choices

    async def inspect_group(self, gid):
        choices = await self.available_groups()
        choice = next((item for item in choices if item["group_id"] == gid), None)
        if choice is None:
            raise ValueError("请选择当前 Telegram 账号已加入的群")
        candidates = {}
        async for message in self.client.iter_messages(choice["entity"], limit=150):
            text = message.raw_text or ""
            if message.sender_id and (
                parse_open(text) or parse_result(text) or telegram_dice_value(message) is not None
            ):
                sender = await message.get_sender()
                if sender and getattr(sender, "bot", False):
                    candidates[sender.id] = {"bot_id": sender.id,
                                             "username": getattr(sender, "username", None),
                                             "name": utils.get_display_name(sender)}
        return list(candidates.values())

    async def dashboard_action(self, action: str, payload: dict[str, Any]):
        if action == "list_groups":
            choices = await asyncio.wait_for(self.available_groups(), 25)
            return {**await self.dashboard_state(), "available_groups": [
                {k: v for k, v in item.items() if k != "entity"} for item in choices]}
        if action == "inspect_group":
            bots = await asyncio.wait_for(self.inspect_group(int(payload["group_id"])), 25)
            return {**await self.dashboard_state(), "bot_candidates": bots}
        if action in {"add_group", "retry_group"}:
            async with self.management_lock:
                await asyncio.wait_for(self.add_or_retry(action, payload), 25)
            return await self.dashboard_state()
        if action in {"pause_all", "stop"}:
            for runner in self.runners.values():
                runner.paused = True
                runner.cancel_scheduled_instant_round()
                self.save_settings(runner)
            if action == "stop":
                asyncio.get_running_loop().call_later(0.3, self.client.disconnect)
            return await self.dashboard_state()
        try:
            gid = int(payload.get("group_id", ""))
        except (ValueError, TypeError):
            raise ValueError("请先选择要操作的群") from None
        runner = self.runners.get(gid)
        if runner is None:
            raise ValueError("该群未加载，请先在群列表中重试")
        if action not in {"pause", "resume", "set_amount", "set_sides", "set_game_mode", "set_target", "reset_turnover", "export"}:
            raise ValueError("未知操作")
        # Pause takes effect immediately even while an already sent bet awaits its response.
        await runner.dashboard_action(action, payload)
        self.save_settings(runner)
        if action == "set_game_mode":
            profile = next(
                (item for item in self.profiles if int(item["group_id"]) == gid), None
            )
            if profile is not None:
                profile["game_mode"] = runner.game_mode
                profile["instant_interval"] = runner.instant_interval
                self.control_store.save_setting(
                    "group_profiles_v1",
                    json.dumps(self.profiles, ensure_ascii=False),
                )
        runner.store.log_event("control", text=f"网页操作：{action}")
        return await self.dashboard_state()

    async def add_or_retry(self, action, payload):
        try:
            gid = int(payload.get("group_id", ""))
        except (ValueError, TypeError):
            raise ValueError("群 ID 必须是数字") from None
        if gid in self.runners:
            raise ValueError("该群已添加，请直接在群列表中选择")
        if action == "retry_group":
            profile = next((p for p in self.profiles if int(p["group_id"]) == gid), None)
            if profile is None:
                raise ValueError("没有该群的保存配置")
            group = await self.client.get_entity(gid)
            self.create_runner(profile, group)
            return
        if any(int(p["group_id"]) == gid for p in self.profiles):
            raise ValueError("该群已有配置，请使用重试连接")
        try:
            minimum, maximum = int(payload["min_amount"]), int(payload["max_amount"])
            target = Decimal(str(payload["target_turnover"]))
            if not 0 < minimum <= maximum or not target.is_finite() or target <= 0:
                raise ValueError()
        except (ValueError, KeyError, TypeError, InvalidOperation):
            raise ValueError("请填写正整数金额范围和大于零的流水目标") from None
        try:
            bet_sides = normalize_bet_sides(payload.get("bet_sides", ["大"]))
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        try:
            game_mode = normalize_game_mode(payload.get("game_mode", "scheduled"))
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        try:
            instant_interval = int(payload.get("instant_interval", 5))
            if not 1 <= instant_interval <= 3600:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("即时模式间隔必须在 1 到 3600 秒之间") from None
        bot_input = str(payload.get("bot_id", "")).strip()
        if re.fullmatch(r"[0-9]+", bot_input) and int(bot_input) > 0:
            bot_selector = int(bot_input)
        elif re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{0,31}", bot_input):
            bot_selector = "@" + bot_input.lstrip("@")
        else:
            raise ValueError("请粘贴机器人的 @用户名，例如 @example_bot；原数字 ID 也可使用。")
        choices = await self.available_groups()
        choice = next((c for c in choices if c["group_id"] == gid), None)
        if choice is None:
            raise ValueError("该账号未加入此群，请刷新群列表")
        try:
            bot = await self.client.get_entity(bot_selector)
        except (ValueError, UsernameInvalidError, UsernameNotOccupiedError):
            raise ValueError("找不到这个机器人，请检查复制的是 @用户名，而不是显示昵称。") from None
        if not getattr(bot, "bot", False):
            raise ValueError("填写的账号不是 Telegram 机器人")
        bot_id = bot.id
        profile = {"group_id": gid, "group_name": choice["name"], "bot_id": bot_id,
                   "min_amount": minimum, "max_amount": maximum, "target_turnover": str(target),
                   "bet_sides": bet_sides, "game_mode": game_mode,
                   "instant_interval": instant_interval}
        runner = self.create_runner(profile, choice["entity"])
        try:
            self.control_store.save_setting("group_profiles_v1", json.dumps(
                [*self.profiles, profile], ensure_ascii=False))
        except Exception:
            del self.runners[gid]
            del self.locks[gid]
            runner.store.conn.close()
            raise
        self.profiles.append(profile)

    async def close(self):
        for runner in self.runners.values():
            self.save_settings(runner)
            tasks = list(runner.timeout_tasks.values())
            if runner.instant_next_task and not runner.instant_next_task.done():
                tasks.append(runner.instant_next_task)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        for runner in self.runners.values():
            runner.store.conn.close()
        if self.owns_control_store:
            self.control_store.conn.close()
