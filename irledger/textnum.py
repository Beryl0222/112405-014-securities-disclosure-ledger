"""中文数字、百分数与金额单位解析。

所有金额最终统一为“元”的有理数（:class:`fractions.Fraction`），
比较时不经过浮点，避免单位换算后出现尾差。
"""
from __future__ import annotations

import re
from fractions import Fraction

# 货币单位到元的换算因子（合成演练口径，只含本资料出现的单位）。
UNIT_TO_YUAN: dict[str, Fraction] = {
    "元": Fraction(1),
    "万元": Fraction(10_000),
    "百万元": Fraction(1_000_000),
    "亿元": Fraction(100_000_000),
}

# 固定/报告汇率等非金额数量使用的空单位。
DIMENSIONLESS = ""

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG_UNITS = {"万": 10_000, "亿": 100_000_000}

# 成数：一成 = 10%。
_CHENG_RATIO = Fraction(1, 10)
# 百分比关键字映射。
_PERCENT_WORDS = {"翻倍": Fraction(2, 1), "翻番": Fraction(2, 1)}

# 数字+可选单位的抽取正则（阿拉伯数字）。
_NUMBER_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>亿元|百万元|万元|元|%)?"
)
# 中文数字+货币单位（如 四亿元、两万万元），单位词不并入数字部分。
_CN_AMOUNT_RE = re.compile(
    r"(?P<num>[零〇一二两三四五六七八九十百千万]+?)\s*(?P<unit>亿元|百万元|万元|元)"
)
# 中文成数：增长两成、增两成、下降两成等。
_CHENG_RE = re.compile(r"(?P<dir>增长|增加|增|上升|下降|减少|降|下跌)\s*(?P<num>[零〇一二两三四五六七八九])成")
_PERCENT_RE = re.compile(r"(?P<dir>增长|增加|增|上升|下降|减少|降|下跌)\s*(?P<num>\d+(?:\.\d+)?)\s*%")


def parse_chinese_number(token: str) -> int:
    """解析简单中文整数（支持到亿级，含“两成”这类语料用字）。

    仅支持语料中出现的简单写法；无法解析时抛出 :class:`ValueError`。
    """
    token = token.strip()
    if not token:
        raise ValueError("空数字")
    if token.isdigit():
        return int(token)
    total = 0
    section = 0  # 当前万/亿小节内累计
    number = 0  # 小节内尚未落到单位的数字
    for ch in token:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_SMALL_UNITS:
            section += (number or 1) * _CN_SMALL_UNITS[ch]
            number = 0
        elif ch in _CN_BIG_UNITS:
            section += number
            factor = _CN_BIG_UNITS[ch]
            total = (total + section) * factor if ch == "万" else total + section * factor
            section = 0
            number = 0
        else:
            raise ValueError(f"无法识别的中文数字字符: {ch}")
    return total + section + number


def ratio_from_cheng(phrase: str) -> Fraction | None:
    """从“增长两成/下降两成”类短语取比率，下降返回负值；未命中返回 None。"""
    match = _CHENG_RE.search(phrase)
    if not match:
        return None
    value = parse_chinese_number(match.group("num")) * _CHENG_RATIO
    if match.group("dir") in {"下降", "减少", "降", "下跌"}:
        value = -value
    return value


def ratio_from_percent(phrase: str) -> Fraction | None:
    """从“增长20%/下降14%”类短语取比率；未命中返回 None。"""
    match = _PERCENT_RE.search(phrase)
    if not match:
        return None
    value = Fraction(match.group("num")) / 100
    if match.group("dir") in {"下降", "减少", "降", "下跌"}:
        value = -value
    return None if value is None else value


def word_ratio(phrase: str) -> Fraction | None:
    """识别“翻倍/翻番”等固定比率词。"""
    for word, ratio in _PERCENT_WORDS.items():
        if word in phrase:
            return ratio
    return None


def extract_ratio(phrase: str) -> Fraction | None:
    """按成数、百分比、固定词的顺序抽取变化比率。"""
    return ratio_from_cheng(phrase) or ratio_from_percent(phrase) or word_ratio(phrase)


def to_yuan(amount: Fraction | int | float, unit: str) -> Fraction:
    """把带单位的金额换算为元。单位必须是已登记的货币单位。"""
    if unit not in UNIT_TO_YUAN:
        raise ValueError(f"未登记的金额单位，禁止借用: {unit!r}")
    return Fraction(amount) * UNIT_TO_YUAN[unit]


def extract_amounts(phrase: str, default_unit: str | None = None) -> list[tuple[Fraction, str]]:
    """抽取短语中的“数字+货币单位”金额（支持阿拉伯数字与简单中文数字）。

    单元（表内省略单位）由调用方通过 ``default_unit`` 显式传入继承值；
    本函数不会自行猜测或跨表借单位。
    返回 ``(以元计金额, 原单位)`` 列表。
    """
    found: list[tuple[Fraction, str]] = []
    for match in _NUMBER_RE.finditer(phrase):
        unit = match.group("unit") or default_unit
        if unit is None or unit == "%" or unit not in UNIT_TO_YUAN:
            continue
        found.append((to_yuan(Fraction(match.group("num")), unit), unit))
    for match in _CN_AMOUNT_RE.finditer(phrase):
        unit = match.group("unit")
        if unit not in UNIT_TO_YUAN:
            continue
        number = parse_chinese_number(match.group("num"))
        found.append((to_yuan(Fraction(number), unit), unit))
    return found


def format_yuan(amount: Fraction) -> str:
    """把以元计的金额格式化为可读字符串（整数不带小数点）。"""
    if amount.denominator == 1:
        return f"{amount.numerator}元"
    return f"{float(amount):.4f}元".rstrip("0").rstrip(".") + "元"


def format_percent(ratio: Fraction) -> str:
    """比率格式化为百分比字符串，保留精确分数语义。"""
    percent = ratio * 100
    if percent.denominator == 1:
        return f"{percent.numerator}%"
    return f"{float(percent):.2f}%"
