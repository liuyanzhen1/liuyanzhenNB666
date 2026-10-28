"""Multiple Telegram accounts behind one local dashboard."""
from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import hmac
import json
import re
import secrets
import shutil
from dataclasses import dataclass
from ctypes import wintypes
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events, utils
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

from multi_group import MultiGroupManager


API_ID_RE = re.compile(r"(?i)api[\s_-]*id\s*[:=]?\s*(\d{5,})")
API_HASH_RE = re.compile(r"(?i)api[\s_-]*hash\s*[:=]?\s*([0-9a-f]{32})")
PHONE_RE = re.compile(r"^\+?\d{6,15}$")
INVITE_RE = re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)")
REPORT_USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(wintypes.BYTE)),
    ]


def _windows_protect(data: bytes, decrypt: bool = False) -> bytes:
    if not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("账号密钥加密仅支持 Windows")
    buffer = (wintypes.BYTE * len(data)).from_buffer_copy(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE)))
    output = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if decrypt:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        ok = function(
            ctypes.byref(source), None, None, None, None, flags, ctypes.byref(output)
        )
    else:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        function.restype = wintypes.BOOL
        ok = function(
            ctypes.byref(source), None, None, None, None, flags,
            ctypes.byref(output),
        )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output.pbData)


def _local_key(path: Path) -> bytes:
    if path.exists():
        key = path.read_bytes()
        if len(key) == 32:
            return key
        raise RuntimeError("本地账号密钥文件已损坏")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _local_crypt(data: bytes, key: bytes, nonce: bytes) -> bytes:
    stream = bytearray()
    counter = 0
    while len(stream) < len(data):
        stream.extend(
            hmac.new(key, b"enc" + nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        )
        counter += 1
    return bytes(left ^ right for left, right in zip(data, stream))


def protect_secret(value: str, key_path: Path | None = None) -> str:
    raw = value.encode("utf-8")
    try:
        return "dpapi:" + base64.b64encode(_windows_protect(raw)).decode("ascii")
    except OSError:
        path = key_path or Path(__file__).resolve().parent / "data" / "accounts" / ".secret.key"
        key = _local_key(path)
        nonce = secrets.token_bytes(16)
        encrypted = _local_crypt(raw, key, nonce)
        tag = hmac.new(key, b"tag" + nonce + encrypted, hashlib.sha256).digest()
        return "local:" + base64.b64encode(nonce + encrypted + tag).decode("ascii")


def unprotect_secret(value: str, key_path: Path | None = None) -> str:
    if value.startswith("dpapi:"):
        raw = base64.b64decode(value[6:].encode("ascii"), validate=True)
        return _windows_protect(raw, decrypt=True).decode("utf-8")
    if not value.startswith("local:"):
        raise ValueError("未知的账号密钥格式")
    payload = base64.b64decode(value[6:].encode("ascii"), validate=True)
    if len(payload) < 49:
        raise ValueError("账号密钥内容已损坏")
    nonce, encrypted, supplied_tag = payload[:16], payload[16:-32], payload[-32:]
    path = key_path or Path(__file__).resolve().parent / "data" / "accounts" / ".secret.key"
    key = _local_key(path)
    expected_tag = hmac.new(key, b"tag" + nonce + encrypted, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_tag, expected_tag):
        raise ValueError("账号密钥校验失败")
    return _local_crypt(encrypted, key, nonce).decode("utf-8")


def parse_api_credentials(value: str) -> tuple[int, str]:
    text = str(value or "").strip()
    api_id_match = API_ID_RE.search(text)
    api_hash_match = API_HASH_RE.search(text)
    if not api_id_match:
        api_id_match = re.search(r"\b(\d{5,})\b", text)
    if not api_hash_match:
        api_hash_match = re.search(r"(?i)\b([0-9a-f]{32})\b", text)
    if not api_id_match or not api_hash_match:
        raise ValueError("请粘贴完整的 API ID 和 32 位 API Hash")
    api_id = int(api_id_match.group(1))
    api_hash = api_hash_match.group(1).lower()
    if api_id <= 0:
        raise ValueError("API ID 无效")
    return api_id, api_hash


def parse_api_fields(api_id_value: Any, api_hash_value: Any) -> tuple[int, str]:
    api_id_text = str(api_id_value or "").strip()
    api_hash = str(api_hash_value or "").strip().lower()
    if not re.fullmatch(r"\d{5,}", api_id_text):
        raise ValueError("App api_id 只填写数字")
    if not re.fullmatch(r"[0-9a-f]{32}", api_hash):
        raise ValueError("App api_hash 必须是 32 位字母和数字")
    api_id = int(api_id_text)
    if api_id <= 0:
        raise ValueError("App api_id 无效")
    return api_id, api_hash


def normalize_phone(value: str) -> str:
    phone = re.sub(r"[\s()-]", "", str(value or ""))
    if not PHONE_RE.fullmatch(phone):
        raise ValueError("请输入含国家区号的手机号，例如 +8613800000000")
    return phone if phone.startswith("+") else "+" + phone


def mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


def next_half_hour(now: datetime | None = None) -> datetime:
    current = now or datetime.now().astimezone()
    base = current.replace(second=0, microsecond=0)
    if current.minute in {0, 30} and current.second == 0 and current.microsecond == 0:
        return base
    if current.minute < 30:
        return base.replace(minute=30)
    return (base.replace(minute=0) + timedelta(hours=1))


@dataclass
class AccountContext:
    account_id: str
    label: str
    display_name: str
    username: str | None
    phone_mask: str
    telegram_user_id: int | None
    client: Any
    manager: MultiGroupManager | None
    can_delete: bool
    status: str = "connected"
    error: str | None = None


class AccountRegistry:
    def __init__(
        self,
        base_client: Any,
        base_manager: MultiGroupManager,
        runner_factory: Any,
        store_factory: Any,
        *,
        accounts_root: Path,
        live: bool,
        ack_timeout: int,
        proxy: Any,
        shared_api_id: int,
        shared_api_hash: str,
        default_label: str,
        default_username: str | None = None,
        default_phone: str | None = None,
        default_user_id: int | None = None,
        report_sender_username: str = "@aghaststar",
        report_chat_selector: str = "https://t.me/+_6dhl6VYwjwxZWU9",
    ):
        self.runner_factory = runner_factory
        self.store_factory = store_factory
        self.accounts_root = accounts_root
        self.accounts_root.mkdir(parents=True, exist_ok=True)
        self.archive_root = accounts_root.parent / "已移除账号"
        self.config_path = accounts_root / "accounts.json"
        self.live = live
        self.ack_timeout = ack_timeout
        self.proxy = proxy
        self.shared_api_id = int(shared_api_id)
        self.shared_api_hash = str(shared_api_hash)
        self.lock = asyncio.Lock()
        self.shutdown_event = asyncio.Event()
        self.watchdog_task: asyncio.Task[None] | None = None
        self.watchdog_interval = 180
        self.last_watchdog_at: str | None = None
        self.report_task: asyncio.Task[None] | None = None
        self.settings_store = base_manager.control_store
        self.report_enabled = self.settings_store.setting(
            "half_hour_report_enabled", "1"
        ) == "1"
        self.report_include_details = self.settings_store.setting(
            "half_hour_report_include_details", "0"
        ) == "1"
        self.report_sender_username = self.settings_store.setting(
            "half_hour_report_sender", str(report_sender_username)
        ).strip().lstrip("@")
        self.report_chat_selector = self.settings_store.setting(
            "half_hour_report_chat", str(report_chat_selector)
        ).strip()
        self.last_report_at: str | None = None
        self.next_report_at: str | None = None
        self.report_status = "等待下一个整点或半点"
        self.report_target_cache: dict[str, Any] = {}
        self.report_lock = asyncio.Lock()
        self.pending: dict[str, dict[str, Any]] = {}
        self.accounts: dict[str, AccountContext] = {
            "default": AccountContext(
                account_id="default",
                label=default_label or "默认账号",
                display_name=default_label or "默认账号",
                username=default_username,
                phone_mask=mask_phone(default_phone),
                telegram_user_id=default_user_id,
                client=base_client,
                manager=base_manager,
                can_delete=False,
            )
        }
        self.saved_configs = self._read_configs()
        if any(
            "api_id" in config or "api_hash_protected" in config
            for config in self.saved_configs
        ):
            for config in self.saved_configs:
                config.pop("api_id", None)
                config.pop("api_hash_protected", None)
            self._save_configs()

    def _read_configs(self) -> list[dict[str, Any]]:
        if not self.config_path.exists():
            return []
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def _save_configs(self) -> None:
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.saved_configs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.config_path)

    async def load(self) -> None:
        async def load_one(config: dict[str, Any]) -> None:
            account_id = str(config.get("account_id", ""))
            if not account_id or account_id in self.accounts:
                return
            try:
                await self._load_saved_account(config)
            except Exception as exc:
                self.accounts[account_id] = AccountContext(
                    account_id=account_id,
                    label=str(config.get("label") or "未命名账号"),
                    display_name=str(config.get("display_name") or "未连接"),
                    username=config.get("username"),
                    phone_mask=str(config.get("phone_mask") or ""),
                    telegram_user_id=config.get("telegram_user_id"),
                    client=None,
                    manager=None,
                    can_delete=True,
                    status="error",
                    error=str(exc),
                )
        await asyncio.gather(*(load_one(config) for config in self.saved_configs))

    async def _load_saved_account(self, config: dict[str, Any]) -> None:
        account_id = str(config["account_id"])
        folder = self.accounts_root / account_id
        client = TelegramClient(
            str(folder / "telegram"),
            self.shared_api_id,
            self.shared_api_hash,
            proxy=self.proxy,
            sequential_updates=False,
        )
        manager = None
        try:
            await asyncio.wait_for(client.connect(), 25)
            if not await client.is_user_authorized():
                raise RuntimeError("登录已失效，请移除后重新添加")
            manager = MultiGroupManager(
                client,
                None,
                self.runner_factory,
                self.store_factory,
                root_dir=folder,
                live=self.live,
                ack_timeout=self.ack_timeout,
            )
            await manager.load()
            client.add_event_handler(manager.handle_message, events.NewMessage())
            await manager.start_running_instant_groups()
            me = await client.get_me()
        except Exception:
            if manager is not None:
                await manager.close()
            await client.disconnect()
            raise
        self.accounts[account_id] = AccountContext(
            account_id=account_id,
            label=str(config.get("label") or utils.get_display_name(me)),
            display_name=utils.get_display_name(me),
            username=getattr(me, "username", None),
            phone_mask=mask_phone(getattr(me, "phone", None)),
            telegram_user_id=int(me.id),
            client=client,
            manager=manager,
            can_delete=True,
        )

    async def dashboard_state(self) -> dict[str, Any]:
        payload = []
        for account in self.accounts.values():
            if account.manager is not None:
                state = await account.manager.dashboard_state()
            else:
                state = {"groups": [], "errors": [], "mode": "live" if self.live else "simulation"}
            payload.append(
                {
                    **state,
                    "account_id": account.account_id,
                    "account_label": account.label,
                    "account_display_name": account.display_name,
                    "account_username": account.username,
                    "account_phone_mask": account.phone_mask,
                    "telegram_user_id": account.telegram_user_id,
                    "account_status": account.status,
                    "account_error": account.error,
                    "can_delete": account.can_delete,
                }
            )
        return {
            "accounts": payload,
            "mode": "live" if self.live else "simulation",
            "watchdog_interval": self.watchdog_interval,
            "last_watchdog_at": self.last_watchdog_at,
            "report_sender_username": self.report_sender_username,
            "report_chat_selector": self.report_chat_selector,
            "report_enabled": self.report_enabled,
            "report_include_details": self.report_include_details,
            "last_report_at": self.last_report_at,
            "next_report_at": self.next_report_at,
            "report_status": self.report_status,
        }

    def reporting_account(self) -> AccountContext | None:
        wanted = self.report_sender_username.lower()
        return next(
            (
                account for account in self.accounts.values()
                if (account.username or "").lower().lstrip("@") == wanted
                and account.client is not None
            ),
            None,
        )

    async def resolve_report_target(self, account: AccountContext):
        cached = self.report_target_cache.get(account.account_id)
        if cached is not None:
            return cached
        selector = self.report_chat_selector
        match = INVITE_RE.search(selector)
        if match:
            invite_hash = match.group(1)
            checked = await asyncio.wait_for(
                account.client(CheckChatInviteRequest(invite_hash)), 25
            )
            target = getattr(checked, "chat", None)
            if target is None:
                joined = await asyncio.wait_for(
                    account.client(ImportChatInviteRequest(invite_hash)), 30
                )
                chats = getattr(joined, "chats", None) or []
                if not chats:
                    raise RuntimeError("汇报账号尚未成功加入目标群")
                target = chats[0]
        else:
            key: Any = int(selector) if selector.lstrip("-").isdigit() else selector
            target = await asyncio.wait_for(account.client.get_entity(key), 25)
        self.report_target_cache[account.account_id] = target
        return target

    async def build_half_hour_report(self) -> list[str]:
        now = datetime.now().astimezone()
        group_rows: list[tuple[AccountContext, dict[str, Any]]] = []
        for account in self.accounts.values():
            if account.manager is None:
                continue
            for runner in account.manager.runners.values():
                group_rows.append((account, await runner.dashboard_state()))

        known_balance = Decimal("0")
        known_balance_groups = 0
        turnover = Decimal("0")
        targets = Decimal("0")
        lifetime = Decimal("0")
        settled = wins = losses = ties = pending = 0
        running = paused = reached = 0
        for _, state in group_rows:
            balance_value = state.get("current_balance")
            if balance_value is None:
                balance_value = (state.get("latest") or {}).get("balance")
            if balance_value is not None:
                known_balance += Decimal(str(balance_value))
                known_balance_groups += 1
            turnover += Decimal(str(state.get("turnover") or 0))
            targets += Decimal(str(state.get("target_turnover") or 0))
            lifetime += Decimal(str(state.get("lifetime_turnover") or 0))
            summary = state.get("summary") or {}
            settled += int(summary.get("settled") or 0)
            wins += int(summary.get("wins") or 0)
            losses += int(summary.get("losses") or 0)
            ties += int(summary.get("ties") or 0)
            pending += sum(
                row.get("status") in {
                    "sending", "sent", "ack_timeout", "result_timeout"
                }
                for row in state.get("rounds") or []
            )
            if state.get("turnover_reached"):
                reached += 1
            elif state.get("paused"):
                paused += 1
            else:
                running += 1

        connected = sum(account.status == "connected" for account in self.accounts.values())
        lines = [
            f"📊 半小时整体数据汇报 · {now:%Y-%m-%d %H:%M}",
            f"账号：{connected}/{len(self.accounts)} 已连接｜群：{len(group_rows)} 个",
            f"群状态：运行 {running}｜暂停/等待 {paused}｜达到目标 {reached}",
            f"本轮流水：{turnover} / {targets}｜历史流水：{lifetime}",
            (
                f"已知群余额合计：{known_balance}（{known_balance_groups} 个群有余额数据）"
                if known_balance_groups else "已知群余额合计：暂无机器人余额数据"
            ),
            f"已结算：{settled}｜中 {wins}｜未中 {losses}｜持平 {ties}｜待确认 {pending}",
        ]
        if self.report_include_details:
            lines.extend(["", "各群明细："])
            for account, state in group_rows:
                status = (
                    "达标" if state.get("turnover_reached")
                    else "暂停" if state.get("paused") else "运行"
                )
                balance = state.get("current_balance")
                if balance is None:
                    balance = (state.get("latest") or {}).get("balance")
                summary = state.get("summary") or {}
                lines.append(
                    f"• {account.label} / {state['group_name']}｜{status}｜"
                    f"余额 {balance if balance is not None else '未知'}｜"
                    f"流水 {state.get('turnover', 0)}/{state.get('target_turnover', 0)}｜"
                    f"结算 {summary.get('settled', 0)} 局｜净额 {summary.get('total_net', 0)}"
                )
            if not group_rows:
                lines.append("• 当前没有已配置群")

        chunks: list[str] = []
        current = ""
        for line in lines:
            candidate = line if not current else current + "\n" + line
            if len(candidate) > 3500 and current:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    async def send_half_hour_report(self) -> bool:
        async with self.report_lock:
            return await self._send_half_hour_report_locked()

    async def _send_half_hour_report_locked(self) -> bool:
        account = self.reporting_account()
        if account is None:
            self.report_status = f"未找到已登录账号 @{self.report_sender_username}"
            return False
        try:
            if hasattr(account.client, "is_connected") and not account.client.is_connected():
                await asyncio.wait_for(account.client.connect(), 25)
            target = await self.resolve_report_target(account)
            chunks = await self.build_half_hour_report()
            for chunk in chunks:
                await asyncio.wait_for(account.client.send_message(target, chunk), 30)
            self.last_report_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.report_status = f"已通过 @{self.report_sender_username} 汇报成功"
            if account.manager is not None:
                account.manager.control_store.log_event(
                    "half_hour_report", text=self.report_status
                )
            return True
        except Exception as exc:
            self.report_status = f"汇报失败，将在下个时段重试：{type(exc).__name__}: {exc}"
            if account.manager is not None:
                account.manager.control_store.log_event(
                    "half_hour_report_error", text=self.report_status
                )
            return False

    def start_half_hour_reporter(self) -> None:
        if self.report_task and not self.report_task.done():
            return
        if not self.report_enabled:
            self.report_status = "定时汇报已关闭"
            self.next_report_at = None
            return
        if self.reporting_account() is None:
            self.report_status = f"未找到已登录账号 @{self.report_sender_username}"

        async def loop() -> None:
            while True:
                scheduled = next_half_hour()
                self.next_report_at = scheduled.isoformat(timespec="seconds")
                delay = max(0.1, (scheduled - datetime.now().astimezone()).total_seconds())
                await asyncio.sleep(delay)
                if self.report_enabled:
                    await self.send_half_hour_report()

        self.report_task = asyncio.create_task(loop(), name="half-hour-summary-reporter")

    async def stop_half_hour_reporter(self) -> None:
        task = self.report_task
        self.report_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def save_report_settings(self, payload: dict[str, Any]) -> None:
        enabled = bool(payload.get("report_enabled"))
        include_details = bool(payload.get("report_include_details"))
        sender = str(payload.get("report_sender_username") or "").strip()
        target = str(payload.get("report_chat_selector") or "").strip()
        if not REPORT_USERNAME_RE.fullmatch(sender):
            raise ValueError("请选择一个带 @用户名的已登录 Telegram 账号")
        if not (
            INVITE_RE.search(target)
            or REPORT_USERNAME_RE.fullmatch(target)
            or target.lstrip("-").isdigit()
        ):
            raise ValueError("目标群必须填写邀请链接、@用户名或数字群 ID")
        self.report_enabled = enabled
        self.report_include_details = include_details
        self.report_sender_username = sender.lstrip("@")
        self.report_chat_selector = target
        self.report_target_cache.clear()
        self.settings_store.save_setting(
            "half_hour_report_enabled", "1" if enabled else "0"
        )
        self.settings_store.save_setting(
            "half_hour_report_include_details", "1" if include_details else "0"
        )
        self.settings_store.save_setting(
            "half_hour_report_sender", self.report_sender_username
        )
        self.settings_store.save_setting("half_hour_report_chat", target)
        await self.stop_half_hour_reporter()
        if enabled:
            self.report_status = "设置已保存，等待下一个整点或半点"
            self.start_half_hour_reporter()
        else:
            self.report_status = "定时汇报已关闭"
            self.next_report_at = None

    async def watchdog_once(self) -> None:
        self.last_watchdog_at = datetime.now().astimezone().isoformat(timespec="seconds")

        async def check(account: AccountContext) -> None:
            if account.manager is None:
                return
            try:
                if account.client is not None and hasattr(account.client, "is_connected"):
                    if not account.client.is_connected():
                        await asyncio.wait_for(account.client.connect(), 25)
                await account.manager.watchdog_once()
                account.status = "connected"
                account.error = None
            except Exception as exc:
                account.status = "error"
                account.error = f"自动检查失败，将重试：{type(exc).__name__}"

        await asyncio.gather(*(check(account) for account in self.accounts.values()))

    def start_watchdog(self) -> None:
        if self.watchdog_task and not self.watchdog_task.done():
            return

        async def loop() -> None:
            while True:
                await asyncio.sleep(self.watchdog_interval)
                await self.watchdog_once()

        self.watchdog_task = asyncio.create_task(loop(), name="all-groups-watchdog")

    async def stop_watchdog(self) -> None:
        task = self.watchdog_task
        self.watchdog_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def dashboard_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "begin_add_account":
            return await self.begin_add_account(payload)
        if action == "complete_add_account":
            return await self.complete_add_account(payload)
        if action == "cancel_add_account":
            return await self.cancel_add_account(payload)
        if action == "remove_account":
            return await self.remove_account(payload)
        if action == "save_report_settings":
            await self.save_report_settings(payload)
            return await self.dashboard_state()
        if action == "send_report_now":
            if not await self.send_half_hour_report():
                raise ValueError(self.report_status)
            return await self.dashboard_state()
        if action == "stop":
            for account in self.accounts.values():
                if account.manager:
                    for runner in account.manager.runners.values():
                        runner.set_run_requested(False)
                        runner.automatic_pause("manual")
                        account.manager.save_settings(runner)
            loop = asyncio.get_running_loop()
            self.shutdown_event.set()
            loop.call_later(0.3, lambda: asyncio.create_task(self.disconnect_all()))
            return await self.dashboard_state()

        account_id = str(payload.get("account_id") or "default")
        account = self.accounts.get(account_id)
        if account is None:
            raise ValueError("请选择要操作的 Telegram 账号")
        if account.manager is None:
            raise ValueError(account.error or "该账号当前未连接")
        result = await account.manager.dashboard_action(action, payload)
        state = await self.dashboard_state()
        # Keep the selected account's group list at the top level for action
        # responses too.  Older dashboard pages still read ``data.groups``
        # after clicking "添加群", while the normal multi-account state keeps
        # groups inside ``data.accounts``.
        for key in (
            "groups", "available_groups", "bot_candidates",
            "removed_group_id", "archived_group_path",
        ):
            if key in result:
                state[key] = result[key]
        return state

    async def begin_add_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        phone = normalize_phone(str(payload.get("phone", "")))
        label = str(payload.get("label") or "").strip()[:40]
        token = secrets.token_urlsafe(24)
        account_id = "acct_" + secrets.token_hex(6)
        folder = self.accounts_root / account_id
        folder.mkdir(parents=True, exist_ok=False)
        client = TelegramClient(
            str(folder / "telegram"),
            self.shared_api_id,
            self.shared_api_hash,
            proxy=self.proxy,
            sequential_updates=False,
        )
        try:
            await asyncio.wait_for(client.connect(), 25)
            sent = await asyncio.wait_for(client.send_code_request(phone), 30)
        except FloodWaitError as exc:
            await client.disconnect()
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError(f"Telegram 限制过于频繁，请等待 {exc.seconds} 秒后重试") from None
        except ApiIdInvalidError:
            await client.disconnect()
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError("API ID 或 API Hash 无效") from None
        except PhoneNumberInvalidError:
            await client.disconnect()
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError("Telegram 不接受这个手机号，请检查国家区号") from None
        except Exception:
            await client.disconnect()
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self.pending[token] = {
            "account_id": account_id,
            "folder": folder,
            "client": client,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "label": label,
            "stage": "code",
        }
        state = await self.dashboard_state()
        state["login_pending"] = {
            "token": token,
            "stage": "code",
            "phone_mask": mask_phone(phone),
        }
        return state

    async def complete_add_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("login_token") or "")
        pending = self.pending.get(token)
        if pending is None:
            raise ValueError("登录步骤已失效，请重新添加账号")
        client = pending["client"]
        try:
            if pending["stage"] == "code":
                code = re.sub(r"\D", "", str(payload.get("code") or ""))
                if not code:
                    raise ValueError("请输入 Telegram 验证码")
                try:
                    await client.sign_in(
                        pending["phone"],
                        code,
                        phone_code_hash=pending["phone_code_hash"],
                    )
                except SessionPasswordNeededError:
                    pending["stage"] = "password"
                    state = await self.dashboard_state()
                    state["login_pending"] = {
                        "token": token,
                        "stage": "password",
                        "phone_mask": mask_phone(pending["phone"]),
                    }
                    return state
                except PhoneCodeInvalidError:
                    raise ValueError("验证码不正确，请重新输入") from None
                except PhoneCodeExpiredError:
                    raise ValueError("验证码已过期，请取消后重新添加") from None
            else:
                password = str(payload.get("password") or "")
                if not password:
                    raise ValueError("请输入 Telegram 两步验证密码")
                try:
                    await client.sign_in(password=password)
                except PasswordHashInvalidError:
                    raise ValueError("两步验证密码不正确") from None
        except FloodWaitError as exc:
            raise ValueError(f"操作过于频繁，请等待 {exc.seconds} 秒后重试") from None

        me = await client.get_me()
        telegram_id = int(me.id)
        if any(account.telegram_user_id == telegram_id for account in self.accounts.values()):
            await self._discard_pending(token)
            raise ValueError("这个 Telegram 账号已经添加过")
        folder = pending["folder"]
        manager = MultiGroupManager(
            client,
            None,
            self.runner_factory,
            self.store_factory,
            root_dir=folder,
            live=self.live,
            ack_timeout=self.ack_timeout,
        )
        await manager.load()
        client.add_event_handler(manager.handle_message, events.NewMessage())
        account_id = pending["account_id"]
        display_name = utils.get_display_name(me)
        label = pending["label"] or display_name or f"账号 {telegram_id}"
        config = {
            "account_id": account_id,
            "label": label,
            "display_name": display_name,
            "username": getattr(me, "username", None),
            "phone_mask": mask_phone(getattr(me, "phone", pending["phone"])),
            "telegram_user_id": telegram_id,
        }
        self.saved_configs.append(config)
        self._save_configs()
        self.accounts[account_id] = AccountContext(
            account_id=account_id,
            label=label,
            display_name=display_name,
            username=getattr(me, "username", None),
            phone_mask=config["phone_mask"],
            telegram_user_id=telegram_id,
            client=client,
            manager=manager,
            can_delete=True,
        )
        self.pending.pop(token, None)
        state = await self.dashboard_state()
        state["login_pending"] = {
            "stage": "done",
            "account_id": account_id,
        }
        return state

    async def _discard_pending(self, token: str) -> None:
        pending = self.pending.pop(token, None)
        if not pending:
            return
        await pending["client"].disconnect()
        shutil.rmtree(pending["folder"], ignore_errors=True)

    async def cancel_add_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self._discard_pending(str(payload.get("login_token") or ""))
        return await self.dashboard_state()

    async def remove_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = str(payload.get("account_id") or "")
        account = self.accounts.get(account_id)
        if account is None:
            raise ValueError("账号不存在")
        if not account.can_delete:
            raise ValueError("默认账号不能移除，以免影响现有数据")
        async with self.lock:
            if account.manager is not None:
                await account.manager.close()
            if account.client is not None:
                await account.client.disconnect()
            config = next(
                (item for item in self.saved_configs if item["account_id"] == account_id),
                None,
            )
            folder = self.accounts_root / account_id
            if folder.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                target = self.archive_root / f"{stamp}-{account_id}"
                target.parent.mkdir(parents=True, exist_ok=True)
                if config is not None:
                    (folder / "account.json").write_text(
                        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                shutil.move(str(folder), str(target))
            self.saved_configs = [
                item for item in self.saved_configs if item["account_id"] != account_id
            ]
            self._save_configs()
            self.accounts.pop(account_id, None)
        state = await self.dashboard_state()
        state["removed_account_id"] = account_id
        return state

    async def close(self) -> None:
        await self.stop_half_hour_reporter()
        await self.stop_watchdog()
        for token in list(self.pending):
            await self._discard_pending(token)
        for account in self.accounts.values():
            if account.manager is not None:
                await account.manager.close()
            if account.client is not None and account.account_id != "default":
                await account.client.disconnect()

    async def wait_until_shutdown(self) -> None:
        await self.shutdown_event.wait()

    async def disconnect_all(self) -> None:
        await asyncio.gather(
            *(
                account.client.disconnect()
                for account in self.accounts.values()
                if account.client is not None
            ),
            return_exceptions=True,
        )

