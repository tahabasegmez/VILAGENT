"""The apps and web domains a task touched: the exact keys lessons and notes are filed under."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_URL = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)
# Not preceded by "@": the domain of an e-mail address is not a site the task visits.
_BARE_DOMAIN = re.compile(r"(?<![@\w.-])((?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|app|edu|gov|co|ai|de|uk|tr|fr|es|it|nl)(?:\.[a-z]{2})?)\b", re.IGNORECASE)


def domain_key(url_or_host: str) -> str | None:
    host = urlparse(url_or_host if "://" in url_or_host else f"https://{url_or_host}").hostname or ""
    host = host.lower().removeprefix("www.")
    return host or None


def app_key(name: str) -> str | None:
    name = " ".join(str(name or "").split()).lower()
    return name or None


def extract(prompt: str, plan: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """(apps, domains) from the plan's launch/visit arguments and any URLs or domains in the prompt."""
    apps: set[str] = set()
    domains: set[str] = set()
    for step in (plan or {}).get("steps") or []:
        args = step.get("args") or {}
        if key := app_key(args.get("app_name") or ""):
            apps.add(key)
        if args.get("url") and (key := domain_key(str(args["url"]))):
            domains.add(key)
    for match in [*_URL.findall(prompt or ""), *_BARE_DOMAIN.findall(prompt or "")]:
        if key := domain_key(match):
            domains.add(key)
    return sorted(apps), sorted(domains)
