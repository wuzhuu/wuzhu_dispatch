from __future__ import annotations

FORBIDDEN_WORDS = ("必涨", "保证收益", "确定买入", "满仓", "梭哈", "目标价保证")


def strip_forbidden(text: str) -> str:
    for word in FORBIDDEN_WORDS:
        text = text.replace(word, "")
    return text
