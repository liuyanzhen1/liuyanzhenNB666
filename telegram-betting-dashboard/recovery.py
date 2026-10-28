"""Per-group recovery without replaying historical open messages or bets."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from parsers import command_matches, parse_ack, parse_close, parse_open, parse_result, telegram_dice_value


def timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class RecoveryMixin:
    def initialize_recovery(self):
        self.run_requested = self.store.setting(
            f"run_requested_{int(self.live)}",
            "0" if self.store.setting(f"paused_{int(self.live)}", "0") == "1" else "1",
        ) == "1"
        self.pause_reason = self.store.setting(f"pause_reason_{int(self.live)}")
        self.last_bet_activity_at = time.monotonic()
        self.last_recovery_attempt_at = None
        self.last_watchdog_at = None
        self.watchdog_status = "每 3 分钟检查一次"
        self.recovery_count = int(self.store.setting(f"recovery_count_{int(self.live)}", "0"))
        self.next_instant_due_at = None
        self.recovery_lock = asyncio.Lock()
        self.private_message_allowed = None
        self.last_observed_open_message_id = 0
        self._reconciling = False

    def set_run_requested(self, requested: bool):
        self.run_requested = bool(requested)
        self.store.save_setting(f"run_requested_{int(self.live)}", "1" if requested else "0")
        if requested:
            self.last_bet_activity_at = time.monotonic()
        else:
            self.cancel_scheduled_instant_round()

    def automatic_pause(self, reason: str):
        self.paused = True
        self.pause_reason = reason
        self.store.save_setting(f"paused_{int(self.live)}", "1")
        self.store.save_setting(f"pause_reason_{int(self.live)}", reason)
        self.cancel_scheduled_instant_round()

    def uncertain_rows(self):
        return list(self.store.conn.execute(
            "SELECT * FROM rounds WHERE live_mode=? AND "
            "send_status IN ('sending','sent','ack_timeout','result_timeout','send_error') "
            "AND bet_amount IS NOT NULL", (int(self.live),)
        ))

    def unresolved_instant(self):
        return self.store.conn.execute(
            "SELECT * FROM rounds WHERE live_mode=? AND game_mode='instant_dice' "
            "AND dice IS NULL AND send_status IN "
            "('sending','sent','accepted','ack_timeout','result_timeout','send_error') "
            "ORDER BY rowid DESC LIMIT 1", (int(self.live),)
        ).fetchone()

    def abandon_unresolved_instant(self, reason: str):
        """Close one stalled instant order so the loop can continue safely."""
        row = self.unresolved_instant()
        if row is None:
            return None
        issue = row["issue"]
        task = self.timeout_tasks.pop(issue, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self.store.update(
            issue,
            send_status="abandoned",
            result_at=row["result_at"] or timestamp(),
            outcome="unknown",
            note=reason,
        )
        self.store.log_event("stalled_skipped", issue=issue, text=reason)
        if self.instant_active_issue == issue:
            self.instant_active_issue = None
        self.write_round_report(issue)
        return issue

    def unresolved_target_exposure(self):
        cutoff = int(self.store.setting(f"turnover_cutoff_{int(self.live)}", "0"))
        value = self.store.conn.execute(
            "SELECT COALESCE(SUM(bet_amount),0) FROM rounds WHERE live_mode=? "
            "AND rowid>? AND send_status IN "
            "('sending','sent','ack_timeout','result_timeout','send_error')",
            (int(self.live), cutoff),
        ).fetchone()[0]
        return Decimal(str(value))

    def recovery_blocker(self):
        if self.game_mode == "instant_dice" and self.unresolved_instant() is not None:
            return "上一笔即时订单仍未确认结果，继续回查"
        if self.live and self.store.cycle_turnover(self.live) + self.unresolved_target_exposure() >= self.stop_turnover:
            return "已确认流水加待确认金额可能达到目标，等待回执核对"
        for row in self.uncertain_rows():
            if row["game_mode"] != "scheduled":
                continue
            if row["dice"] is None and not row["closed_at"] and self.last_observed_open_message_id <= (row["open_message_id"] or 0):
                return "上一期尚无封盘或开奖信息，继续等待新消息"
        return ""

    def resume_after_recovery(self, force=False):
        if self._reconciling and not force:
            return False
        if not self.run_requested or self.pause_if_turnover_reached():
            return False
        if self.pause_reason in {"manual", "target", "simulation"}:
            return False
        blocker = self.recovery_blocker()
        if blocker:
            self.watchdog_status = blocker
            return False
        was_paused = self.paused
        self.paused = False
        self.pause_reason = ""
        self.store.save_setting(f"paused_{int(self.live)}", "0")
        self.store.save_setting(f"pause_reason_{int(self.live)}", "")
        if was_paused:
            self.watchdog_status = "已恢复，等待下一期" if self.game_mode == "scheduled" else "已恢复即时循环"
            self.store.log_event("ack_recovered", text=self.watchdog_status)
        self.schedule_next_instant_round()
        return True

    def row_matches_event_chat(self, row, event):
        if row["live_mode"] != int(self.live):
            return False
        expected_chat = self.bot_id if row["bet_delivery"] == "private_bot" else self.group_id
        return getattr(event, "chat_id", expected_chat) == expected_chat

    @staticmethod
    def message_after_send(row, event):
        sent_at = row["sent_at"] or row["opened_at"]
        date = getattr(event.message, "date", None)
        if not sent_at or not isinstance(date, datetime):
            return True
        sent = datetime.fromisoformat(sent_at)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.timestamp() >= sent.timestamp() - 2

    def match_ack_row(self, event, text, strict=False):
        reply_id = self._reply_message_id(event.message)
        if reply_id:
            row = self.store.by_sent_message(reply_id)
            return row if row is not None and self.row_matches_event_chat(row, event) and self.message_after_send(row, event) else None
        if strict:
            return None
        matches = [row for row in self.uncertain_rows()
                   if row["command"] and command_matches(text, row["command"])
                   and self.row_matches_event_chat(row, event) and self.message_after_send(row, event)]
        return matches[0] if len(matches) == 1 else None

    async def reconcile_recent_history(self):
        """Use only existing rounds and positively correlated replies; never replay opens."""
        if not self.live or not hasattr(self.client, "iter_messages"):
            return
        sources = [(self.group, self.group_id)]
        if self.bet_delivery == "private_bot" or any(r["bet_delivery"] == "private_bot" for r in self.uncertain_rows()):
            bot = await asyncio.wait_for(self.client.get_entity(self.bot_id), 10)
            sources.append((bot, self.bot_id))
        messages = []
        self._reconciling = True
        try:
            for entity, chat_id in sources:
                async def collect():
                    return [message async for message in self.client.iter_messages(entity, limit=100)]
                for message in await asyncio.wait_for(collect(), 12):
                    if getattr(message, "sender_id", None) == self.bot_id:
                        messages.append(SimpleNamespace(
                            message=message, sender_id=message.sender_id, chat_id=chat_id,
                            is_private=chat_id == self.bot_id,
                            raw_text=getattr(message, "raw_text", "") or "",
                        ))
            # Reconcile acknowledgments first so results can use the accepted amount.
            for event in messages:
                ack = parse_ack(event.raw_text)
                if ack and self.match_ack_row(event, event.raw_text, strict=True) is not None:
                    await self.handle_ack(event, ack, event.raw_text, strict=True)
            for event in messages:
                text = event.raw_text
                message_id = getattr(event.message, "id", 0) or 0
                if event.chat_id == self.group_id:
                    issue = parse_open(text)
                    if issue:
                        self.last_observed_open_message_id = max(self.last_observed_open_message_id, message_id)
                    closed = parse_close(text)
                    if closed and self.store.get(closed) is not None:
                        self.store.update(closed, closed_at=timestamp(), close_message_id=message_id)
                    result = parse_result(text)
                    if result and self.store.get(result[0]) is not None:
                        await self.handle_result(*result, message_id, text)
                dice = telegram_dice_value(event.message)
                reply = self._reply_message_id(event.message)
                if dice is not None and reply:
                    row = self.store.by_sent_message(reply)
                    if row is not None and self.row_matches_event_chat(row, event):
                        await self.handle_instant_dice(event, dice, strict=True)
        finally:
            self._reconciling = False

    async def recover_if_idle(self, now=None):
        now = time.monotonic() if now is None else now
        self.last_watchdog_at = timestamp()
        if not self.run_requested or self.pause_if_turnover_reached():
            self.watchdog_status = "已达流水目标" if self.turnover_reached else "手动暂停，跳过自动恢复"
            return False
        if now - self.last_bet_activity_at < 180:
            self.watchdog_status = "最近 3 分钟有下注活动"
            return False
        if self.next_instant_due_at is not None and now < self.next_instant_due_at + 30:
            self.watchdog_status = "正在等待设定的下一单间隔"
            return False
        if self.last_recovery_attempt_at is not None and now - self.last_recovery_attempt_at < 180:
            return False
        async with self.recovery_lock:
            if not self.run_requested:
                return False
            self.last_recovery_attempt_at = now
            self.watchdog_status = "正在刷新群连接并回查最近消息"
            self.store.log_event("watchdog_check", text="已启用且 3 分钟没有新下注，开始检查")
            self.automatic_pause("recovering")
            try:
                if self.client is not None and hasattr(self.client, "get_entity"):
                    self.group = await asyncio.wait_for(self.client.get_entity(self.group_id), 10)
                await asyncio.wait_for(self.reconcile_recent_history(), 30)
            except Exception as exc:
                self.watchdog_status = f"刷新失败，将在下次检查重试：{type(exc).__name__}"
                self.store.log_event("watchdog_error", text=self.watchdog_status)
                return False
            if not self.run_requested or self.pause_if_turnover_reached():
                return False
            if self.game_mode == "instant_dice" and self.unresolved_instant() is not None:
                issue = self.abandon_unresolved_instant(
                    "连续 3 分钟没有收到对应骰子，自动跳过此失联订单并继续下一单"
                )
                self.store.log_event(
                    "watchdog_skipped_stalled",
                    issue=issue,
                    text="已回查最近消息，仍无结果；自动继续即时循环",
                )
            blocker = self.recovery_blocker()
            if blocker:
                self.watchdog_status = blocker
                self.store.log_event("watchdog_wait", text=blocker)
                return False
            self.cancel_scheduled_instant_round()
            if not self.resume_after_recovery(force=True):
                return False
            self.recovery_count += 1
            self.store.save_setting(f"recovery_count_{int(self.live)}", str(self.recovery_count))
            self.store.log_event("watchdog_recovered", text=self.watchdog_status)
            return True
