"""Push the newest relevant industry-engineering articles to Feishu."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup

from paperBotV2.feishu import send_card
from paperBotV2.llm_enrichment import generate_chinese_summaries
from paperBotV2.metrics import (
    attach_hn_metrics,
    canonical_url,
    fetch_hn_industry_articles,
    fetch_hn_metrics,
)
from paperBotV2.relevance import (
    is_recommendation_relevant,
    recommendation_relevance_score,
)


DEFAULT_DATA = Path(__file__).resolve().parent / "data" / "article.json"
UPSTREAM_ARTICLE_DATA_URL = (
    "https://raw.githubusercontent.com/Doragd/Algorithm-Practice-in-Industry/"
    "main/paperBotV2/industry_practice/data/article.json"
)
DEFAULT_FEEDS = {
    "Netflix TechBlog": "https://netflixtechblog.com/feed",
    "Spotify Engineering": "https://engineering.atspotify.com/feed/",
    "GitHub Engineering": "https://github.blog/engineering/feed/",
    "Pinterest Engineering": "https://medium.com/feed/pinterest-engineering",
    "Airbnb Engineering": "https://medium.com/feed/airbnb-engineering",
    "Meta Engineering": "https://engineering.fb.com/feed/",
    "Dropbox Tech": "https://dropbox.tech/feed",
    "Slack Engineering": "https://slack.engineering/feed/",
    "Apple Machine Learning Research": "https://machinelearning.apple.com/rss.xml",
    "Google Research": "https://research.google/blog/rss/",
    "Amazon Science": "https://www.amazon.science/index.rss",
    "Meituan Tech": "https://tech.meituan.com/feed/",
    "PayPal Tech": "https://medium.com/feed/paypal-tech",
    "Walmart Global Tech": "https://medium.com/feed/walmartglobaltech",
}


def _normalize_articles(data: object, *, source_priority: int) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("expected an article list")
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
                "summary": item.get("summary", item.get("摘要", "")),
                "source_priority": source_priority,
            }
        )
    return normalized


def load_articles(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        return _normalize_articles(data, source_priority=2)
    except ValueError as exc:
        raise ValueError(f"expected an article list in {path}") from exc


def fetch_upstream_articles(timeout: int = 30) -> list[dict]:
    """Load the original author's curated article database on every run."""

    response = requests.get(
        os.environ.get("INDUSTRY_UPSTREAM_DATA_URL", UPSTREAM_ARTICLE_DATA_URL),
        headers={"User-Agent": "Algorithm-Practice-in-Industry/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    return _normalize_articles(response.json(), source_priority=3)


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
                "source_priority": 2,
            }
        )
    return articles


def merge_articles(*groups: Iterable[dict]) -> list[dict]:
    """Deduplicate sources while keeping the richest, most trusted record."""

    merged: dict[str, dict] = {}
    for group in groups:
        for item in group:
            link = canonical_url(item.get("link", ""))
            identity = link or str(item.get("title") or "").strip().lower()
            if not identity:
                continue
            candidate = dict(item)
            if link:
                candidate["link"] = link
            previous = merged.get(identity)
            if previous is None:
                merged[identity] = candidate
                continue
            preferred, other = sorted(
                (previous, candidate),
                key=lambda value: (
                    int(value.get("source_priority", 0)),
                    bool(value.get("summary")),
                ),
                reverse=True,
            )
            combined = dict(other)
            combined.update(preferred)
            for key in ("summary", "company", "date", "tags"):
                if not combined.get(key):
                    combined[key] = other.get(key) or preferred.get(key)
            combined["hn_points"] = max(
                int(previous.get("hn_points", 0)), int(candidate.get("hn_points", 0))
            )
            combined["hn_comments"] = max(
                int(previous.get("hn_comments", 0)),
                int(candidate.get("hn_comments", 0)),
            )
            merged[identity] = combined
    return list(merged.values())


