"""
本地快速通道的严格匹配器。

和旧的关键词解析不同, 这里要求整句话从头到尾都能被已知句式覆盖:
一句话 = [语气词/连接词]* 命令 ([连接词]* 命令)* [重复次数] [语气词]*
任何一处匹配不上, 整句就判为不命中 (交给 LLM), 不会去猜。

用法:
    result = match("向前走三十厘米，然后左转90度")
    result.commands  -> ["forward 30", "left 90"]
    match("把红块搬到B区") -> None
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from pc.llm.cn_number import NUMBER_PATTERN, parse_number, round_half_up

MAX_COMMANDS = 30
MAX_REPEAT = 10

_NUM = rf"(?P<num>{NUMBER_PATTERN})"
_DIST_UNIT = r"(?P<unit>厘米|公分|cm|毫米|mm|米|m)"
_OPT_DIST_UNIT = rf"{_DIST_UNIT}?"
_ANGLE_UNIT = r"(?:度|°|deg)?"

_GRIP = r"(?:夹子|夹爪|爪子|抓手)"
_UP = r"(?:抬起来|抬起|抬高|升起来|升起|举起来|举起|向上)"
_DOWN = r"(?:放下来|放下|放低|降下来|降下|落下来|落下|向下)"
_TO = r"(?:转到|转动到|调到|调整到|设为|设到|到)"
_BOTH = rf"(?:两个|双|左右|左{_GRIP}和右)"
_LEFT = r"左(?:边|侧)?的?"
_RIGHT = r"右(?:边|侧)?的?"

_UNIT_FACTORS = {
    None: 1.0, "厘米": 1.0, "公分": 1.0, "cm": 1.0,
    "毫米": 0.1, "mm": 0.1,
    "米": 100.0, "m": 100.0,
}

_FILLER = re.compile(
    r"(?:请你|请|麻烦你|麻烦|帮我|你|小车|机器人"
    r"|吧|啊|呀|哦|了|一下|好吗|好不好|谢谢)"
)
_CONNECTOR = re.compile(
    r"(?:然后|之后|以后|紧接着|接着|随后|并且|同时|再|并|和|后|,|，|;|；|、)"
)
_REPEAT = re.compile(rf"(?:一共|总共)?(?:重复|做|来|执行)(?P<num>{NUMBER_PATTERN})(?:次|遍)")

_STOP_WORDS = r"(?:急停|停下来|停下|停止|停车|别动|停|stop)"


@dataclass
class MatchResult:
    commands: list[str]
    clauses: list[str] = field(default_factory=list)

    @property
    def is_emergency_stop(self) -> bool:
        """整句只由停止词组成 (可以重复、带语气词)。"""
        return bool(self.commands) and all(c == "stop" for c in self.commands)


Builder = Callable[[re.Match], "str | None"]


def _distance(action: str, default_unit: str | None = None) -> Builder:
    def build(m: re.Match) -> str | None:
        value = parse_number(m.group("num"))
        unit = m.groupdict().get("unit") or default_unit
        if value is None or unit not in _UNIT_FACTORS:
            return None
        return f"{action} {round_half_up(value * _UNIT_FACTORS[unit])}"
    return build


def _angle(action: str) -> Builder:
    def build(m: re.Match) -> str | None:
        value = parse_number(m.group("num"))
        if value is None:
            return None
        return f"{action} {round_half_up(value)}"
    return build


def _fixed(action: str) -> Builder:
    def build(_m: re.Match) -> str | None:
        return action
    return build


# (句式, 生成命令的函数)。中文句式要求距离必须带单位;
# 英文命令式写法 (forward 30) 本身就是机器人命令的格式, 不带单位时按厘米处理。
_PATTERNS: list[tuple[str, Builder]] = [
    # 前进 / 后退
    (rf"(?:(?:向|往|朝)?前(?:进|走|开|移动|移)?){_NUM}{_DIST_UNIT}", _distance("forward")),
    (rf"forward{_NUM}{_OPT_DIST_UNIT}", _distance("forward", default_unit="cm")),
    (rf"(?:(?:向|往|朝)后(?:退|走|移动|移)?|后退|倒退|倒车){_NUM}{_DIST_UNIT}",
     _distance("backward")),
    (rf"(?:backward|back){_NUM}{_OPT_DIST_UNIT}", _distance("backward", default_unit="cm")),
    # 转向
    (rf"(?:(?:向|往|朝)?左(?:转|拐)弯?){_NUM}{_ANGLE_UNIT}", _angle("left")),
    (rf"(?:turnleft|left){_NUM}{_ANGLE_UNIT}", _angle("left")),
    (rf"(?:(?:向|往|朝)?右(?:转|拐)弯?){_NUM}{_ANGLE_UNIT}", _angle("right")),
    (rf"(?:turnright|right){_NUM}{_ANGLE_UNIT}", _angle("right")),
    # 巡线
    (rf"沿?着?(?:黑线)?的?{_LEFT}(?:巡线|走|行走|前进){_NUM}{_DIST_UNIT}",
     _distance("line_follow_left")),
    (rf"line_?follow_?left{_NUM}{_OPT_DIST_UNIT}", _distance("line_follow_left", "cm")),
    (rf"沿?着?(?:黑线)?的?{_RIGHT}(?:巡线|走|行走|前进){_NUM}{_DIST_UNIT}",
     _distance("line_follow_right")),
    (rf"line_?follow_?right{_NUM}{_OPT_DIST_UNIT}", _distance("line_follow_right", "cm")),
    # 单个夹爪: 抬起 / 放下 / 转到角度
    (rf"把?{_LEFT}{_GRIP}{_UP}|{_UP}{_LEFT}{_GRIP}", _fixed("gripper_left_up")),
    (rf"把?{_LEFT}{_GRIP}{_DOWN}|{_DOWN}{_LEFT}{_GRIP}", _fixed("gripper_left_down")),
    (rf"把?{_RIGHT}{_GRIP}{_UP}|{_UP}{_RIGHT}{_GRIP}", _fixed("gripper_right_up")),
    (rf"把?{_RIGHT}{_GRIP}{_DOWN}|{_DOWN}{_RIGHT}{_GRIP}", _fixed("gripper_right_down")),
    (rf"把?{_LEFT}{_GRIP}{_TO}{_NUM}{_ANGLE_UNIT}", _angle("gripper_left_pos")),
    (rf"把?{_RIGHT}{_GRIP}{_TO}{_NUM}{_ANGLE_UNIT}", _angle("gripper_right_pos")),
    (r"gripper_?left_?up|left_?gripper_?up", _fixed("gripper_left_up")),
    (r"gripper_?left_?down|left_?gripper_?down", _fixed("gripper_left_down")),
    (r"gripper_?right_?up|right_?gripper_?up", _fixed("gripper_right_up")),
    (r"gripper_?right_?down|right_?gripper_?down", _fixed("gripper_right_down")),
    (rf"(?:gripper_?left_?pos|left_?gripper_?pos){_NUM}{_ANGLE_UNIT}", _angle("gripper_left_pos")),
    (rf"(?:gripper_?right_?pos|right_?gripper_?pos){_NUM}{_ANGLE_UNIT}",
     _angle("gripper_right_pos")),
    # 两个夹爪一起
    (rf"把?{_BOTH}?{_GRIP}都?{_UP}|{_UP}{_BOTH}?{_GRIP}", _fixed("gripper_up")),
    (rf"把?{_BOTH}?{_GRIP}都?{_DOWN}|{_DOWN}{_BOTH}?{_GRIP}", _fixed("gripper_down")),
    (rf"把?{_BOTH}?{_GRIP}都?{_TO}{_NUM}{_ANGLE_UNIT}", _angle("gripper_pos")),
    (r"gripper_?up", _fixed("gripper_up")),
    (r"gripper_?down", _fixed("gripper_down")),
    (rf"gripper_?pos{_NUM}{_ANGLE_UNIT}", _angle("gripper_pos")),
    # 停止
    (_STOP_WORDS, _fixed("stop")),
]

_COMPILED: list[tuple[re.Pattern, Builder]] = [(re.compile(p), b) for p, b in _PATTERNS]


def match(text: str) -> MatchResult | None:
    """整句匹配。所有部分都被已知句式覆盖时返回 MatchResult, 否则返回 None。"""
    s = _prepare(text)
    if not s:
        return None

    commands: list[str] = []
    clauses: list[str] = []
    pos = 0

    while pos < len(s):
        hit = _match_command(s, pos)
        if hit is not None:
            cmd, end = hit
            commands.append(cmd)
            clauses.append(s[pos:end])
            pos = end
            continue

        rep = _REPEAT.match(s, pos)
        if rep is not None and commands:
            times = parse_number(rep.group("num"))
            if times is None or not 1 <= times <= MAX_REPEAT or times != int(times):
                return None
            commands = commands * int(times)
            clauses = clauses * int(times)
            pos = rep.end()
            continue

        skip = _FILLER.match(s, pos) or _CONNECTOR.match(s, pos)
        if skip is not None:
            pos = skip.end()
            continue

        return None

    if not commands or len(commands) > MAX_COMMANDS:
        return None
    return MatchResult(commands=commands, clauses=clauses)


_CLAUSE_SPLIT = re.compile(
    r"[,，。;；、!！?？]+"
    r"|然后|之后|以后|紧接着|接着|随后"
    r"|(?<=米)后|(?<=公分)后|(?<=m)后|(?<=度)后|(?<=°)后"
    r"|再(?!见)"
)
_STOP_RE = re.compile(_STOP_WORDS)


def split_clauses(text: str) -> list[str]:
    """按连接词和标点把一段话拆成分句 (保持顺序, 去掉空分句)。"""
    s = (text or "").lower()
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", "，", s)
    s = re.sub(r"\s+", "", s)
    return [c for c in _CLAUSE_SPLIT.split(s) if c]


def match_repeat(clause: str) -> int | None:
    """分句是不是"一共做四次"这样的重复说法, 是的话返回次数。"""
    s = _prepare(clause)
    m = _REPEAT.fullmatch(s)
    if m is None:
        return None
    times = parse_number(m.group("num"))
    if times is None or times != int(times) or not 1 <= times <= MAX_REPEAT:
        return None
    return int(times)


def contains_stop_word(text: str) -> bool:
    """文字里是否出现停止词 (流式模式下用于在中间结果上立即急停, 宁可多停)。"""
    return _STOP_RE.search((text or "").lower()) is not None


def _match_command(s: str, pos: int) -> tuple[str, int] | None:
    """在 pos 处找最长的命令匹配。"""
    best: tuple[str, int] | None = None
    for pattern, build in _COMPILED:
        m = pattern.match(s, pos)
        if m is None or m.end() == pos:
            continue
        cmd = build(m)
        if cmd is None:
            continue
        if best is None or m.end() > best[1]:
            best = (cmd, m.end())
    return best


def _prepare(text: str) -> str:
    s = (text or "").lower()
    # 只保留数字之间的小数点; 其他标点去掉 (逗号、分号保留, 当作连接词)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", "", s)
    s = re.sub(r"[\s!！?？。~～…\"'“”‘’()（）\[\]【】]+", "", s)
    return s
