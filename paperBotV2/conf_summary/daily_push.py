"""Push a rotating selection of relevant top-conference papers to Feishu."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Iterable

from paperBotV2.conf_summary.conf_daily import DEFAULT_CONFS, match_score
from paperBotV2.feishu import send_card


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
    return (-int(item["year"]), -float(item["match_score"]), digest)


def select_papers(candidates: Iterable[dict], push_date: date, limit: int) -> list[dict]:
    ordered = sorted(candidates, key=_stable_key)
    if not ordered or limit <= 0:
        return []
    start = ((push_date - ROTATION_EPOCH).days * limit) % len(ordered)
    take = min(limit, len(ordered))
    return [ordered[(start + offset) % len(ordered)] for offset in range(take)]


def _shorten(text: str, limit: int = 420) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


def build_markdown(items: Iterable[dict]) -> str:
    blocks = []
    for item in items:
        title = item["paper_name"]
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
            item.get("abstract_translation")
            or item.get("translated")
            or item.get("paper_abstract")
            or ""
        )
        block = f"**{item['conference']} {item['year']}**\n{linked_title}"
        if authors_text:
            block += f"\n作者：{authors_text}"
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
    selected = select_papers(candidates, args.date, args.limit)
    if not selected:
        raise RuntimeError("no conference papers matched the configured filters")

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
