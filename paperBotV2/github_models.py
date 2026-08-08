"""Compatibility helpers for the repository's former translation interface.

Active daily digests use :mod:`paperBotV2.llm_enrichment` directly. This module keeps
the historical ``enrich_chinese`` import working without falling back to the old public
machine-translation endpoint.
"""

from __future__ import annotations

from typing import Iterable

from paperBotV2.llm_enrichment import generate_chinese_summaries


def enrich_chinese(items: Iterable[dict], *, timeout: int = 60) -> dict[str, dict]:
    """Return LLM-produced Chinese summaries keyed by the historical item id.

    Titles intentionally remain in their source language, so this compatibility result
    only contains ``summary_zh``.
    """

    source_items = [dict(item) for item in items]
    summaries = generate_chinese_summaries(source_items, timeout=timeout)
    return {
        str(item["id"]): {"summary_zh": summaries[str(item["id"])]}
        for item in source_items
        if str(item.get("id") or "") in summaries
    }
