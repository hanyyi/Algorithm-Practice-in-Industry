"""Push the newest relevant industry-engineering articles to Feishu."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

from paperBotV2.feishu import send_card
from paperBotV2.github_models import enrich_chinese


DEFAULT_DATA = Path(__file__).resolve().parent / "data" / "article.json"
DEFAULT_FEEDS = {
    "Netflix TechBlog": "https://netflixtechblog.com/feed",
    "Uber Engineering": "https://eng.uber.com/feed/",
    "Spotify Engineering": "https://engineering.atspotify.com/feed/",
    "GitHub Engineering": "https://github.blog/engineering/feed/",
    "Pinterest Engineering": "https://medium.com/feed/pinterest-engineering",
    "Airbnb Engineering": "https://medium.com/feed/airbnb-engineering",
}
INDUSTRY_KEYWORDS = (
    "recommend",
    "retrieval",
    "search",
    "ranking",
    "advertis",
    "personalization",
    "machine learning",
    "deep learning",
    "large language model",
    "llm",
    "generative ai",
    "agent",
    "data platform",
    "inference",
    "feature store",
)


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


def _plain_text(value: str) -> str:
    return " ".join(BeautifulSoup(value or "", "html.parser").get_text(" ").split())


def _entry_date(entry) -> str:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.utcfromtimestamp(calendar.timegm(parsed)).date().isoformat()
    return ""


def _fetch_feed(company: str, url: str, timeout: int) -> list[dict]:
    import feedparser

    response = requests.get(
        url,
        headers={"User-Agent": "Algorithm-Practice-in-Industry/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    articles = []
    for entry in feed.entries:
        tags = [tag.get("term", "") for tag in entry.get("tags", []) if tag.get("term")]
        articles.append(
            {
                "company": company,
                "link": entry.get("link", ""),
                "title": _plain_text(entry.get("title", "")),
                "summary": _plain_text(entry.get("summary", entry.get("description", ""))),
                "tags": tags or ["Engineering Blog"],
                "date": _entry_date(entry),
                "live": True,
            }
        )
    return articles


def fetch_live_articles(timeout: int = 20) -> list[dict]:
    configured = os.environ.get("INDUSTRY_FEEDS", "").strip()
    feeds = DEFAULT_FEEDS
    if configured:
        feeds = dict(json.loads(configured))

    articles = []
    with ThreadPoolExecutor(max_workers=min(6, len(feeds))) as executor:
        futures = {
            executor.submit(_fetch_feed, company, url, timeout): company
            for company, url in feeds.items()
        }
        for future in as_completed(futures):
            company = futures[future]
            try:
                articles.extend(future.result())
            except Exception as exc:
                print(f"Warning: skipped {company} feed: {exc}")
    return articles


def industry_relevance(item: dict) -> int:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    return sum(1 for keyword in INDUSTRY_KEYWORDS if keyword in text)


def select_articles(
    articles: Iterable[dict], push_date: date, limit: int, tags: set[str] | None = None
) -> list[dict]:
    filtered = []
    seen_links = set()
    for item in articles:
        item_tags = {str(tag).strip() for tag in item.get("tags", [])}
        if tags and not (item_tags & tags):
            continue
        if item.get("title") and item.get("link") and item["link"] not in seen_links:
            filtered.append(item)
            seen_links.add(item["link"])

    if not filtered or limit <= 0:
        return []

    # Newest first. Relevance and a stable key only break ties on the same day.
    filtered.sort(
        key=lambda item: (
            str(item.get("date", "")),
            industry_relevance(item),
            _stable_key(item),
        ),
        reverse=True,
    )
    return filtered[:limit]


def build_markdown(items: Iterable[dict]) -> str:
    blocks = []
    for item in items:
        tags = " / ".join(str(tag) for tag in item.get("tags", [])) or "未分类"
        meta = " · ".join(
            value
            for value in [str(item.get("company", "未知公司")), tags, str(item.get("date", ""))]
            if value
        )
        title = item.get("title_zh") or item["title"]
        block = f"**{meta}**\n[{title}]({item['link']})"
        if item.get("title_zh") and item["title"] != item["title_zh"]:
            block += f"\n原题：{item['title']}"
        summary = item.get("summary_zh") or item.get("summary")
        if summary:
            block += f"\n摘要：{summary}"
        blocks.append(block)
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
    live_articles = fetch_live_articles()
    if not live_articles:
        raise RuntimeError("no live engineering-blog feeds were available; refusing stale fallback")
    selected = select_articles(live_articles, args.date, args.limit, wanted_tags)
    if not selected:
        raise RuntimeError("no industry-practice articles matched the configured filters")

    if not args.dry_run:
        translations = enrich_chinese(
            {
                "id": str(index),
                "title": article["title"],
                "summary": article.get("summary", ""),
            }
            for index, article in enumerate(selected)
        )
        for index, article in enumerate(selected):
            article.update(translations[str(index)])

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
