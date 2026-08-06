"""Push a rotating selection of relevant top-conference papers to Feishu."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from paperBotV2.conf_summary.conf_daily import DEFAULT_CONFS, match_score
from paperBotV2.feishu import send_card
from paperBotV2.github_models import enrich_chinese
from paperBotV2.metrics import fetch_s2_metrics


DEFAULT_DATA = Path(__file__).resolve().parent / "data" / "results.json"
ROTATION_EPOCH = date(2026, 1, 1)
KEY_PATTERN = re.compile(r"^([a-zA-Z]+)(\d{4})$")


def load_results(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("version https://git-lfs.github.com/spec/"):
        raise RuntimeError(
            f"{path} is a Git LFS pointer; checkout the workflow with lfs: true"
        )
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"expected a conference result mapping in {path}")
    return data


def conference_candidates(
    results: dict, conferences: set[str], start_year: int
) -> list[dict]:
    candidates = []
    for key, papers in results.items():
        match = KEY_PATTERN.match(key)
        if not match or not isinstance(papers, list):
            continue
        conference, year_text = match.groups()
        conference = conference.lower()
        year = int(year_text)
        if conference not in conferences or year < start_year:
            continue

        for paper in papers:
            if not isinstance(paper, dict) or not paper.get("paper_name"):
                continue
            score = match_score(paper)
            if score <= 0:
                continue
            enriched = dict(paper)
            enriched.update({"conference": conference.upper(), "year": year, "match_score": score})
            candidates.append(enriched)
    return candidates


def _stable_key(item: dict) -> tuple:
    identity = f"{item.get('conference')}\n{item.get('year')}\n{item.get('paper_name')}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    try:
        published_ordinal = date.fromisoformat(
            str(item.get("publication_date", ""))[:10]
        ).toordinal()
    except ValueError:
        published_ordinal = 0
    return (
        -int(item.get("citation_count", 0)),
        -int(item.get("influential_citation_count", 0)),
        -float(item["match_score"]),
        -published_ordinal,
        digest,
    )


def select_papers(
    candidates: Iterable[dict], push_date: date, limit: int, lookback_days: int = 7
) -> list[dict]:
    cutoff = push_date - timedelta(days=max(1, lookback_days) - 1)
    recent = []
    for item in candidates:
        try:
            published = date.fromisoformat(str(item.get("publication_date", ""))[:10])
        except ValueError:
            continue
        if cutoff <= published <= push_date:
            recent.append(item)
    ordered = sorted(recent, key=_stable_key)
    if not ordered or limit <= 0:
        return []
    return ordered[:limit]


def _shorten(text: str, limit: int = 420) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


def build_markdown(items: Iterable[dict]) -> str:
    blocks = []
    for item in items:
        title = item.get("title_zh") or item["paper_name"]
        url = item.get("paper_url") or item.get("url") or ""
        linked_title = f"[{title}]({url})" if url else title
        authors = item.get("paper_authors") or []
        if isinstance(authors, str):
            authors_text = authors
        else:
            authors_text = ", ".join(str(author) for author in authors[:5])
            if len(authors) > 5:
                authors_text += " 等"
        abstract = (
            item.get("summary_zh")
            or item.get("abstract_translation")
            or item.get("translated")
            or item.get("paper_abstract")
            or ""
        )
        block = f"**{item['conference']} {item['year']}**\n{linked_title}"
        if item.get("title_zh"):
            block += f"\n原题：{item['paper_name']}"
        if authors_text:
            block += f"\n作者：{authors_text}"
        block += (
            f"\n发布日期：{item.get('publication_date', '')} · "
            f"引用 {int(item.get('citation_count', 0))} · "
            f"高影响引用 {int(item.get('influential_citation_count', 0))}"
        )
        if abstract:
            block += f"\n摘要：{_shorten(str(abstract))}"
        blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push daily conference papers to Feishu")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--limit", type=int, default=int(os.environ.get("CONF_LIMIT", "3")))
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(
            os.environ.get("CONF_LOOKBACK_DAYS", os.environ.get("LOOKBACK_DAYS", "7"))
        ),
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=int(os.environ.get("CONF_START_YEAR", str(date.today().year - 5))),
    )
    parser.add_argument("--confs", default=os.environ.get("CONFS", ",".join(DEFAULT_CONFS)))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    conferences = {item.strip().lower() for item in args.confs.split(",") if item.strip()}
    candidates = conference_candidates(load_results(args.data), conferences, args.start_year)
    cutoff = args.date - timedelta(days=max(1, args.lookback_days) - 1)
    possible_recent = [
        paper for paper in candidates if cutoff.year <= int(paper["year"]) <= args.date.year
    ]
    identifiers = {}
    for paper in possible_recent:
        url = str(paper.get("paper_url") or paper.get("url") or "")
        marker = "doi.org/"
        if marker in url.lower():
            identifiers[id(paper)] = f"DOI:{url.lower().split(marker, 1)[1]}"
    if identifiers:
        s2_metrics = fetch_s2_metrics(identifiers.values())
        for paper in possible_recent:
            identifier = identifiers.get(id(paper), "")
            paper.update(s2_metrics.get(identifier, {}))

    selected = select_papers(
        possible_recent, args.date, args.limit, args.lookback_days
    )
    if not selected:
        send_card(
            f"🏆 顶会论文日推 · {args.date.isoformat()}",
            f"近 {args.lookback_days} 天暂无指定顶会新论文；未使用历史论文补位。",
            color="purple",
            dry_run=args.dry_run,
        )
        print("Selected 0 conference papers (no stale backfill)")
        return 0

    if not args.dry_run:
        model_items = []
        for index, paper in enumerate(selected):
            abstract = (
                paper.get("abstract_translation")
                or paper.get("translated")
                or paper.get("paper_abstract")
                or ""
            )
            model_items.append(
                {"id": str(index), "title": paper["paper_name"], "summary": abstract}
            )
        translations = enrich_chinese(model_items)
        for index, paper in enumerate(selected):
            paper.update(translations[str(index)])

    send_card(
        f"🏆 顶会论文日推 · {args.date.isoformat()}",
        build_markdown(selected),
        color="purple",
        dry_run=args.dry_run,
    )
    print(f"Selected {len(selected)} papers from {len(candidates)} candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
