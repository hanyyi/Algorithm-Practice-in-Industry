"""Chinese translation and summarization through GitHub Models."""

from __future__ import annotations

import json
import os
import re
from typing import Iterable

import requests


MODELS_URL = "https://models.github.ai/inference/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o"


def _json_payload(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def enrich_chinese(
    items: Iterable[dict],
    *,
    token: str | None = None,
    model: str | None = None,
    timeout: int = 90,
) -> dict[str, dict]:
    """Return concise Chinese titles and summaries keyed by the supplied item id."""
    source_items = []
    for item in items:
        source_items.append(
            {
                "id": str(item["id"]),
                "title": str(item.get("title", ""))[:500],
                "summary": str(item.get("summary", ""))[:1800],
            }
        )
    if not source_items:
        return {}

    auth_token = token or os.environ.get("GITHUB_TOKEN", "")
    if not auth_token:
        raise RuntimeError("GITHUB_TOKEN is required for Chinese summaries")

    instructions = (
        "你是严谨的机器学习研究编辑。请把输入条目翻译并概括为简体中文。"
        "标题要准确、专业；摘要用2到3句话，不超过160个汉字，说明问题、方法和主要结论，"
        "不得补充原文没有的结果。若输入没有摘要，明确写‘仅根据标题：’后再概括。"
        "只输出合法JSON，格式必须是"
        '{"items":[{"id":"原id","title_zh":"中文标题","summary_zh":"中文摘要"}]}。'
    )
    response = requests.post(
        MODELS_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        json={
            "model": model or os.environ.get("GITHUB_MODEL", DEFAULT_MODEL),
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(source_items, ensure_ascii=False)},
            ],
        },
        timeout=timeout,
    )
    if not response.ok:
        raise RuntimeError(
            f"GitHub Models request failed: HTTP {response.status_code}: {response.text[:500]}"
        )

    try:
        content = response.json()["choices"][0]["message"]["content"]
        translated = _json_payload(content)["items"]
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("GitHub Models returned an invalid translation payload") from exc

    result = {}
    for item in translated:
        item_id = str(item.get("id", ""))
        title = str(item.get("title_zh", "")).strip()
        summary = str(item.get("summary_zh", "")).strip()
        if item_id and title and summary:
            result[item_id] = {"title_zh": title, "summary_zh": summary}

    missing = [item["id"] for item in source_items if item["id"] not in result]
    if missing:
        raise RuntimeError(f"GitHub Models omitted {len(missing)} translated items")
    return result
