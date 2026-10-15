"""订阅推送的全局关键词筛选，不依赖数据源或消息发送方式。"""

import re
import unicodedata
from typing import Any


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


class TweetContentFilter:
    """按字面量检查正文及引用原文；空配置保持原有推送行为。"""

    def __init__(self, keywords: Any = (), match_mode: str = "contains") -> None:
        # 兼容手工配置的换行文本，避免把字符串拆成逐个字符匹配。
        if isinstance(keywords, str):
            keywords = keywords.splitlines()
        if not isinstance(keywords, (list, tuple)):
            keywords = ()
        entries: dict[str, str] = {}
        for keyword in keywords:
            if isinstance(keyword, str):
                normalized = _normalize(keyword).strip()
                if normalized:
                    entries.setdefault(normalized, keyword.strip())
        self._keywords = tuple(entries.items())
        self.match_mode = "word" if match_mode == "word" else "contains"
        self._patterns = tuple(
            re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)")
            for keyword, _label in self._keywords
        ) if self.match_mode == "word" else ()

    @property
    def enabled(self) -> bool:
        return bool(self._keywords)

    def match(self, tweet_info: dict) -> str | None:
        """返回首个命中的配置词；不匹配作者、媒体地址或翻译生成的文本。"""
        if not self.enabled:
            return None
        parts = [tweet_info.get("text")]
        quote = tweet_info.get("quote")
        if isinstance(quote, dict):
            parts.append(quote.get("text"))
        texts = tuple(_normalize(part) for part in parts if isinstance(part, str))
        for index, (keyword, label) in enumerate(self._keywords):
            for text in texts:
                matched = (
                    self._patterns[index].search(text)
                    if self.match_mode == "word" else keyword in text
                )
                if matched:
                    return label
        return None
