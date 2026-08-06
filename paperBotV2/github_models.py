"""Chinese translation helpers for daily digests.

The module keeps its historical name so existing imports remain compatible.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

import requests


TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")


def _translate_chunk(text: str, timeout: int) -> str:
    response = requests.get(
        TRANSLATE_URL,
        params={
            "client": "gtx",
            "sl": "auto",
            "tl": "zh-CN",
            "dt": "t",
            "q": text,
        },
        headers={"User-Agent": "Algorithm-Practice-in-Industry/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    try:
        translated = "".join(
            str(segment[0]) for segment in response.json()[0] if segment and segment[0]
        ).strip()
    except (IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("translation service returned an invalid payload") from exc
    if not translated or not CHINESE_RE.search(translated):
        raise RuntimeError("translation service did not return Chinese text")
    return translated


def _translate(text: str, timeout: int) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return ""
    if CHINESE_RE.search(text) and len(CHINESE_RE.findall(text)) >= len(text) // 3:
        return text

    # Keep request URLs comfortably below common proxy limits.
    chunks = [text[index : index + 1000] for index in range(0, len(text), 1000)]
    return "".join(_translate_chunk(chunk, timeout) for chunk in chunks)


def _concise_summary(text: str, title_zh: str, limit: int = 160) -> str:
    if not text:
        return f"仅根据标题：本文围绕“{title_zh}”展开研究，详细内容请查看论文原文。"

    sentences = [part.strip() for part in re.split(r"(?<=[。！？])", text) if part.strip()]
    chosen = ""
    for sentence in sentences[:3]:
        if len(chosen) + len(sentence) > limit:
            break
        chosen += sentence
    if chosen:
        return chosen
    return text[: limit - 1].rstrip("，。；： ") + "…"


def _enrich_one(item: dict, timeout: int) -> tuple[str, dict]:
    item_id = str(item["id"])
    title_zh = _translate(str(item.get("title", ""))[:500], timeout)
    source_summary = str(item.get("summary", ""))[:1800]
    summary_zh = _concise_summary(_translate(source_summary, timeout), title_zh)
    return item_id, {"title_zh": title_zh, "summary_zh": summary_zh}


def enrich_chinese(items: Iterable[dict], *, timeout: int = 30) -> dict[str, dict]:
    """Return Chinese titles and concise Chinese summaries keyed by item id."""
    source_items = [dict(item) for item in items]
    if not source_items:
        return {}
    with ThreadPoolExecutor(max_workers=min(4, len(source_items))) as executor:
        translated = list(executor.map(lambda item: _enrich_one(item, timeout), source_items))
    return dict(translated)
