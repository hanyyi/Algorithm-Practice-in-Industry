import argparse
import ast
import json
from datetime import datetime, timezone
from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
RESULTS_PATH = SCRIPT_DIR / "data" / "results.json"
DEFAULT_FILTERS = ["kddcup", "w.html", "lbr.html"]

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def parse_issue(issue_body):
    payload = issue_body

    for _ in range(4):
        if not isinstance(payload, str):
            break

        text = payload.strip()
        if not text:
            raise ValueError("Empty issue body")

        for loader in (json.loads, ast.literal_eval):
            try:
                payload = loader(text)
                break
            except (json.JSONDecodeError, SyntaxError, ValueError):
                continue
        else:
            escaped_text = text.replace("\\r", "").replace("\\n", "\n")
            try:
                payload = ast.literal_eval(escaped_text)
            except (SyntaxError, ValueError) as exc:
                raise ValueError("Wrong issue body format") from exc

    if not isinstance(payload, list) or len(payload) != 1:
        raise ValueError("Issue body must be a list with exactly one item")

    item = payload[0]
    if not isinstance(item, dict):
        raise ValueError("Issue item must be an object")

    required_fields = ("confs", "year", "filter")
    missing_fields = [field for field in required_fields if field not in item]
    if missing_fields:
        raise ValueError(f"Missing required fields: {', '.join(missing_fields)}")

    return item


def build_filters(filter_text):
    filters = DEFAULT_FILTERS.copy()
    normalized_filter = str(filter_text or "").strip()

    if normalized_filter and normalized_filter != "默认留空就行":
        filters.extend(normalized_filter.lower().split())

    return filters


def enrich_citation_metrics(confs, year):
    """Persist public citation metrics for the refreshed conference year."""
    from paperBotV2.metrics import fetch_s2_metrics

    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    paper_refs = {}
    for conf in confs:
        for paper in results.get(f"{conf}{year}", []):
            if not isinstance(paper, dict):
                continue
            doi = str(paper.get("doi") or "").strip()
            if not doi:
                url = str(paper.get("paper_url") or "")
                if "doi.org/" in url.lower():
                    doi = url.lower().split("doi.org/", 1)[1]
            if doi:
                paper_refs.setdefault(f"DOI:{doi}", []).append(paper)

    if not paper_refs:
        print(f"No DOI-backed {year} papers found for citation enrichment")
        return
    try:
        metrics = fetch_s2_metrics(paper_refs.keys())
    except Exception as exc:
        print(f"Warning: citation metrics were not persisted: {exc}")
        return

    fetched_at = datetime.now(timezone.utc).isoformat()
    updated = 0
    for identifier, papers in paper_refs.items():
        values = metrics.get(identifier)
        if not values:
            continue
        for paper in papers:
            paper.update(values)
            paper["metrics_source"] = "Semantic Scholar"
            paper["metrics_fetched_at"] = fetched_at
            updated += 1
    RESULTS_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=4), encoding="utf-8"
    )
    print(f"Persisted citation metrics for {updated} {year} conference papers")


def run(
    confs_text, start_year, filter_text="", threads=20, refresh_existing=False
):
    import crawler

    confs = str(confs_text).lower().split()
    if not confs:
        raise ValueError("No conferences were provided")

    crawler.run_all(
        confs=confs,
        filter_keywords=build_filters(filter_text),
        start_year=int(start_year),
        filename=str(RESULTS_PATH),
        writename=str(RESULTS_PATH),
        threads=threads,
        refresh_existing=refresh_existing,
    )
    enrich_citation_metrics(confs, int(start_year))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Update paperBotV2 conference data from a GitHub issue body."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--issue", "-i", help="GitHub issue body")
    source.add_argument("--confs", help="Space-separated conference names")
    parser.add_argument("--year", type=int, help="First conference year")
    parser.add_argument("--filter", default="", help="Additional URL filters")
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Revisit existing conference-year pages and append newly indexed papers",
    )
    parser.add_argument("--threads", type=int, default=20, help="Crawler concurrency")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.issue:
        item = parse_issue(args.issue)
        run(
            confs_text=item["confs"],
            start_year=item["year"],
            filter_text=item.get("filter", ""),
            threads=args.threads,
            refresh_existing=args.refresh_existing,
        )
    else:
        if args.year is None:
            raise ValueError("--year is required with --confs")
        run(
            confs_text=args.confs,
            start_year=args.year,
            filter_text=args.filter,
            threads=args.threads,
            refresh_existing=args.refresh_existing,
        )


if __name__ == "__main__":
    main()
