"""Public engagement and citation metrics used by daily digest ranking."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
TRACKING_PARAMS = {
    "source",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


class MetricBadRequest(RuntimeError):
    """A provider rejected one or more identifiers in a batch."""


def canonical_url(value: str) -> str:
    parts = urlsplit(str(value or "").strip())
    if not parts.netloc:
        return ""
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode(
        [
            (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
        ]
    )
    return urlunsplit((parts.scheme.lower() or "https", host, parts.path.rstrip("/"), query, ""))


def _request(method: str, url: str, *, timeout: int, **kwargs):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code == 400:
                raise MetricBadRequest(
                    f"HTTP 400: {response.text[:500]}"
                )
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise RuntimeError(
                    f"metric provider returned HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(
                    f"HTTP {response.status_code}: {response.text[:300]}", response=response
                )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"metric provider request failed: {last_error}") from last_error


def fetch_hn_metrics(since: datetime, timeout: int = 30) -> dict[str, dict]:
    """Return Hacker News points/comments keyed by canonical story URL."""
    cutoff = int(since.astimezone(timezone.utc).timestamp())
    response = _request(
        "GET",
        HN_SEARCH_URL,
        timeout=timeout,
        params={
            "tags": "story",
            "numericFilters": f"created_at_i>={cutoff}",
            "hitsPerPage": 1000,
        },
        headers={"User-Agent": "Algorithm-Practice-in-Industry/1.0"},
    )
    try:
        hits = response.json()["hits"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Hacker News returned an invalid metrics payload") from exc

    result: dict[str, dict] = {}
    for hit in hits:
        url = canonical_url(hit.get("url", ""))
        if not url:
            continue
        metrics = {
            "hn_points": int(hit.get("points") or 0),
            "hn_comments": int(hit.get("num_comments") or 0),
            "hn_id": str(hit.get("objectID") or ""),
        }
        previous = result.get(url)
        if previous is None or (
            metrics["hn_points"], metrics["hn_comments"]
        ) > (previous["hn_points"], previous["hn_comments"]):
            result[url] = metrics
    return result


def attach_hn_metrics(items: Iterable[dict], metrics: dict[str, dict], url_key: str) -> None:
    for item in items:
        item.update(
            metrics.get(
                canonical_url(item.get(url_key, "")),
                {"hn_points": 0, "hn_comments": 0, "hn_id": ""},
            )
        )


def fetch_s2_metrics(identifiers: Iterable[str], timeout: int = 30) -> dict[str, dict]:
    """Return Semantic Scholar citation metrics keyed by supplied identifier."""
    ids = [str(identifier) for identifier in identifiers if identifier]
    result: dict[str, dict] = {}
    fields = "title,citationCount,influentialCitationCount,publicationDate,externalIds"
    def collect(batch: list[str]) -> None:
        if not batch:
            return
        try:
            response = _request(
                "POST",
                S2_BATCH_URL,
                timeout=timeout,
                params={"fields": fields},
                json={"ids": batch},
                headers={"User-Agent": "Algorithm-Practice-in-Industry/1.0"},
            )
        except MetricBadRequest as exc:
            if len(batch) == 1:
                print(f"Warning: skipped invalid Semantic Scholar id {batch[0]}: {exc}")
                return
            midpoint = len(batch) // 2
            collect(batch[:midpoint])
            collect(batch[midpoint:])
            return
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Semantic Scholar returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise RuntimeError("Semantic Scholar returned an invalid metrics payload")
        for identifier, paper in zip(batch, payload):
            if not paper:
                continue
            result[identifier] = {
                "citation_count": int(paper.get("citationCount") or 0),
                "influential_citation_count": int(
                    paper.get("influentialCitationCount") or 0
                ),
                "publication_date": str(paper.get("publicationDate") or ""),
            }
    for start in range(0, len(ids), 500):
        collect(ids[start : start + 500])
    return result


def search_s2_conference_papers(
    *, year: int, venues: Iterable[str], limit: int = 100, timeout: int = 30
) -> list[dict]:
    """Find current-year conference papers already indexed by Semantic Scholar."""
    venue_filter = ",".join(dict.fromkeys(str(venue) for venue in venues if venue))
    if not venue_filter:
        return []
    response = _request(
        "GET",
        S2_SEARCH_URL,
        timeout=timeout,
        params={
            "query": (
                "recommendation OR recommender OR retrieval OR search OR ranking "
                "OR advertising OR personalization"
            ),
            "publicationDateOrYear": str(year),
            "publicationTypes": "Conference",
            "venue": venue_filter,
            "fields": (
                "title,abstract,year,venue,authors,url,externalIds,citationCount,"
                "influentialCitationCount,publicationDate"
            ),
            "sort": "citationCount:desc",
            "limit": min(max(1, limit), 1000),
        },
        headers={"User-Agent": "Algorithm-Practice-in-Industry/1.0"},
    )
    try:
        payload = response.json()["data"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Semantic Scholar returned an invalid search payload") from exc
    return [paper for paper in payload if isinstance(paper, dict) and paper.get("title")]
