from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import random
import socket
import sqlite3
import sys
import threading
import webbrowser
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events, utils

from account_manager import AccountRegistry
from dashboard import DashboardServer
from multi_group import MultiGroupManager
from parsers import (
    is_big,
    normalize_bet_sides,
    normalize_game_mode,
    parse_ack,
    parse_close,
    parse_open,
    parse_result,
    result_sides,
    telegram_dice_value,
)


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ENV_FILE = ROOT / ".env"
DEFAULT_DB = DATA_DIR / "tg_big_bet.sqlite3"
DEFAULT_SESSION = DATA_DIR / "tg_big_bet"
LOG_FILE = DATA_DIR / "tg_big_bet.log"
ROUND_REPORT_DIR = DATA_DIR / "每局结果"
WEB_DIR = ROOT / "web"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def positive_int(value: str) -> int:
    try:
        result = int(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("金额必须是整数") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("金额必须大于 0")
    return result


def positive_decimal(value: str) -> Decimal:
    try:
        result = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise argparse.ArgumentTypeError("余额必须是数字") from exc
    if not result.is_finite() or result <= 0:
        raise argparse.ArgumentTypeError("余额必须大于 0")
    return result


def instant_interval_seconds(value: Any) -> int:
    try:
        result = int(str(value).strip())
    except (ValueError, TypeError) as exc:
        raise ValueError("即时模式间隔必须是整数秒") from exc
    if not 1 <= result <= 3600:
        raise ValueError("即时模式间隔必须在 1 到 3600 秒之间")
    return result


def random_bet_amount(min_amount: int, max_amount: int) -> int:
    if min_amount <= 0 or max_amount <= 0:
        raise ValueError("随机金额范围必须大于 0")
    if min_amount > max_amount:
        raise ValueError("最小金额不能大于最大金额")
    return random.SystemRandom().randint(min_amount, max_amount)


@dataclass
class Config:
    api_id: int
    api_hash: str
    group_selector: str
    bot_id: int | None
    ack_timeout: int
    live: bool
    amount: int | None
    min_amount: int | None
    max_amount: int | None
    stop_turnover: Decimal | None
    start_balance: Decimal | None
    proxy: tuple[str, str, int, bool, str | None, str | None] | None
    proxy_label: str | None
    dashboard_port: int
    open_browser: bool
    db_path: Path
    session_path: Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rounds (
                issue TEXT PRIMARY KEY,
                game_mode TEXT NOT NULL DEFAULT 'scheduled',
                opened_at TEXT,
                open_message_id INTEGER,
                bet_side TEXT NOT NULL DEFAULT '大',
                unit_amount INTEGER,
                bet_amount INTEGER,
                command TEXT,
                live_mode INTEGER NOT NULL DEFAULT 0,
                send_status TEXT NOT NULL DEFAULT 'new',
                sent_message_id INTEGER UNIQUE,
                sent_at TEXT,
                ack_message_id INTEGER,
                ack_at TEXT,
                balance_after_ack TEXT,
                frozen TEXT,
                closed_at TEXT,
                close_message_id INTEGER,
                result_message_id INTEGER,
                result_at TEXT,
                dice INTEGER,
                result_side TEXT,
                outcome TEXT,
                net TEXT,
                turnover_after_bet TEXT,
                estimated_balance_after_result TEXT,
                raw_open TEXT,
                raw_ack TEXT,
                raw_result TEXT,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue TEXT,
                event_type TEXT NOT NULL,
                message_id INTEGER,
                sender_id INTEGER,
                created_at TEXT NOT NULL,
                text TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_event_issue ON event_log(issue);
            CREATE INDEX IF NOT EXISTS idx_round_status ON rounds(send_status);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(rounds)").fetchall()
        }
        if "turnover_after_bet" not in columns:
            self.conn.execute("ALTER TABLE rounds ADD COLUMN turnover_after_bet TEXT")
        if "unit_amount" not in columns:
            self.conn.execute("ALTER TABLE rounds ADD COLUMN unit_amount INTEGER")
        if "game_mode" not in columns:
            self.conn.execute(
                "ALTER TABLE rounds ADD COLUMN game_mode TEXT NOT NULL DEFAULT 'scheduled'"
            )
        self.conn.execute("UPDATE rounds SET unit_amount=bet_amount WHERE unit_amount IS NULL AND bet_amount IS NOT NULL")
        self.conn.commit()
        self.backfill_turnover()

    def log_event(
        self,
        event_type: str,
        *,
        issue: str | None = None,
        message_id: int | None = None,
        sender_id: int | None = None,
        text: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO event_log(issue,event_type,message_id,sender_id,created_at,text) VALUES(?,?,?,?,?,?)",
            (issue, event_type, message_id, sender_id, now_iso(), text),
        )
        self.conn.commit()

    def ensure_round(self, issue: str, **values: Any) -> bool:
        columns = ["issue", *values.keys()]
        placeholders = ",".join("?" for _ in columns)
        cursor = self.conn.execute(
            f"INSERT OR IGNORE INTO rounds({','.join(columns)}) VALUES({placeholders})",
            [issue, *values.values()],
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def update(self, issue: str, **values: Any) -> None:
        if not values:
            return
        assignments = ",".join(f"{key}=?" for key in values)
        self.conn.execute(
            f"UPDATE rounds SET {assignments} WHERE issue=?",
            [*values.values(), issue],
        )
        self.conn.commit()

    def get(self, issue: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM rounds WHERE issue=?", (issue,)).fetchone()

    def by_sent_message(self, message_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM rounds WHERE sent_message_id=?", (message_id,)
        ).fetchone()

    def latest_pending(self, command: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM rounds
            WHERE command=? AND send_status IN ('sending','sent','ack_timeout')
            ORDER BY sent_at DESC LIMIT 1
            """,
            (command,),
        ).fetchone()

    def latest_unsettled_instant(self) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM rounds
            WHERE game_mode='instant_dice'
              AND result_at IS NULL
              AND send_status IN ('sending','sent','accepted')
            ORDER BY COALESCE(sent_at,opened_at) DESC LIMIT 1
            """
        ).fetchone()

    def recent(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM rounds ORDER BY COALESCE(opened_at,result_at) DESC LIMIT ?",
                (limit,),
            )
        )

    def all_settled(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM rounds WHERE outcome IN ('win','loss','tie') ORDER BY result_at"
            )
        )

    def total_turnover(self, live: bool) -> Decimal:
        expected_status = "accepted" if live else "dry_run"
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(bet_amount), 0)
            FROM rounds
            WHERE live_mode=? AND send_status=? AND bet_amount IS NOT NULL
            """,
            (int(live), expected_status),
        ).fetchone()
        return Decimal(str(row[0] if row is not None else 0))

    def setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def save_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def cycle_turnover(self, live: bool) -> Decimal:
        cutoff = int(self.setting(f"turnover_cutoff_{int(live)}", "0"))
        row = self.conn.execute(
            "SELECT COALESCE(SUM(bet_amount),0) FROM rounds "
            "WHERE live_mode=? AND send_status=? AND rowid>?",
            (int(live), "accepted" if live else "dry_run", cutoff),
        ).fetchone()
        return Decimal(str(row[0]))

    def reset_turnover(self, live: bool, target: Decimal) -> None:
        # Keep historical rounds intact. Late acknowledgments for earlier rounds
        # remain in lifetime totals, without being charged to the new cycle.
        with self.conn:
            cutoff = self.conn.execute("SELECT COALESCE(MAX(rowid),0) FROM rounds").fetchone()[0]
            for key, value in {
                f"turnover_cutoff_{int(live)}": str(cutoff),
                f"turnover_reset_at_{int(live)}": now_iso(),
                f"turnover_target_{int(live)}": str(target),
            }.items():
                self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
            self.conn.execute(
                "INSERT INTO event_log(event_type,created_at,text) VALUES (?,?,?)",
                ("turnover_reset", now_iso(), f"{'真实' if live else '模拟'}本轮流水归零，目标 {target}，历史保留"),
            )

    def backfill_turnover(self) -> None:
        for live_mode, counted_status in ((0, "dry_run"), (1, "accepted")):
            total = Decimal(0)
            rows = self.conn.execute(
                """
                SELECT issue, send_status, bet_amount
                FROM rounds
                WHERE live_mode=?
                ORDER BY COALESCE(opened_at, sent_at, ack_at, result_at, issue), issue
                """,
                (live_mode,),
            ).fetchall()
            for row in rows:
                if row["send_status"] == counted_status and row["bet_amount"] is not None:
                    total += Decimal(row["bet_amount"])
                self.conn.execute(
                    "UPDATE rounds SET turnover_after_bet=? WHERE issue=?",
                    (str(total), row["issue"]),
                )
        self.conn.commit()

    def all_rounds(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM rounds ORDER BY COALESCE(opened_at,result_at), issue"
            )
        )

    def recent_events(self, limit: int = 80) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT issue,event_type,message_id,sender_id,created_at,text
                FROM event_log ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            )
        )

    def recent_for_mode(self, live: bool, limit: int = 60) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM rounds WHERE live_mode=?
                ORDER BY COALESCE(opened_at,result_at) DESC LIMIT ?
                """,
                (int(live), limit),
            )
        )

    def settled_for_mode(self, live: bool) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM rounds
                WHERE live_mode=? AND outcome IN ('win','loss','tie')
                ORDER BY result_at
                """,
                (int(live),),
            )
        )

    def export_csv(self, target: Path) -> None:
        rows = list(self.conn.execute("SELECT * FROM rounds ORDER BY COALESCE(opened_at,result_at)"))
        if not rows:
            raise RuntimeError("还没有可导出的记录")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(rows[0].keys())
            writer.writerows(tuple(row) for row in rows)


class BigBetRunner:
    def __init__(
        self,
        client: TelegramClient,
        group: Any,
        group_id: int,
        bot_id: int,
        store: Store,
        *,
        min_amount: int,
        max_amount: int,
        stop_turnover: Decimal,
        start_balance: Decimal | None,
        live: bool,
        ack_timeout: int,
        report_dir: Path = ROUND_REPORT_DIR,
        group_name: str | None = None,
        bet_sides: Any = None,
        game_mode: str | None = None,
        instant_interval: int | None = None,
    ):
        self.client = client
        self.group = group
        self.group_id = group_id
        self.bot_id = bot_id
        self.store = store
        self.min_amount = min_amount
        self.max_amount = max_amount
        self.stop_turnover = Decimal(store.setting(f"turnover_target_{int(live)}", str(stop_turnover)))
        self.current_balance = start_balance
        self.current_turnover = store.cycle_turnover(live)
        self.live = live
        self.ack_timeout = ack_timeout
        self.report_dir = report_dir
        self.group_name = group_name or str(group_id)
        saved_sides = store.setting("bet_sides")
        self.bet_sides = normalize_bet_sides(saved_sides or bet_sides or ["大"])
        self.game_mode = normalize_game_mode(
            store.setting("game_mode") or game_mode or "scheduled"
        )
        self.instant_interval = instant_interval_seconds(
            store.setting("instant_interval") or instant_interval or 5
        )
        self.paused = False
        self.turnover_reached = False
        self.lock = asyncio.Lock()
        self.timeout_tasks: dict[str, asyncio.Task[None]] = {}
        self.instant_next_task: asyncio.Task[None] | None = None
        self.instant_result_timeout = max(30, self.ack_timeout * 3)
        pending = self.store.latest_unsettled_instant()
        self.instant_active_issue = pending["issue"] if pending else None
        for existing_round in self.store.all_rounds():
            self.write_round_report(existing_round["issue"])

    def write_round_report(self, issue: str) -> Path | None:
        row = self.store.get(issue)
        if row is None:
            return None

        status_labels = {
            "new": "等待处理",
            "sending": "正在发送",
            "sent": "已发送，等待机器人确认",
            "accepted": "下注成功",
            "rejected": "下注未接受",
            "ack_timeout": "回执超时",
            "result_timeout": "开奖超时",
            "send_error": "发送失败",
            "dry_run": "模拟下注",
            "skipped_paused": "暂停，未下注",
            "skipped_target": "已达流水目标，未下注",
        }
        outcome_labels = {
            "win": "中了",
            "loss": "未中",
            "tie": "盈亏持平",
            "unknown": "结果无法确认",
            "not_bet": "未下注",
        }
        counted = row["send_status"] in {"accepted", "dry_run"}
        round_turnover = row["bet_amount"] if counted else 0
        balance = (
            row["estimated_balance_after_result"]
            or row["balance_after_ack"]
            or "未知"
        )
        result = outcome_labels.get(row["outcome"], "待开奖")
        dice = row["dice"] if row["dice"] is not None else "待开奖"
        turnover = row["turnover_after_bet"] or "待确认"
        lines = [
            f"群：{self.group_name} ({self.group_id})",
            f"开奖方式：{'下注后掷骰' if row['game_mode'] == 'instant_dice' else '定时开盘'}",
            f"期号：{row['issue']}",
            f"发送：{row['command'] or '未发送'}",
            f"下注选项：{row['bet_side'] or '未知'}",
            f"单项金额：{row['unit_amount'] if row['unit_amount'] is not None else '未知'}",
            f"结果：{result}",
            f"本局计入流水：{round_turnover}",
            f"累计流水：{turnover}",
            f"余额：{balance}",
            f"骰子：{dice}",
            f"开奖属性：{row['result_side'] or '待开奖'}",
            f"下注状态：{status_labels.get(row['send_status'], row['send_status'])}",
            f"本局净额：{row['net'] if row['net'] is not None else '待结算'}",
            f"开盘时间：{row['opened_at'] or '未知'}",
            f"下注确认时间：{row['ack_at'] or '未知'}",
            f"开奖时间：{row['result_at'] or '未知'}",
            f"更新时间：{now_iso()}",
        ]
        safe_issue = "".join(
            character for character in issue if character.isalnum() or character in "-_"
        )
        if not safe_issue:
            return None
        self.report_dir.mkdir(parents=True, exist_ok=True)
        target = self.report_dir / f"{safe_issue}.txt"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        return target

    def track_timeout(self, issue: str, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.timeout_tasks[issue] = task

        def forget(completed: asyncio.Task[None]) -> None:
            if self.timeout_tasks.get(issue) is completed:
                self.timeout_tasks.pop(issue, None)

        task.add_done_callback(forget)

    async def handle_message(self, event: events.NewMessage.Event) -> None:
        if event.sender_id != self.bot_id:
            return
        text = event.raw_text or ""
        message_id = event.message.id

        if self.game_mode == "instant_dice":
            dice = telegram_dice_value(event.message)
            if dice is not None:
                await self.handle_instant_dice(event, dice)
                return

            ack = parse_ack(text)
            if ack:
                await self.handle_ack(event, ack, text)
            return

        result = parse_result(text)
        if result:
            issue, dice = result
            await self.handle_result(issue, dice, message_id, text)
            return

        close_issue = parse_close(text)
        if close_issue:
            self.store.ensure_round(close_issue)
            self.store.update(close_issue, closed_at=now_iso(), close_message_id=message_id)
            self.store.log_event(
                "closed", issue=close_issue, message_id=message_id, sender_id=event.sender_id
            )
            self.write_round_report(close_issue)
            print(f"\n[封盘] {close_issue}")
            return

        ack = parse_ack(text)
        if ack:
            await self.handle_ack(event, ack, text)
            return

        issue = parse_open(text)
        if issue:
            await self.handle_open(issue, message_id, text)

    async def handle_open(self, issue: str, message_id: int, text: str) -> None:
        async with self.lock:
            inserted = self.store.ensure_round(
                issue,
                game_mode="scheduled",
                opened_at=now_iso(),
                open_message_id=message_id,
                raw_open=f"期号:{issue}",
                bet_side=",".join(self.bet_sides),
            )
            self.store.log_event(
                "open",
                issue=issue,
                message_id=message_id,
                sender_id=self.bot_id,
                text=f"期号:{issue}",
            )
            if not inserted:
                print(f"\n[忽略重复期号] {issue}")
                return

            unit_amount = random_bet_amount(self.min_amount, self.max_amount)
            amount = unit_amount * len(self.bet_sides)
            command = " ".join(f"{side}{unit_amount}" for side in self.bet_sides)
            self.store.update(
                issue,
                unit_amount=unit_amount,
                bet_amount=amount,
                command=command,
                live_mode=int(self.live),
                turnover_after_bet=str(self.store.total_turnover(self.live)),
            )
            if self.turnover_reached:
                self.store.update(
                    issue,
                    send_status="skipped_target",
                    note=f"累计流水已达到停止目标 {self.stop_turnover}",
                )
                self.write_round_report(issue)
                print(f"\n[达到目标未下注] {issue}，计划 {command}")
                return
            if self.paused:
                self.store.update(issue, send_status="skipped_paused", note="程序处于暂停状态")
                self.write_round_report(issue)
                print(f"\n[暂停未下注] {issue}，计划 {command}")
                return

            if not self.live:
                self.store.update(issue, send_status="dry_run")
                print(f"\n[模拟] {issue} 将发送：{command}")
                self.pause_if_turnover_reached()
                self.store.update(issue, turnover_after_bet=str(self.store.total_turnover(self.live)))
                self.write_round_report(issue)
                return

            self.store.update(issue, send_status="sending")
            try:
                sent = await self.client.send_message(self.group, command)
            except Exception as exc:
                self.paused = True
                self.store.update(issue, send_status="send_error", note=repr(exc))
                self.store.log_event("send_error", issue=issue, text=repr(exc))
                self.write_round_report(issue)
                logging.exception("发送失败，程序已暂停")
                print(f"\n[发送失败并暂停] {issue}: {exc}")
                return

            self.store.update(
                issue,
                send_status="sent",
                sent_message_id=sent.id,
                sent_at=now_iso(),
            )
            self.store.log_event(
                "sent", issue=issue, message_id=sent.id, text=command
            )
            self.write_round_report(issue)
            print(f"\n[已发送] {issue}: {command}，等待机器人确认")
            self.track_timeout(issue, self.watch_ack_timeout(issue))

    def _new_instant_issue(self) -> str:
        try:
            sequence = int(self.store.setting("instant_sequence", "0")) + 1
        except ValueError:
            sequence = 1
        self.store.save_setting("instant_sequence", str(sequence))
        stamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
        return f"IN{stamp}-{sequence:06d}"

    async def start_instant_round(self) -> None:
        if self.game_mode != "instant_dice":
            return
        async with self.lock:
            await self._start_instant_round_locked()

    async def _start_instant_round_locked(self) -> None:
        if self.paused or self.turnover_reached or self.game_mode != "instant_dice":
            return
        if self.instant_active_issue:
            row = self.store.get(self.instant_active_issue)
            if row and row["dice"] is None and row["send_status"] in {
                "sending", "sent", "accepted"
            }:
                return
            self.instant_active_issue = None

        issue = self._new_instant_issue()
        unit_amount = random_bet_amount(self.min_amount, self.max_amount)
        amount = unit_amount * len(self.bet_sides)
        command = " ".join(f"{side}{unit_amount}" for side in self.bet_sides)
        self.store.ensure_round(
            issue,
            game_mode="instant_dice",
            opened_at=now_iso(),
            raw_open="即时模式：主动下注后等待机器人掷骰",
            bet_side=",".join(self.bet_sides),
            unit_amount=unit_amount,
            bet_amount=amount,
            command=command,
            live_mode=int(self.live),
            turnover_after_bet=str(self.store.total_turnover(self.live)),
        )
        self.store.log_event(
            "instant_start", issue=issue, text=f"准备主动发送：{command}"
        )

        if not self.live:
            self.store.update(
                issue,
                send_status="dry_run",
                note="即时掷骰在模拟模式下只生成一单，不发送，因此不会触发机器人骰子；已自动暂停",
            )
            self.pause_if_turnover_reached()
            self.paused = True
            self.store.save_setting(f"paused_{int(self.live)}", "1")
            self.store.update(
                issue, turnover_after_bet=str(self.store.total_turnover(self.live))
            )
            self.write_round_report(issue)
            print(f"\n[模拟即时单] {issue} 计划发送：{command}；未真实发送，已暂停")
            return

        self.instant_active_issue = issue
        self.store.update(issue, send_status="sending")
        try:
            sent = await self.client.send_message(self.group, command)
        except Exception as exc:
            self.instant_active_issue = None
            self.paused = True
            self.store.save_setting(f"paused_{int(self.live)}", "1")
            self.store.update(issue, send_status="send_error", note=repr(exc))
            self.store.log_event("send_error", issue=issue, text=repr(exc))
            self.write_round_report(issue)
            logging.exception("即时下注发送失败，群已暂停")
            return

        current = self.store.get(issue)
        already_settled = current is not None and current["dice"] is not None
        self.store.update(
            issue,
            **({} if already_settled else {"send_status": "sent"}),
            sent_message_id=sent.id,
            sent_at=now_iso(),
        )
        self.store.log_event("sent", issue=issue, message_id=sent.id, text=command)
        self.write_round_report(issue)
        if already_settled:
            return
        print(f"\n[即时下注已发送] {issue}: {command}，等待机器人掷骰")
        self.track_timeout(issue, self.watch_instant_result(issue))

    def cancel_scheduled_instant_round(self) -> None:
        task = self.instant_next_task
        if task and not task.done():
            task.cancel()
        self.instant_next_task = None

    def schedule_next_instant_round(self) -> None:
        if self.paused or self.turnover_reached or self.game_mode != "instant_dice":
            return
        if self.instant_next_task and not self.instant_next_task.done():
            return

        async def run_next() -> None:
            try:
                await asyncio.sleep(self.instant_interval)
                await self.start_instant_round()
            finally:
                if self.instant_next_task is asyncio.current_task():
                    self.instant_next_task = None

        self.instant_next_task = asyncio.create_task(run_next())

    async def watch_instant_result(self, issue: str) -> None:
        try:
            await asyncio.sleep(self.instant_result_timeout)
        except asyncio.CancelledError:
            return
        row = self.store.get(issue)
        if not row or row["dice"] is not None:
            return
        self.paused = True
        self.store.save_setting(f"paused_{int(self.live)}", "1")
        if self.instant_active_issue == issue:
            self.instant_active_issue = None
        values: dict[str, Any] = {
            "result_at": now_iso(),
            "outcome": "unknown",
            "note": f"{self.instant_result_timeout} 秒内未收到这一单的机器人骰子，已自动暂停；请先核对群消息",
        }
        if row["send_status"] in {"sending", "sent"}:
            values["send_status"] = "result_timeout"
        self.store.update(issue, **values)
        self.store.log_event("result_timeout", issue=issue)
        self.write_round_report(issue)
        print(f"\n[开奖超时并暂停] {issue}。请先核对群消息再恢复")

    @staticmethod
    def _reply_message_id(message: Any) -> int | None:
        reply = getattr(message, "reply_to", None)
        return (
            getattr(reply, "reply_to_msg_id", None)
            or getattr(message, "reply_to_msg_id", None)
        )

    async def handle_instant_dice(self, event: events.NewMessage.Event, dice: int) -> None:
        reply_id = self._reply_message_id(event.message)
        row = self.store.by_sent_message(reply_id) if reply_id else None
        if row is not None and row["game_mode"] != "instant_dice":
            row = None
        if row is None and self.instant_active_issue:
            row = self.store.get(self.instant_active_issue)
        if row is None:
            self.store.log_event(
                "unmatched_dice",
                message_id=event.message.id,
                sender_id=event.sender_id,
                text=f"收到骰子 {dice}，但当前没有待开奖即时单，已忽略",
            )
            return
        if row["dice"] is not None:
            self.store.log_event(
                "duplicate_result", issue=row["issue"], message_id=event.message.id
            )
            return
        if row["send_status"] in {"rejected", "send_error", "skipped_paused", "skipped_target"}:
            return

        issue = row["issue"]
        task = self.timeout_tasks.pop(issue, None)
        if task:
            task.cancel()
        if row["send_status"] != "accepted":
            self.store.update(
                issue,
                send_status="accepted",
                ack_at=now_iso(),
                note="机器人已掷骰，视为本单已接受",
            )
            self.store.log_event(
                "accepted_by_result",
                issue=issue,
                message_id=event.message.id,
                sender_id=event.sender_id,
                text="收到对应骰子，确认下注已被处理",
            )
            self.pause_if_turnover_reached()
            self.store.update(
                issue, turnover_after_bet=str(self.store.total_turnover(self.live))
            )
        if self.instant_active_issue == issue:
            self.instant_active_issue = None
        await self.handle_result(
            issue, dice, event.message.id, f"即时掷骰：{dice}"
        )
        self.schedule_next_instant_round()

    async def watch_ack_timeout(self, issue: str) -> None:
        await asyncio.sleep(self.ack_timeout)
        row = self.store.get(issue)
        if row and row["send_status"] == "sent":
            self.paused = True
            self.store.save_setting(f"paused_{int(self.live)}", "1")
            self.store.update(
                issue,
                send_status="ack_timeout",
                note=f"{self.ack_timeout} 秒内未收到可匹配的机器人回执，已自动暂停",
            )
            self.store.log_event("ack_timeout", issue=issue)
            self.write_round_report(issue)
            print(f"\n[回执超时并暂停] {issue}。请核对群消息后再输入 resume")

    async def handle_ack(self, event: events.NewMessage.Event, ack: Any, text: str) -> None:
        reply_id = self._reply_message_id(event.message)
        row = self.store.by_sent_message(reply_id) if reply_id else None
        if row is None:
            # 某些机器人不会真正引用原消息；只在回执中出现完全相同指令时匹配最近待确认订单。
            for candidate in self.store.recent(5):
                command = candidate["command"]
                if command and command in text and candidate["send_status"] in {
                    "sending",
                    "sent",
                    "ack_timeout",
                }:
                    row = candidate
                    break
        if row is None:
            return

        issue = row["issue"]
        is_instant = row["game_mode"] == "instant_dice"
        if not is_instant or ack.rejected:
            task = self.timeout_tasks.pop(issue, None)
            if task:
                task.cancel()
        if ack.accepted:
            status = "accepted"
            note = None
            print(
                f"\n[下注成功] {issue} {row['command']}，"
                f"余额={ack.balance if ack.balance is not None else '未提供'}，"
                f"冻结={ack.frozen if ack.frozen is not None else '未提供'}"
            )
        else:
            status = "rejected"
            note = "机器人拒绝投注或已封盘；本期不会重试"
            print(f"\n[下注未接受] {issue} {row['command']}，本期不重试")
            if is_instant:
                self.paused = True
                self.store.save_setting(f"paused_{int(self.live)}", "1")
                if self.instant_active_issue == issue:
                    self.instant_active_issue = None

        self.store.update(
            issue,
            send_status=status,
            ack_message_id=event.message.id,
            ack_at=now_iso(),
            balance_after_ack=str(ack.balance) if ack.balance is not None else None,
            frozen=str(ack.frozen) if ack.frozen is not None else None,
            raw_ack=text,
            note=note,
        )
        self.store.log_event(
            status,
            issue=issue,
            message_id=event.message.id,
            sender_id=event.sender_id,
            text=text,
        )
        if ack.accepted:
            self.pause_if_turnover_reached()
            self.store.update(issue, turnover_after_bet=str(self.store.total_turnover(self.live)))
        self.write_round_report(issue)

    async def handle_result(self, issue: str, dice: int, message_id: int, text: str) -> None:
        self.store.ensure_round(issue)
        row = self.store.get(issue)
        assert row is not None
        if row["dice"] is not None:
            self.store.log_event(
                "duplicate_result", issue=issue, message_id=message_id, text=f"重复骰子 {dice}"
            )
            return
        safe_result_text = f"第{issue}期 骰子为:{dice}"
        winning_sides = result_sides(dice)
        result_side = ",".join(winning_sides)
        outcome = "not_bet"
        net: Decimal | None = None
        estimated_balance: Decimal | None = None

        if row["send_status"] == "accepted" and row["bet_amount"] is not None:
            selected = normalize_bet_sides(row["bet_side"] or "大")
            unit = Decimal(row["unit_amount"]) if row["unit_amount"] is not None else Decimal(row["bet_amount"]) / Decimal(len(selected))
            won_count = len(set(selected) & set(winning_sides))
            net = unit * (Decimal(won_count) - Decimal(len(selected) - won_count))
            outcome = "win" if net > 0 else "loss" if net < 0 else "tie"
            if row["balance_after_ack"] is not None:
                liquid_after_ack = Decimal(row["balance_after_ack"])
                estimated_balance = liquid_after_ack + unit * 2 * won_count
                self.current_balance = estimated_balance
        elif row["send_status"] == "dry_run" and row["bet_amount"] is not None:
            selected = normalize_bet_sides(row["bet_side"] or "大")
            unit = Decimal(row["unit_amount"]) if row["unit_amount"] is not None else Decimal(row["bet_amount"]) / Decimal(len(selected))
            won_count = len(set(selected) & set(winning_sides))
            net = unit * (Decimal(won_count) - Decimal(len(selected) - won_count))
            outcome = "win" if net > 0 else "loss" if net < 0 else "tie"
            if self.current_balance is not None:
                self.current_balance += net
                estimated_balance = self.current_balance
        elif row["send_status"] in {"sent", "ack_timeout", "sending"}:
            outcome = "unknown"

        self.store.update(
            issue,
            result_message_id=message_id,
            result_at=now_iso(),
            dice=dice,
            result_side=result_side,
            outcome=outcome,
            net=str(net) if net is not None else None,
            estimated_balance_after_result=(
                str(estimated_balance) if estimated_balance is not None else None
            ),
            raw_result=safe_result_text,
        )
        self.store.log_event(
            "result",
            issue=issue,
            message_id=message_id,
            sender_id=self.bot_id,
            text=safe_result_text,
        )
        self.write_round_report(issue)
        if outcome in {"win", "loss", "tie"}:
            print(
                f"\n[开奖] {issue} 骰子={dice}({result_side})，"
                f"结果={'中' if outcome == 'win' else '未中' if outcome == 'loss' else '持平'}，"
                f"本期净额={net:+}，"
                f"估算结算余额={estimated_balance if estimated_balance is not None else '无法估算'}"
            )
        else:
            print(f"\n[开奖] {issue} 骰子={dice}({result_side})，下注状态={outcome}")

    def pause_if_turnover_reached(self) -> bool:
        self.current_turnover = self.store.cycle_turnover(self.live)
        if self.current_turnover < self.stop_turnover:
            return False
        self.turnover_reached = True
        self.paused = True
        print(
            f"\n[已达到流水目标] 当前累计流水={self.current_turnover}，"
            f"目标流水={self.stop_turnover}；"
            "已停止后续下注，但会继续监听和记录。"
        )
        return True

    def show_status(self) -> None:
        row = self.store.recent(1)
        print(
            f"模式={'真实发送' if self.live else '模拟'} | "
            f"状态={'已达流水目标' if self.turnover_reached else ('暂停' if self.paused else '运行')} | "
            f"随机金额={self.min_amount}~{self.max_amount} | "
            f"累计流水={self.current_turnover}/{self.stop_turnover} | "
            f"最近余额={self.current_balance if self.current_balance is not None else '未知'}"
        )
        if row:
            item = row[0]
            print(
                f"最近期号={item['issue']} | 发送状态={item['send_status']} | "
                f"骰子={item['dice'] if item['dice'] is not None else '-'} | "
                f"结果={item['outcome'] or '-'} | 净额={item['net'] or '-'}"
            )

    def show_history(self, limit: int = 10) -> None:
        rows = self.store.recent(limit)
        if not rows:
            print("暂无记录")
            return
        print("期号 | 指令 | 状态 | 骰子 | 结果 | 净额 | 估算余额")
        for row in rows:
            print(
                f"{row['issue']} | {row['command'] or '-'} | {row['send_status']} | "
                f"{row['dice'] if row['dice'] is not None else '-'} | "
                f"{row['outcome'] or '-'} | {row['net'] or '-'} | "
                f"{row['estimated_balance_after_result'] or '-'}"
            )

    def show_summary(self) -> None:
        rows = self.store.all_settled()
        wins = sum(row["outcome"] == "win" for row in rows)
        losses = sum(row["outcome"] == "loss" for row in rows)
        total = sum((Decimal(row["net"]) for row in rows if row["net"]), Decimal(0))
        print(
            f"已结算={len(rows)} | 中={wins} | 未中={losses} | "
            f"命中率={(wins / len(rows) * 100):.2f}% | 累计净额={total:+}"
            if rows
            else "暂无已结算记录"
        )

    @staticmethod
    def _round_for_dashboard(row: sqlite3.Row) -> dict[str, Any]:
        balance = row["estimated_balance_after_result"] or row["balance_after_ack"]
        return {
            "issue": row["issue"],
            "game_mode": row["game_mode"],
            "command": row["command"],
            "amount": row["bet_amount"],
            "unit_amount": row["unit_amount"],
            "bet_sides": normalize_bet_sides(row["bet_side"] or "大"),
            "status": row["send_status"],
            "dice": row["dice"],
            "result_side": row["result_side"],
            "outcome": row["outcome"],
            "net": row["net"],
            "turnover": row["turnover_after_bet"],
            "balance": balance,
            "opened_at": row["opened_at"],
            "sent_at": row["sent_at"],
            "ack_at": row["ack_at"],
            "result_at": row["result_at"],
            "note": row["note"],
        }

    async def dashboard_state(self) -> dict[str, Any]:
        self.current_turnover = self.store.cycle_turnover(self.live)
        rounds = self.store.recent_for_mode(self.live, 80)
        settled = self.store.settled_for_mode(self.live)
        wins = sum(row["outcome"] == "win" for row in settled)
        losses = sum(row["outcome"] == "loss" for row in settled)
        ties = sum(row["outcome"] == "tie" for row in settled)
        total_net = sum(
            (Decimal(row["net"]) for row in settled if row["net"] is not None),
            Decimal(0),
        )
        progress = min(
            100,
            float(self.current_turnover / self.stop_turnover * 100)
            if self.stop_turnover
            else 0,
        )
        events_payload = []
        for event in self.store.recent_events(100):
            event_data = dict(event)
            if event_data.get("text") and len(event_data["text"]) > 500:
                event_data["text"] = event_data["text"][:500] + "…"
            events_payload.append(event_data)
        return {
            "updated_at": now_iso(),
            "mode": "live" if self.live else "simulation",
            "group_name": self.group_name,
            "group_id": self.group_id,
            "bot_id": self.bot_id,
            "game_mode": self.game_mode,
            "game_mode_label": "下注后掷骰" if self.game_mode == "instant_dice" else "定时开盘",
            "instant_pending_issue": self.instant_active_issue,
            "instant_result_timeout": self.instant_result_timeout,
            "instant_interval": self.instant_interval,
            "instant_waiting_next": bool(
                self.instant_next_task and not self.instant_next_task.done()
            ),
            "paused": self.paused,
            "turnover_reached": self.turnover_reached,
            "min_amount": self.min_amount,
            "max_amount": self.max_amount,
            "bet_sides": self.bet_sides,
            "turnover": str(self.current_turnover),
            "target_turnover": str(self.stop_turnover),
            "lifetime_turnover": str(self.store.total_turnover(self.live)),
            "turnover_reset_at": self.store.setting(f"turnover_reset_at_{int(self.live)}"),
            "turnover_progress": round(progress, 2),
            "current_balance": (
                str(self.current_balance) if self.current_balance is not None else None
            ),
            "summary": {
                "settled": len(settled),
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "hit_rate": round(wins / len(settled) * 100, 2) if settled else 0,
                "total_net": str(total_net),
            },
            "latest": self._round_for_dashboard(rounds[0]) if rounds else None,
            "rounds": [self._round_for_dashboard(row) for row in rounds],
            "events": events_payload,
        }

    async def dashboard_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        start_instant = False
        if action == "pause":
            self.paused = True
            self.cancel_scheduled_instant_round()
        elif action == "resume":
            self.current_turnover = self.store.cycle_turnover(self.live)
            if self.current_turnover >= self.stop_turnover:
                self.turnover_reached = True
                raise ValueError("累计流水已达到目标，请先提高流水目标")
            self.turnover_reached = False
            self.paused = False
            start_instant = self.game_mode == "instant_dice"
        elif action == "set_amount":
            try:
                minimum = positive_int(str(payload.get("min_amount", "")))
                maximum = positive_int(str(payload.get("max_amount", "")))
            except argparse.ArgumentTypeError as exc:
                raise ValueError(str(exc)) from exc
            if minimum > maximum:
                raise ValueError("最小金额不能大于最大金额")
            self.min_amount = minimum
            self.max_amount = maximum
        elif action == "set_sides":
            self.bet_sides = normalize_bet_sides(payload.get("bet_sides"))
            self.store.save_setting("bet_sides", ",".join(self.bet_sides))
        elif action == "set_game_mode":
            new_mode = normalize_game_mode(payload.get("game_mode"))
            new_interval = instant_interval_seconds(
                payload.get("instant_interval", self.instant_interval)
            )
            if new_mode != self.game_mode and not self.paused:
                raise ValueError("请先暂停此群，再修改开奖方式")
            if new_mode != self.game_mode and self.instant_active_issue:
                raise ValueError("上一单仍在等待骰子，暂时不能修改开奖方式")
            was_waiting = bool(
                self.instant_next_task and not self.instant_next_task.done()
            )
            if new_mode != self.game_mode or was_waiting:
                self.cancel_scheduled_instant_round()
            self.game_mode = new_mode
            self.instant_interval = new_interval
            self.store.save_setting("game_mode", self.game_mode)
            self.store.save_setting("instant_interval", str(self.instant_interval))
            if was_waiting and self.game_mode == "instant_dice":
                self.schedule_next_instant_round()
        elif action in {"set_target", "reset_turnover"}:
            try:
                target = positive_decimal(
                    str(payload.get("target_turnover", ""))
                )
            except argparse.ArgumentTypeError as exc:
                raise ValueError(str(exc)) from exc
            if action == "reset_turnover":
                async with self.lock:
                    self.store.reset_turnover(self.live, target)
                    self.paused = True
                    self.cancel_scheduled_instant_round()
            else:
                self.store.save_setting(f"turnover_target_{int(self.live)}", str(target))
            self.stop_turnover = target
            self.turnover_reached = False
            self.pause_if_turnover_reached()
        elif action == "export":
            self.store.export_csv(self.store.path.parent / "rounds.csv")
        elif action == "stop":
            asyncio.get_running_loop().call_later(0.2, self.client.disconnect)
        else:
            raise ValueError("未知操作")
        if start_instant:
            await self.start_instant_round()
        return await self.dashboard_state()

    async def console(self) -> None:
        print(
            "控制命令：status | amount 最小金额 最大金额 | target 目标流水 | "
            "pause | resume | history [条数] | summary | export | quit"
        )
        loop = asyncio.get_running_loop()
        input_queue: asyncio.Queue[str] = asyncio.Queue()

        def read_console() -> None:
            while True:
                try:
                    raw_value = input("> ")
                except (EOFError, KeyboardInterrupt):
                    raw_value = "quit"
                loop.call_soon_threadsafe(input_queue.put_nowait, raw_value)
                if raw_value.strip().lower() == "quit":
                    return

        threading.Thread(target=read_console, name="console-input", daemon=True).start()
        while True:
            raw = await input_queue.get()
            parts = raw.strip().split()
            if not parts:
                continue
            command = parts[0].lower()
            if command == "status":
                self.show_status()
            elif command == "amount" and len(parts) in {2, 3}:
                try:
                    minimum = positive_int(parts[1])
                    maximum = positive_int(parts[2]) if len(parts) == 3 else minimum
                    if minimum > maximum:
                        raise ValueError("最小金额不能大于最大金额")
                    self.min_amount = minimum
                    self.max_amount = maximum
                    print(f"下一期起随机金额改为：{minimum}~{maximum}")
                except (argparse.ArgumentTypeError, ValueError) as exc:
                    print(exc)
            elif command == "target" and len(parts) == 2:
                try:
                    self.stop_turnover = positive_decimal(parts[1])
                    self.store.save_setting(f"turnover_target_{int(self.live)}", str(self.stop_turnover))
                    self.turnover_reached = False
                    print(f"停止目标已改为：累计流水达到 {self.stop_turnover}")
                    self.pause_if_turnover_reached()
                except argparse.ArgumentTypeError as exc:
                    print(exc)
            elif command == "pause":
                self.paused = True
                print("已暂停；仍继续记录群消息和开奖结果")
            elif command == "resume":
                self.current_turnover = self.store.cycle_turnover(self.live)
                if self.current_turnover >= self.stop_turnover:
                    self.turnover_reached = True
                    print("累计流水仍已达到停止目标；请先用 target 设置更高目标")
                else:
                    self.turnover_reached = False
                    self.paused = False
                    print("已恢复；从下一个新期号开始下注，不补投旧期")
            elif command == "history":
                limit = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 10
                self.show_history(min(max(limit, 1), 100))
            elif command == "summary":
                self.show_summary()
            elif command == "export":
                target = DATA_DIR / "rounds.csv"
                try:
                    self.store.export_csv(target)
                    print(f"已导出：{target}")
                except RuntimeError as exc:
                    print(exc)
            elif command == "quit":
                print("正在安全退出……")
                await self.client.disconnect()
                return
            else:
                print(
                    "未知命令。输入 status、amount、target、pause、resume、history、"
                    "summary、export 或 quit"
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram 多群大、小、单、双组合下注与记录脚本")
    parser.add_argument("--live", action="store_true", help="真实发送；不加时只模拟和记录")
    parser.add_argument("--amount", type=positive_int, help="固定金额（等同于最小值和最大值相同）")
    parser.add_argument("--min-amount", type=positive_int, help="每期随机金额下限")
    parser.add_argument("--max-amount", type=positive_int, help="每期随机金额上限")
    parser.add_argument(
        "--stop-turnover",
        type=positive_decimal,
        help="累计成功下注流水达到该值时停止下注",
    )
    parser.add_argument(
        "--start-balance",
        type=positive_decimal,
        help="可选的起始余额，主要供模拟模式计算目标",
    )
    parser.add_argument("--group", help="覆盖 .env 中的 TG_GROUP")
    parser.add_argument("--bot-id", type=int, help="覆盖 .env 中的 TG_GAME_BOT_ID")
    parser.add_argument("--dashboard-port", type=positive_int, help="本地网页端口")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--session", type=Path, default=DEFAULT_SESSION)
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Config:
    load_env_file(ENV_FILE)
    raw_api_id = os.getenv("TG_API_ID", "").strip()
    api_hash = os.getenv("TG_API_HASH", "").strip()
    if not raw_api_id.isdigit() or not api_hash:
        raise SystemExit("请复制 .env.example 为 .env，并在本机填写 TG_API_ID 和 TG_API_HASH")
    bot_raw = str(args.bot_id or os.getenv("TG_GAME_BOT_ID", "")).strip()
    ack_raw = os.getenv("TG_ACK_TIMEOUT", "10").strip()

    def env_int(name: str) -> int | None:
        raw = os.getenv(name, "").strip()
        return positive_int(raw) if raw else None

    def env_decimal(name: str) -> Decimal | None:
        raw = os.getenv(name, "").strip()
        return positive_decimal(raw) if raw else None

    def local_socks5_available(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.3) as connection:
                connection.sendall(b"\x05\x01\x00")
                return connection.recv(2) == b"\x05\x00"
        except OSError:
            return False

    proxy_type = os.getenv("TG_PROXY_TYPE", "socks5").strip().lower()
    proxy_host = os.getenv("TG_PROXY_HOST", "").strip()
    proxy_port_raw = os.getenv("TG_PROXY_PORT", "").strip()
    proxy_user = os.getenv("TG_PROXY_USER", "").strip() or None
    proxy_password = os.getenv("TG_PROXY_PASSWORD", "").strip() or None
    proxy: tuple[str, str, int, bool, str | None, str | None] | None = None
    proxy_label: str | None = None

    if proxy_host or proxy_port_raw:
        if not proxy_host or not proxy_port_raw.isdigit():
            raise SystemExit("TG_PROXY_HOST 和 TG_PROXY_PORT 必须同时正确填写")
        if proxy_type not in {"socks5", "socks4", "http"}:
            raise SystemExit("TG_PROXY_TYPE 只能是 socks5、socks4 或 http")
        proxy_port = int(proxy_port_raw)
        proxy = (proxy_type, proxy_host, proxy_port, True, proxy_user, proxy_password)
        proxy_label = f"{proxy_type}://{proxy_host}:{proxy_port}"
    else:
        # v2rayN 常见的本地 SOCKS/混合端口；只有完成 SOCKS5 握手才会启用。
        for proxy_port in (10808, 1080, 10809, 7890):
            if local_socks5_available("127.0.0.1", proxy_port):
                proxy = ("socks5", "127.0.0.1", proxy_port, True, None, None)
                proxy_label = f"socks5://127.0.0.1:{proxy_port}（自动检测）"
                break

    dashboard_port_raw = os.getenv("DASHBOARD_PORT", "8765").strip()
    dashboard_port = args.dashboard_port or (
        int(dashboard_port_raw) if dashboard_port_raw.isdigit() else 8765
    )
    if not 1 <= dashboard_port <= 65535:
        raise SystemExit("DASHBOARD_PORT 必须在 1 到 65535 之间")

    return Config(
        api_id=int(raw_api_id),
        api_hash=api_hash,
        group_selector=(args.group or os.getenv("TG_GROUP", "")).strip(),
        bot_id=int(bot_raw) if bot_raw.lstrip("-").isdigit() else None,
        ack_timeout=max(3, int(ack_raw)) if ack_raw.isdigit() else 10,
        live=args.live,
        amount=args.amount,
        min_amount=args.min_amount or env_int("BET_MIN_AMOUNT"),
        max_amount=args.max_amount or env_int("BET_MAX_AMOUNT"),
        stop_turnover=args.stop_turnover or env_decimal("BET_STOP_TURNOVER"),
        start_balance=args.start_balance or env_decimal("BET_START_BALANCE"),
        proxy=proxy,
        proxy_label=proxy_label,
        dashboard_port=dashboard_port,
        open_browser=not args.no_browser,
        db_path=args.db,
        session_path=args.session,
    )


async def choose_group(client: TelegramClient, selector: str) -> Any:
    if selector:
        key: Any = int(selector) if selector.lstrip("-").isdigit() else selector
        return await client.get_entity(key)

    choices = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            choices.append(dialog)
    if not choices:
        raise RuntimeError("当前账号没有可见群聊")
    print("请选择目标群：")
    for index, dialog in enumerate(choices, start=1):
        print(f"{index}. {dialog.name} | ID={utils.get_peer_id(dialog.entity)}")
    while True:
        value = input("输入序号：").strip()
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1].entity
        print("序号无效")


async def choose_bot_id(client: TelegramClient, group: Any, configured: int | None) -> int:
    if configured is not None:
        return configured

    counts: Counter[int] = Counter()
    samples: dict[int, str] = {}
    async for message in client.iter_messages(group, limit=150):
        text = message.raw_text or ""
        opened_issue = parse_open(text)
        parsed_result = parse_result(text)
        if opened_issue or parsed_result:
            if message.sender_id is not None:
                counts[message.sender_id] += 1
                safe_sample = (
                    f"开盘 {opened_issue}"
                    if opened_issue
                    else f"开奖 {parsed_result[0]} 骰子={parsed_result[1]}"
                )
                samples.setdefault(message.sender_id, safe_sample)
    if not counts:
        raise RuntimeError("最近消息中没有找到开奖机器人候选；请在 .env 填写 TG_GAME_BOT_ID")

    print("检测到可能的游戏机器人：")
    for sender_id, count in counts.most_common():
        entity = await client.get_entity(sender_id)
        name = utils.get_display_name(entity)
        print(f"ID={sender_id} | 名称={name} | 匹配消息={count} | 示例={samples[sender_id]}")
    while True:
        value = input("输入并确认游戏机器人数字 ID：").strip()
        if value.lstrip("-").isdigit() and int(value) in counts:
            return int(value)
        print("请输入上面列出的候选 ID；这一步用于防止其他群成员伪造开奖消息")


async def main() -> None:
    args = parse_args()
    config = build_config(args)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    min_amount = config.amount or config.min_amount
    max_amount = config.amount or config.max_amount
    while min_amount is None:
        try:
            min_amount = positive_int(input("请输入每期随机下注金额下限 X（整数）："))
        except argparse.ArgumentTypeError as exc:
            print(exc)
    while max_amount is None:
        try:
            max_amount = positive_int(input("请输入每期随机下注金额上限 Y（整数）："))
        except argparse.ArgumentTypeError as exc:
            print(exc)
    while min_amount > max_amount:
        print("最小金额不能大于最大金额，请重新输入。")
        min_amount = None
        max_amount = None
        while min_amount is None:
            try:
                min_amount = positive_int(input("请输入金额下限 X："))
            except argparse.ArgumentTypeError as exc:
                print(exc)
        while max_amount is None:
            try:
                max_amount = positive_int(input("请输入金额上限 Y："))
            except argparse.ArgumentTypeError as exc:
                print(exc)

    store = Store(config.db_path)
    saved_target = store.setting(f"turnover_target_{int(config.live)}")
    stop_turnover = Decimal(saved_target) if saved_target else config.stop_turnover
    if args.stop_turnover is not None:
        stop_turnover = args.stop_turnover
        store.save_setting(f"turnover_target_{int(config.live)}", str(stop_turnover))
    while stop_turnover is None:
        try:
            stop_turnover = positive_decimal(input("请输入停止下注的总流水 N："))
        except argparse.ArgumentTypeError as exc:
            print(exc)

    client = TelegramClient(
        str(config.session_path),
        config.api_id,
        config.api_hash,
        proxy=config.proxy,
        sequential_updates=False,
    )
    if config.proxy_label:
        print(f"Telegram 连接代理：{config.proxy_label}")
    else:
        print("Telegram 连接代理：未检测到，尝试直接连接")
    try:
        await client.start()
    except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
        print(f"无法连接 Telegram：{exc}")
        print("请确认 v2rayN 正在运行，并检查 .env 中的 TG_PROXY_* 设置。")
        return
    group = await choose_group(client, config.group_selector or store.setting("last_group"))
    group_id = utils.get_peer_id(group)
    bound_group = store.setting("bound_group_id") or store.setting("last_group")
    if bound_group and int(bound_group) != group_id:
        await client.disconnect()
        store.conn.close()
        raise ValueError("原数据库已绑定其他群。请恢复原群配置，并在网页中添加新群，避免混合历史记录。")
    store.save_setting("bound_group_id", str(group_id))
    saved_bot = store.setting(f"bot_for_group_{group_id}")
    bot_id = await choose_bot_id(client, group, config.bot_id or (int(saved_bot) if saved_bot else None))
    store.save_setting("last_group", str(group_id))
    store.save_setting(f"bot_for_group_{group_id}", str(bot_id))
    group_name = utils.get_display_name(group)

    print(f"目标群：{group_name} ({group_id})")
    print(f"游戏机器人 ID：{bot_id}")
    print(f"停止目标：累计成功下注流水达到 {stop_turnover}")
    if config.start_balance is not None:
        print(f"起始余额：{config.start_balance}")
    print(f"运行模式：{'真实自动发送' if config.live else '模拟监听，不发送'}")
    runner = BigBetRunner(
        client,
        group,
        group_id,
        bot_id,
        store,
        min_amount=min_amount,
        max_amount=max_amount,
        stop_turnover=stop_turnover,
        start_balance=config.start_balance,
        live=config.live,
        ack_timeout=config.ack_timeout,
        group_name=group_name,
    )
    print(f"下注选项：{'＋'.join(runner.bet_sides)}")
    print(f"每个选项随机金额：{min_amount}~{max_amount}")
    manager = MultiGroupManager(client, runner, BigBetRunner, Store)
    await manager.load()
    client.add_event_handler(
        manager.handle_message,
        events.NewMessage(),
    )
    await manager.start_running_instant_groups()
    me = await client.get_me()
    accounts = AccountRegistry(
        client,
        manager,
        BigBetRunner,
        Store,
        accounts_root=DATA_DIR / "accounts",
        live=config.live,
        ack_timeout=config.ack_timeout,
        proxy=config.proxy,
        shared_api_id=config.api_id,
        shared_api_hash=config.api_hash,
        default_label=utils.get_display_name(me) or "默认账号",
        default_username=getattr(me, "username", None),
        default_phone=getattr(me, "phone", None),
        default_user_id=int(me.id),
    )
    await accounts.load()
    dashboard = DashboardServer(
        accounts,
        asyncio.get_running_loop(),
        WEB_DIR,
        port=config.dashboard_port,
    )
    dashboard_url = dashboard.start()
    print(f"本地控制面板：{dashboard_url}")
    if config.open_browser:
        await asyncio.to_thread(webbrowser.open, dashboard_url)
    print("监听已启动；只处理启动后的新消息，不追投历史期号。")
    print("多群管理已启用；请在网页中切换群、添加群和控制下注。Ctrl+C 可退出。")
    try:
        await client.run_until_disconnected()
    finally:
        dashboard.stop()
        await accounts.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已退出")

