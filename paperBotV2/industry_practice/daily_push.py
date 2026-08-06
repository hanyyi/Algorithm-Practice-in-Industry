"""Push a rotating selection of industry-practice articles to Feishu."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Iterable

from paperBotV2.feishu import send_card


DEFAULT_DATA = Path(__file__).resolve().parent / "data" / "article.json"
ROTATION_EPOCH = date(2026, 1, 1)


def load_articles(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"expected an article list in {path}")
    normalized = []
    for item in data:
        if not isinstance(item, dict):
            continue
        tags = item.get("tags", item.get("标签", []))
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        normalized.append(
            {
                "company": item.get("company", item.get("公司", "未知公司")),
                "link": item.get("link", item.get("链接", "")),
                "title": item.get("title", item.get("内容", item.get("标题", ""))),
                "tags": tags or [],
                "date": item.get("date", item.get("时间", item.get("日期", ""))),
            }
        )
    return normalized


def _stable_key(item: dict) -> str:
    identity = f"{item.get('link', '')}\n{item.get('title', '')}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def select_articles(
    articles: Iterable[dict], push_date: date, limit: int, tags: set[str] | None = None
) -> list[dict]:
    filtered = []
    for item in articles:
        item_tags = {str(tag).strip() for tag in item.get("tags", [])}
        if tags and not (item_tags & tags):
            continue
        if item.get("title") and item.get("link"):
            filtered.append(item)

    if not filtered or limit <= 0:
        return []

    # A stable shuffled order plus a date-derived offset gives variety without
    # committing mutable delivery state back into the repository.
    filtered.sort(key=_stable_key)
    start = ((push_date - ROTATION_EPOCH).days * limit) % len(filtered)
    take = min(limit, len(filtered))
    return [filtered[(start + offset) % len(filtered)] for offset in range(take)]


def build_markdown(items: Iterable[dict]) -> str:
    blocks = []
    for item in items:
        tags = " / ".join(str(tag) for tag in item.get("tags", [])) or "未分类"
        meta = " · ".join(
            value
            for value in [str(item.get("company", "未知公司")), tags, str(item.get("date", ""))]
            if value
        )
        blocks.append(f"**{meta}**\n[{item['title']}]({item['link']})")
    return "\n\n---\n\n".join(blocks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push daily industry articles to Feishu")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--limit", type=int, default=int(os.environ.get("INDUSTRY_LIMIT", "5")))
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--tags", default=os.environ.get("INDUSTRY_TAGS", ""))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    wanted_tags = {tag.strip() for tag in args.tags.split(",") if tag.strip()} or None
    selected = select_articles(load_articles(args.data), args.date, args.limit, wanted_tags)
    if not selected:
        raise RuntimeError("no industry-practice articles matched the configured filters")

    send_card(
        f"🏭 行业实践日推 · {args.date.isoformat()}",
        build_markdown(selected),
        color="turquoise",
        dry_run=args.dry_run,
    )
    print(f"Selected {len(selected)} industry-practice articles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
