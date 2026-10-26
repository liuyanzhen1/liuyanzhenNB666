import unittest
from decimal import Decimal
from types import SimpleNamespace

from parsers import (
    is_big,
    normalize_game_mode,
    parse_ack,
    parse_close,
    parse_open,
    parse_result,
    telegram_dice_value,
)


class ParserTests(unittest.TestCase):
    def test_native_telegram_dice_and_game_modes(self):
        dice = SimpleNamespace(media=SimpleNamespace(value=5, emoticon="🎲"))
        darts = SimpleNamespace(media=SimpleNamespace(value=5, emoticon="🎯"))
        self.assertEqual(telegram_dice_value(dice), 5)
        self.assertIsNone(telegram_dice_value(darts))
        self.assertEqual(normalize_game_mode("instant_dice"), "instant_dice")
        with self.assertRaises(ValueError):
            normalize_game_mode("unknown")

    def test_open(self):
        text = "期号: SL654928300500962\n发包金额: 1u\n倍数: 2.0"
        self.assertEqual(parse_open(text), "SL654928300500962")

    def test_close(self):
        text = "已封盘 ----SL654928300500961期下注玩家-----"
        self.assertEqual(parse_close(text), "SL654928300500961")

    def test_result(self):
        text = "❤️第SL654928300500960期输赢 ❤️\n骰子为: 5"
        self.assertEqual(parse_result(text), ("SL654928300500960", 5))
        self.assertTrue(is_big(5))
        self.assertFalse(is_big(3))

    def test_success_ack(self):
        ack = parse_ack("大10 下注成功\n余额: 5000.07 (冻结: 10)")
        self.assertIsNotNone(ack)
        self.assertTrue(ack.accepted)
        self.assertEqual(ack.balance, Decimal("5000.07"))
        self.assertEqual(ack.frozen, Decimal("10"))

    def test_rejected_ack(self):
        ack = parse_ack("大10 投注无效, 已封盘")
        self.assertIsNotNone(ack)
        self.assertTrue(ack.rejected)


if __name__ == "__main__":
    unittest.main()

