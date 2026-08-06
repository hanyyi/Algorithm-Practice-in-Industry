"""Fetch and push a lightweight daily arXiv digest without an LLM API key."""

from __future__ import annotations

import argparse
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import requests

from paperBotV2.feishu import send_card
from paperBotV2.github_models import enrich_chinese
from paperBotV2.metrics import attach_hn_metrics, fetch_hn_metrics, fetch_s2_metrics


ARXIV_API_URL = "https://export.arxiv.org/api/query"
DEFAULT_CATEGORIES = ("cs.IR", "cs.CL", "cs.LG")
KEYWORD_WEIGHTS = {
    "recommend": 5,
    "retrieval": 5,
    "search": 4,
    "ranking": 4,
    "recommender": 5,
    "advertising": 4,
    "ads": 3,
    "click-through": 4,
    "ctr": 3,
    "information retrieval": 5,
    "user modeling": 3,
    "personalization": 3,
    "large language model": 2,
    "llm": 2,
    "agent": 1,
}
ARXIV_ID_PATTERN = re.compile(r"arxiv\.org/(?:abs|pdf)/([^/?#]+)", re.IGNORECASE)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def normalize_entry(entry) -> dict:
    links = [link.get("href", "") for link in entry.get("links", [])]
    paper_url = next((link for link in links if "/abs/" in link), entry.get("id", ""))
    return {
        "id": entry.get("id", ""),
        "title": " ".join(entry.get("title", "").split()),
        "summary": " ".join(entry.get("summary", "").split()),
        "url": paper_url,
        "published": _parse_datetime(entry.get("published")),
        "authors": [author.get("name", "") for author in entry.get("authors", [])],
        "categories": [tag.get("term", "") for tag in entry.get("tags", [])],
    }


def relevance_score(paper: dict) -> int:
    text = f"{paper.get('title', '')} {paper.get('summary', '')}".lower()
    score = sum(weight for keyword, weight in KEYWORD_WEIGHTS.items() if keyword in text)
    categories = set(paper.get("categories", []))
    if "cs.IR" in categories:
        score += 3
    if "cs.CL" in categories or "cs.LG" in categories:
        score += 1
    return score


def semantic_scholar_id(paper: dict) -> str:
    match = ARXIV_ID_PATTERN.search(f"{paper.get('url', '')} {paper.get('id', '')}")
    if not match:
        return ""
    arxiv_id = re.sub(r"v\d+$", "", match.group(1), flags=re.IGNORECASE)
    return f"ARXIV:{arxiv_id}"


def select_papers(
    papers: Iterable[dict], now: datetime, limit: int, lookback_days: int
) -> list[dict]:
    cutoff = now.astimezone(timezone.utc) - timedelta(days=lookback_days)
    unique = {}
    for paper in papers:
        if not paper.get("title") or not paper.get("url"):
            continue
        published = paper.get("published")
        if not published or published < cutoff or published > now:
            continue
        unique[paper.get("id") or paper["url"]] = paper

    ordered = sorted(
        unique.values(),
        key=lambda paper: (
            -int(paper.get("citation_count", 0)),
            -int(paper.get("influential_citation_count", 0)),
            -int(paper.get("hn_points", 0)),
            -int(paper.get("hn_comments", 0)),
            -relevance_score(paper),
            -paper.get("published", datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            paper.get("id", ""),
        ),
    )
    return ordered[: max(0, limit)]


def fetch_papers(categories: Iterable[str], max_results: int, timeout: int = 30) -> list[dict]:
    import feedparser

    query = " OR ".join(f"cat:{category}" for category in categories)
    response = requests.get(
        ARXIV_API_URL,
        params={
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        },
        headers={"User-Agent": "Algorithm-Practice-in-Industry/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    if getattr(feed, "bozo", False) and not feed.entries:
        raise RuntimeError(f"arXiv returned an invalid feed: {feed.bozo_exception}")
    return [normalize_entry(entry) for entry in feed.entries]


def _shorten(text: str, limit: int = 360) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_markdown(papers: Iterable[dict]) -> str:
    blocks = []
    for paper in papers:
        authors = ", ".join(paper.get("authors", [])[:5])
        if len(paper.get("authors", [])) > 5:
            authors += " et al."
        categories = " / ".join(paper.get("categories", [])[:4])
        published = paper.get("published")
        published_text = published.date().isoformat() if published else ""
        meta = " · ".join(value for value in [published_text, categories, authors] if value)
        display_title = paper.get("title_zh") or paper["title"]
        block = f"[{display_title}]({paper['url']})"
        if paper.get("title_zh"):
            block += f"\n原题：{paper['title']}"
        if meta:
            block += f"\n{meta}"
        block += (
            f"\n指标：引用 {int(paper.get('citation_count', 0))} · "
            f"高影响引用 {int(paper.get('influential_citation_count', 0))} · "
            f"HN {int(paper.get('hn_points', 0))} 分/{int(paper.get('hn_comments', 0))} 评论"
        )
        summary = paper.get("summary_zh") or paper.get("summary")
        if summary:
            block += f"\n摘要：{_shorten(summary)}"
        blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push a fresh arXiv digest to Feishu")
    parser.add_argument("--limit", type=int, default=int(os.environ.get("ARXIV_LIMIT", "10")))
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("ARXIV_LOOKBACK_DAYS", "7")),
    )
    parser.add_argument(
        "--categories",
        default=os.environ.get("ARXIV_CATEGORIES", ",".join(DEFAULT_CATEGORIES)),
    )
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    categories = [item.strip() for item in args.categories.split(",") if item.strip()]
    now = datetime.now(timezone.utc)
    recent = select_papers(
        fetch_papers(categories, args.max_results),
        now,
        args.max_results,
        args.lookback_days,
    )
    if not recent:
        raise RuntimeError("no recent arXiv papers matched the configured categories")

    metric_sources = 0
    identifiers = {paper["id"]: semantic_scholar_id(paper) for paper in recent}
    try:
        s2_metrics = fetch_s2_metrics(identifier for identifier in identifiers.values() if identifier)
        for paper in recent:
            paper.update(s2_metrics.get(identifiers[paper["id"]], {}))
        metric_sources += 1
    except Exception as exc:
        print(f"Warning: Semantic Scholar metrics unavailable: {exc}")

    try:
        hn_metrics = fetch_hn_metrics(now - timedelta(days=args.lookback_days))
        attach_hn_metrics(recent, hn_metrics, "url")
        metric_sources += 1
    except Exception as exc:
        print(f"Warning: Hacker News metrics unavailable: {exc}")

    if metric_sources == 0:
        raise RuntimeError("all arXiv metric providers were unavailable")
    selected = select_papers(recent, now, args.limit, args.lookback_days)

    if not args.dry_run:
        translations = enrich_chinese(
            {"id": paper["id"], "title": paper["title"], "summary": paper["summary"]}
            for paper in selected
        )
        for paper in selected:
            paper.update(translations[paper["id"]])

    send_card(
        f"📚 arXiv 论文日推 · {date.today().isoformat()}",
        build_markdown(selected),
        color="blue",
        dry_run=args.dry_run,
    )
    print(f"Selected {len(selected)} arXiv papers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
