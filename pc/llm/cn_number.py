"""
中文数字和阿拉伯数字解析。

支持:
- 阿拉伯数字: 30, 1.5
- 中文数字 0-9999: 三十, 十五, 两百, 一百二十, 一百零二, 两百五 (=250), 三千五 (=3500)
- 中文小数: 二十五点五

不支持的写法 (返回 None): 一二三 (连续数字), 十百 (单位顺序错误), 带其他字符。
"""

from __future__ import annotations

import math
import re

_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_UNITS = {"十": 10, "百": 100, "千": 1000}

# 在其他正则里使用的数字片段
NUMBER_PATTERN = (
    r"(?:\d+(?:\.\d+)?"
    r"|[零〇一二两三四五六七八九十百千]+(?:点[零〇一二三四五六七八九]+)?)"
)

_ARABIC_RE = re.compile(r"^\d+(?:\.\d+)?$")


def parse_number(text: str) -> float | None:
    s = (text or "").strip()
    if not s:
        return None

    if _ARABIC_RE.match(s):
        return float(s)

    if "点" in s:
        int_part, _, dec_part = s.partition("点")
        integer = _parse_cn_integer(int_part)
        if integer is None or not dec_part:
            return None
        decimals = []
        for ch in dec_part:
            if ch not in _DIGITS:
                return None
            decimals.append(str(_DIGITS[ch]))
        return float(f"{integer}.{''.join(decimals)}")

    value = _parse_cn_integer(s)
    return None if value is None else float(value)


def round_half_up(value: float) -> int:
    """四舍五入 (Python 的 round 是银行家舍入, 24.5 会变成 24)。"""
    return math.floor(value + 0.5)


def _parse_cn_integer(s: str) -> int | None:
    if not s:
        return None

    total = 0
    pending: int | None = None  # 还没乘单位的数字
    last_unit = 0  # 最近一个单位的值
    after_unit = False  # 上一个字符是否是单位
    digit_after_unit = False  # 末尾数字是否紧跟在单位后面 (用于 "两百五" 这种口语)

    for ch in s:
        if ch in _DIGITS:
            digit = _DIGITS[ch]
            if digit == 0:
                after_unit = False
                if pending is None:
                    pending = 0
                continue
            if pending:
                return None  # "一二" 这种连续数字不是规范写法
            pending = digit
            digit_after_unit = after_unit
            after_unit = False
        elif ch in _UNITS:
            unit = _UNITS[ch]
            if last_unit and unit >= last_unit:
                return None  # 单位必须从大到小, 比如 "十百" 不合法
            total += (pending if pending else 1) * unit
            pending = None
            last_unit = unit
            after_unit = True
        else:
            return None

    if pending:
        if digit_after_unit and last_unit >= 100:
            # 口语: 两百五 = 250, 三千五 = 3500
            total += pending * (last_unit // 10)
        else:
            total += pending

    return total
