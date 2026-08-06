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
    r"\blearning\s+to\s+rank\b",
    r"\bfeed\s+ranking\b",
    r"\bads?\s+(?:ranking|recommendation)\b",
    r"\badvertising\s+(?:ranking|recommendation)\b",
    r"\bpersonalized\s+(?:search|ranking|retrieval)\b",
    r"\bsearch\s+(?:ranking|reranking|re-ranking|retrieval)\b",
)
STRONG_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in STRONG_PATTERNS)

RECOMMENDATION_RE = re.compile(r"\brecommendations?\b", re.IGNORECASE)
USER_ITEM_CONTEXT = (
    "user",
    "item",
    "content",
    "feed",
    "click",
    "conversion",
    "personalized",
    "personalization",
    "e-commerce",
    "ecommerce",
)
RANKING_ACTIONS = ("ranking", "reranking", "re-ranking", "retrieval", "matching")


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
    score = 0
    for pattern in STRONG_RE:
        if pattern.search(body_text):
            score += 6 if pattern.search(title_text) else 4

    has_recommendation = bool(RECOMMENDATION_RE.search(body_text))
    context_hits = sum(word in body_text for word in USER_ITEM_CONTEXT)
    action_hits = sum(word in body_text for word in RANKING_ACTIONS)
    if has_recommendation and context_hits:
        score += 5 + min(context_hits, 3)
    if action_hits and context_hits >= 2:
        score += 4 + min(action_hits, 2)
    return score


def is_recommendation_relevant(
    title: str, summary: str = "", tags: Iterable[str] = ()
) -> bool:
    return recommendation_relevance_score(title, summary, tags) >= 4
