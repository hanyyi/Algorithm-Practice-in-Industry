"""High-precision relevance checks for recommendation-system digests."""

from __future__ import annotations

import re
from typing import Iterable


STRONG_PATTERNS = (
    r"\brecommender(?:\s+systems?)?\b",
    r"\brecommendation\s+(?:systems?|models?|algorithms?|engines?|tasks?|ranking|retrieval)\b",
    r"\bcollaborative\s+filtering\b",
    r"\bsequential\s+recommendation\b",
    r"\bsession[- ]based\s+recommendation\b",
    r"\bpersonalized\s+recommendation\b",
    r"\b(?:product|item|content|news|video|music|feed)\s+recommendation\b",
    r"\bcandidate\s+generation\b",
    r"\buser[-– ]item\s+interaction",
    r"\bclick[- ]through\s+rate\b",
    r"\bctr\s+prediction\b",
    r"\bcvr\s+prediction\b",
    r"\bconversion[- ]rate\s+prediction\b",
    r"\bfeed\s+ranking\b",
    r"\bads?\s+(?:ranking|recommendation)\b",
    r"\badvertising\s+(?:ranking|recommendation)\b",
    r"\bpersonalized\s+(?:search|ranking|retrieval)\b",
    r"\bsearch\s+(?:ranking|reranking|re-ranking|retrieval)\b",
    r"\b(?:digital\s+)?product\s+advisors?\b",
    r"\bconversational\s+commerce\b",
)
STRONG_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in STRONG_PATTERNS)

TASK_TITLE_RE = re.compile(
    r"\b(?:recommen(?:dation|der)|rank(?:ing)?|rerank(?:ing)?|re-ranking|"
    r"retrieval|matching|search|ads?|advertising|feed|click|conversion|"
    r"candidate\s+generation|product\s+advisors?|conversational\s+commerce)\b",
    re.IGNORECASE,
)
LEARNING_TO_RANK_RE = re.compile(r"\blearning\s+to\s+rank\b", re.IGNORECASE)
LTR_DOMAIN_RE = re.compile(
    r"\b(?:web\s+search|search\s+(?:engine|results?|ranking)|"
    r"information\s+retrieval|recommen(?:dation|der)|ads?|"
    r"advertising|feed|click|conversion|user[- ]item)\b",
    re.IGNORECASE,
)


def _qualifies(title_text: str, body_text: str) -> bool:
    """Require an explicit user-facing ranking/recommendation task.

    A domain phrase buried in an otherwise generic abstract is insufficient.
    This rejects papers that merely use recommendation experiments, generic
    RAG/retrieval, financial personalization, or learning-to-rank terminology
    for unrelated scientific workloads.
    """

    if any(pattern.search(title_text) for pattern in STRONG_RE):
        return True
    if (
        LEARNING_TO_RANK_RE.search(title_text)
        and LTR_DOMAIN_RE.search(body_text)
    ):
        return True
    return bool(
        TASK_TITLE_RE.search(title_text)
        and any(pattern.search(body_text) for pattern in STRONG_RE)
    )


def recommendation_relevance_score(
    title: str, summary: str = "", tags: Iterable[str] = ()
) -> int:
    """Return a conservative score; zero means the item is out of scope.

    Generic ML, LLM, agent, RAG, search, and retrieval mentions deliberately do
    not qualify by themselves. They must be tied to recommendation, user-item
    behavior, feed/ad ranking, or another explicit recommender-system signal.
    """

    title_text = " ".join(str(title or "").lower().split())
    body_text = " ".join(
        [title_text, str(summary or "").lower(), " ".join(str(tag).lower() for tag in tags)]
    )
    if not _qualifies(title_text, body_text):
        return 0

    score = 0
    for pattern in STRONG_RE:
        if pattern.search(body_text):
            score += 6 if pattern.search(title_text) else 4
    if LEARNING_TO_RANK_RE.search(title_text) and LTR_DOMAIN_RE.search(body_text):
        score += 6
    return score


def is_recommendation_relevant(
    title: str, summary: str = "", tags: Iterable[str] = ()
) -> bool:
    return recommendation_relevance_score(title, summary, tags) >= 4
