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
from paperBotV2.metrics import fetch_s2_metrics, search_s2_conference_papers
from paperBotV2.relevance import is_recommendation_relevant


DEFAULT_DATA = Path(__file__).resolve().parent / "data" / "results.json"
ROTATION_EPOCH = date(2026, 1, 1)
KEY_PATTERN = re.compile(r"^([a-zA-Z]+)(\d{4})$")
VENUE_FILTERS = {
    "kdd": ("KDD",),
    "www": ("WWW", "The Web Conference"),
    "cikm": ("CIKM",),
    "recsys": ("RecSys",),
    "wsdm": ("WSDM",),
    "sigir": ("SIGIR",),
    "ecir": ("ECIR",),
}


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
            if score <= 0 or not is_recommendation_relevant(
                paper.get("paper_name", ""), paper.get("paper_abstract", "")
            ):
                continue
            enriched = dict(paper)
            enriched.update({"conference": conference.upper(), "year": year, "match_score": score})
            candidates.append(enriched)
    return candidates


def _conference_code(venue: str) -> str:
    lowered = str(venue or "").lower()
    if "web conference" in lowered or re.search(r"\bwww\b", lowered):
        return "WWW"
    for code in ("recsys", "sigir", "cikm", "wsdm", "ecir", "kdd"):
        if code in lowered:
            return code.upper()
    return str(venue or "TOP CONF").upper()


def online_conference_candidates(papers: Iterable[dict]) -> list[dict]:
    candidates = []
    for paper in papers:
        external_ids = paper.get("externalIds") or {}
        doi = str(external_ids.get("DOI") or "")
        normalized = {
            "paper_name": str(paper.get("title") or ""),
            "paper_url": f"https://doi.org/{doi}" if doi else str(paper.get("url") or ""),
            "paper_authors": [
                str(author.get("name") or "")
                for author in paper.get("authors", [])
                if isinstance(author, dict) and author.get("name")
            ],
            "paper_abstract": str(paper.get("abstract") or ""),
            "conference": _conference_code(str(paper.get("venue") or "")),
            "year": int(paper.get("year") or 0),
            "publication_date": str(paper.get("publicationDate") or ""),
            "citation_count": int(paper.get("citationCount") or 0),
            "influential_citation_count": int(
                paper.get("influentialCitationCount") or 0
            ),
        }
        normalized["match_score"] = match_score(normalized)
        if (
            normalized["paper_name"]
            and normalized["year"]
            and normalized["match_score"] > 0
            and is_recommendation_relevant(
                normalized["paper_name"], normalized["paper_abstract"]
            )
        ):
            candidates.append(normalized)
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


def select_yearly_papers(
    candidates: Iterable[dict], year: int, limit: int, excluded: set[str] | None = None
) -> list[dict]:
    excluded = excluded or set()
    current_year = [
        item
        for item in candidates
        if int(item.get("year", 0)) == year
        and str(item.get("paper_url") or item.get("paper_name")) not in excluded
    ]
    return sorted(current_year, key=_stable_key)[: max(0, limit)]


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
                authors_text += " et al."
        abstract = item.get("paper_abstract") or ""
        block = f"**{item['conference']} {item['year']}**\n{linked_title}"
        if item.get("selection_window"):
            block += f"\nWindow: {item['selection_window']}"
        if authors_text:
            block += f"\nAuthors: {authors_text}"
        block += (
            f"\nPublished: {item.get('publication_date', '')} · "
            f"{int(item.get('citation_count', 0))} citations · "
            f"{int(item.get('influential_citation_count', 0))} influential citations"
        )
        if abstract:
            block += f"\nAbstract: {_shorten(str(abstract))}"
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
    venue_filters = [
        venue
        for conference in conferences
        for venue in VENUE_FILTERS.get(conference, (conference.upper(),))
    ]
    online = online_conference_candidates(
        search_s2_conference_papers(
            year=args.date.year,
            venues=venue_filters,
            limit=max(100, args.limit * 20),
        )
    )
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

    combined = {}
    for paper in possible_recent + online:
        identity = str(paper.get("paper_url") or paper.get("paper_name", "")).lower()
        if identity:
            combined[identity] = paper
    eligible = list(combined.values())

    selected = select_papers(eligible, args.date, args.limit, args.lookback_days)
    for paper in selected:
        paper["selection_window"] = f"last {args.lookback_days} days"
    selected_ids = {
        str(paper.get("paper_url") or paper.get("paper_name")) for paper in selected
    }
    fallback = select_yearly_papers(
        eligible,
        args.date.year,
        args.limit - len(selected),
        selected_ids,
    )
    for paper in fallback:
        paper["selection_window"] = f"{args.date.year} quality fallback"
    selected.extend(fallback)
    if not selected:
        send_card(
            f"🏆 Top-Conference Recommender Papers · {args.date.isoformat()}",
            f"No relevant paper was found in the last {args.lookback_days} days "
            f"or the {args.date.year} conference editions; no prior-year fallback was used.",
            color="purple",
            dry_run=args.dry_run,
        )
        print("Selected 0 conference papers (no prior-year backfill)")
        return 0

    send_card(
        f"🏆 Top-Conference Recommender Papers · {args.date.isoformat()}",
        build_markdown(selected),
        color="purple",
        dry_run=args.dry_run,
    )
    print(f"Selected {len(selected)} papers from {len(candidates)} candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
