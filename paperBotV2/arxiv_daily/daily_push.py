"""Fetch and push a lightweight daily arXiv digest without an LLM API key."""

from __future__ import annotations

import argparse
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import requests

from paperBotV2.feishu import send_card
from paperBotV2.metrics import attach_hn_metrics, fetch_hn_metrics, fetch_s2_metrics
from paperBotV2.relevance import (
    is_recommendation_relevant,
    recommendation_relevance_score,
)


ARXIV_API_URL = "https://export.arxiv.org/api/query"
DEFAULT_CATEGORIES = ("cs.IR", "cs.CL", "cs.LG")
ARXIV_ID_PATTERN = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([^\s/?#]+)", re.IGNORECASE
)


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
    return recommendation_relevance_score(
        paper.get("title", ""), paper.get("summary", ""), paper.get("categories", [])
    )


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
        if not is_recommendation_relevant(
            paper.get("title", ""),
            paper.get("summary", ""),
            paper.get("categories", []),
        ):
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


def _result_pages(max_results: int, page_size: int = 100) -> list[tuple[int, int]]:
    if max_results <= 0 or page_size <= 0:
        return []
    return [
        (start, min(page_size, max_results - start))
        for start in range(0, max_results, page_size)
    ]


def fetch_papers(
    categories: Iterable[str],
    max_results: int,
    timeout: int = 30,
    pause_seconds: float = 3.0,
) -> list[dict]:
    import feedparser

    query = " OR ".join(f"cat:{category}" for category in categories)
    papers = []
    pages = _result_pages(max_results)
    for page_index, (start, page_limit) in enumerate(pages):
        response = requests.get(
            ARXIV_API_URL,
            params={
                "search_query": query,
                "start": start,
                "max_results": page_limit,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
            headers={
                "Accept": "application/atom+xml",
                "User-Agent": "Algorithm-Practice-in-Industry/1.0",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if getattr(feed, "bozo", False) and not feed.entries:
            raise RuntimeError(f"arXiv returned an invalid feed: {feed.bozo_exception}")
        papers.extend(normalize_entry(entry) for entry in feed.entries)
        if len(feed.entries) < page_limit:
            break
        if pause_seconds > 0 and page_index < len(pages) - 1:
            time.sleep(pause_seconds)
    return papers


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
        block = f"[{paper['title']}]({paper['url']})"
        if meta:
            block += f"\n{meta}"
        block += (
            f"\nMetrics: {int(paper.get('citation_count', 0))} citations · "
            f"{int(paper.get('influential_citation_count', 0))} influential citations · "
            f"HN {int(paper.get('hn_points', 0))} points/{int(paper.get('hn_comments', 0))} comments"
        )
        summary = paper.get("summary")
        if summary:
            block += f"\nAbstract: {_shorten(summary)}"
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
    parser.add_argument("--max-results", type=int, default=300)
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
        raise RuntimeError("no recent recommendation-system arXiv papers matched")

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

    send_card(
        f"📚 Daily Digest · arXiv Recommender Papers · {date.today().isoformat()}",
        build_markdown(selected),
        color="blue",
        dry_run=args.dry_run,
    )
    print(f"Selected {len(selected)} arXiv papers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
