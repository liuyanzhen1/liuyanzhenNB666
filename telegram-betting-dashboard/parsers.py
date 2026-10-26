from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


ISSUE_RE = re.compile(r"期号\s*[:：]\s*(SL\d+)", re.IGNORECASE)
CLOSE_RE = re.compile(r"已封盘[\s\S]*?(SL\d+)\s*期", re.IGNORECASE)
RESULT_RE = re.compile(
    r"第\s*(SL\d+)\s*期输赢[\s\S]*?骰子为\s*[:：]\s*([1-6])",
    re.IGNORECASE,
)
BALANCE_RE = re.compile(r"余额\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)")
FROZEN_RE = re.compile(r"冻结\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)")
BET_SIDE_ORDER = ("大", "小", "单", "双")
GAME_MODES = ("scheduled", "instant_dice")


@dataclass(frozen=True)
class Ack:
    accepted: bool
    rejected: bool
    balance: Decimal | None
    frozen: Decimal | None


def normalize(text: str) -> str:
    return text.replace("\u2068", "").replace("\u2069", "").strip()


def parse_open(text: str) -> str | None:
    match = ISSUE_RE.search(normalize(text))
    return match.group(1).upper() if match else None


def parse_close(text: str) -> str | None:
    match = CLOSE_RE.search(normalize(text))
    return match.group(1).upper() if match else None


def parse_result(text: str) -> tuple[str, int] | None:
    match = RESULT_RE.search(normalize(text))
    return (match.group(1).upper(), int(match.group(2))) if match else None


def telegram_dice_value(message: Any) -> int | None:
    """Return a native Telegram dice value, while ignoring other emoji games."""
    media = getattr(message, "media", None)
    if media is None:
        media = getattr(message, "dice", None)
    value = getattr(media, "value", None)
    emoticon = getattr(media, "emoticon", None)
    if emoticon == "🎲" and isinstance(value, int) and 1 <= value <= 6:
        return value
    return None


def _decimal(match: re.Match[str] | None) -> Decimal | None:
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


def parse_ack(text: str) -> Ack | None:
    value = normalize(text)
    accepted = "下注成功" in value
    rejected = "投注无效" in value or "下注失败" in value
    if not accepted and not rejected:
        return None
    return Ack(
        accepted=accepted,
        rejected=rejected,
        balance=_decimal(BALANCE_RE.search(value)),
        frozen=_decimal(FROZEN_RE.search(value)),
    )


def is_big(dice: int) -> bool:
    if dice not in range(1, 7):
        raise ValueError("骰子必须是 1 到 6")
    return dice >= 4


def normalize_bet_sides(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [part for part in values.replace("，", ",").replace(" ", ",").split(",") if part]
    if not isinstance(values, (list, tuple, set)):
        raise ValueError("下注选项必须是大、小、单、双中的至少一项")
    selected = {str(value).strip() for value in values}
    if not selected or not selected.issubset(BET_SIDE_ORDER):
        raise ValueError("下注选项必须是大、小、单、双中的至少一项")
    return [side for side in BET_SIDE_ORDER if side in selected]


def normalize_game_mode(value: Any) -> str:
    mode = str(value or "scheduled").strip().lower()
    if mode not in GAME_MODES:
        raise ValueError("开奖方式必须是定时开盘或下注后掷骰")
    return mode


def result_sides(dice: int) -> list[str]:
    return ["大" if is_big(dice) else "小", "单" if dice % 2 else "双"]

