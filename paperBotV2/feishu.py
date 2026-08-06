"""Small Feishu custom-bot client shared by the daily push jobs."""

from __future__ import annotations

import json
import os
from typing import Iterable, List

import requests


def webhook_urls(value: str | None = None) -> List[str]:
    """Parse one or more comma-separated custom-bot webhook URLs."""
    raw_value = os.environ.get("FEISHU_URL", "") if value is None else value
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _response_error(response: requests.Response) -> str | None:
    if not response.ok:
        return f"HTTP {response.status_code}: {response.text[:500]}"

    try:
        payload = response.json()
    except ValueError:
        return f"response is not JSON: {response.text[:500]}"

    code = payload.get("StatusCode", payload.get("code", 0))
    if code != 0:
        message = payload.get("StatusMessage", payload.get("msg", "unknown error"))
        return f"Feishu code={code}: {message}"
    return None


def send_card(
    title: str,
    markdown: str,
    urls: Iterable[str] | None = None,
    *,
    color: str = "blue",
    dry_run: bool = False,
    timeout: int = 15,
) -> int:
    """Send a generic interactive card and fail if any webhook rejects it."""
    targets = list(webhook_urls() if urls is None else urls)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": markdown},
            }
        ],
    }
    body = {"msg_type": "interactive", "card": json.dumps(card, ensure_ascii=False)}

    if dry_run:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0

    if not targets:
        raise RuntimeError("FEISHU_URL is empty; configure it as a GitHub Actions secret")

    failures = []
    for index, url in enumerate(targets, start=1):
        try:
            response = requests.post(url, json=body, timeout=timeout)
            error = _response_error(response)
            if error:
                failures.append(f"[{index}/{len(targets)}] {error}")
            else:
                print(f"Feishu push [{index}/{len(targets)}] succeeded")
        except requests.RequestException as exc:
            failures.append(f"[{index}/{len(targets)}] request failed: {exc}")

    if failures:
        raise RuntimeError("Feishu push failed:\n" + "\n".join(failures))
    return len(targets)