def _fetch_article_summary(item: dict, timeout: int) -> str:
    response = requests.get(
        item["link"],
        headers={"User-Agent": "Mozilla/5.0 Algorithm-Practice-in-Industry/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for selector, attribute in (
        ('meta[property="og:description"]', "content"),
        ('meta[name="description"]', "content"),
        ('meta[name="twitter:description"]', "content"),
    ):
        node = soup.select_one(selector)
        if node and node.get(attribute):
            summary = _plain_text(str(node.get(attribute)))
            if len(summary) >= 40:
                return summary[:5000]
    article = soup.select_one("article") or soup.select_one("main")
    if article:
        return _plain_text(str(article))[:5000]
    return ""


def hydrate_article_summaries(
    articles: Iterable[dict], timeout: int = 20, max_workers: int = 5
) -> None:
    missing = [item for item in articles if item.get("link") and not item.get("summary")]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=min(max_workers, len(missing))) as executor:
        futures = {
            executor.submit(_fetch_article_summary, item, timeout): item
            for item in missing
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                summary = future.result()
            except Exception as exc:
                print(f"Warning: could not read article summary {item.get('link')}: {exc}")
                continue
            if summary:
                item["summary"] = summary


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
                company_articles = future.result()
                articles.extend(company_articles)
                print(f"Industry RSS source: {company}={len(company_articles)}")
            except Exception as exc:
                print(f"Warning: skipped {company} feed: {exc}")
    return articles


def industry_relevance(item: dict) -> int:
    return recommendation_relevance_score(
        item.get("title", ""), item.get("summary", ""), item.get("tags", [])
    )


def select_articles(
    articles: Iterable[dict],
    push_date: date,
    limit: int,
    tags: set[str] | None = None,
    lookback_days: int = 7,
) -> list[dict]:
    filtered = []
    seen_links = set()
    cutoff = push_date - timedelta(days=max(1, lookback_days) - 1)
    for item in articles:
        item_tags = {str(tag).strip() for tag in item.get("tags", [])}
        if tags and not (item_tags & tags):
            continue
        if not is_recommendation_relevant(
            item.get("title", ""), item.get("summary", ""), item_tags
        ):
            continue
        try:
            published = date.fromisoformat(str(item.get("date", ""))[:10])
        except ValueError:
            continue
        if published < cutoff or published > push_date:
            continue
        if item.get("title") and item.get("link") and item["link"] not in seen_links:
            filtered.append(item)
            seen_links.add(item["link"])

    if not filtered or limit <= 0:
        return []

    # Real engagement first inside the strict weekly window.
    filtered.sort(
        key=lambda item: (
            int(item.get("hn_points", 0)),
            int(item.get("hn_comments", 0)),
            industry_relevance(item),
            int(item.get("source_priority", 0)),
            str(item.get("date", "")),
            _stable_key(item),
        ),
        reverse=True,
    )
    return filtered[:limit]


def build_markdown(items: Iterable[dict]) -> str:
    blocks = []
    for item in items:
        tags = " / ".join(str(tag) for tag in item.get("tags", [])) or "Uncategorized"
        meta = " · ".join(
            value
            for value in [str(item.get("company", "Unknown company")), tags, str(item.get("date", ""))]
            if value
        )
        block = f"**{meta}**\n[{item['title']}]({item['link']})"
        block += (
            f"\nMetrics: HN {int(item.get('hn_points', 0))} points/"
            f"{int(item.get('hn_comments', 0))} comments"
        )
        summary_zh = item.get("summary_zh")
        summary = item.get("summary")
        if summary_zh:
            block += f"\n中文解读：{summary_zh}"
        elif summary:
            block += f"\nSummary: {summary}"
        blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push daily industry articles to Feishu")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--limit", type=int, default=int(os.environ.get("INDUSTRY_LIMIT", "5")))
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(
            os.environ.get(
                "INDUSTRY_LOOKBACK_DAYS", os.environ.get("LOOKBACK_DAYS", "7")
            )
        ),
    )
    parser.add_argument("--tags", default=os.environ.get("INDUSTRY_TAGS", ""))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    wanted_tags = {tag.strip() for tag in args.tags.split(",") if tag.strip()} or None
    live_articles = fetch_live_articles()
    local_articles = load_articles(args.data)
    try:
        upstream_articles = fetch_upstream_articles()
    except Exception as exc:
        print(f"Warning: original-author article database unavailable: {exc}")
        upstream_articles = []
    cutoff_datetime = datetime.combine(
        args.date - timedelta(days=max(1, args.lookback_days) - 1),
        time.min,
        tzinfo=timezone.utc,
    )
    try:
        hn_articles = fetch_hn_industry_articles(cutoff_datetime)
    except Exception as exc:
        print(f"Warning: Hacker News discovery unavailable: {exc}")
        hn_articles = []
    all_articles = merge_articles(
        local_articles, upstream_articles, live_articles, hn_articles
    )
    print(
        "Industry source coverage: "
        f"author-local={len(local_articles)}, author-upstream={len(upstream_articles)}, "
        f"official-feeds={len(live_articles)}, hn-discovery={len(hn_articles)}, "
        f"deduplicated={len(all_articles)}"
    )
    if not all_articles:
        raise RuntimeError("no industry article sources were available")
    recent = select_articles(
        all_articles,
        args.date,
        len(all_articles),
        wanted_tags,
        args.lookback_days,
    )
    if not recent:
        send_card(
            f"🏭 Daily Digest · Recommender Systems in Industry · {args.date.isoformat()}",
            f"No relevant recommender-system article was published in the last "
            f"{args.lookback_days} days; no stale content was used.",
            color="turquoise",
            dry_run=args.dry_run,
        )
        print("Selected 0 industry-practice articles (no stale backfill)")
        return 0

    hn_metrics = fetch_hn_metrics(cutoff_datetime)
    attach_hn_metrics(recent, hn_metrics, "link")
    selected = select_articles(
        recent, args.date, args.limit, wanted_tags, args.lookback_days
    )
    hydrate_article_summaries(selected)
    summaries = (
        {}
        if args.dry_run
        else generate_chinese_summaries(
            [
                {
                    "id": str(item.get("link")),
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                }
                for item in selected
            ]
        )
    )
    for item in selected:
        item_id = str(item.get("link"))
        if summaries.get(item_id):
            item["summary_zh"] = summaries[item_id]

    send_card(
        f"🏭 Daily Digest · Recommender Systems in Industry · {args.date.isoformat()}",
        build_markdown(selected),
        color="turquoise",
        dry_run=args.dry_run,
    )
    print(f"Selected {len(selected)} industry-practice articles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
