"""GitHub search API client (unauthenticated works; GITHUB_TOKEN raises limits)."""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Optional

log = logging.getLogger("ghdigest.github")

# Brand-new repos at the top of "stars today" are often malware lures with
# bought stars (game cheats, cracks, "free" tools). Skip the obvious ones.
_LURE_RE = re.compile(
    r"cheat|crack|keygen|aimbot|spoofer|injector|executor|activator|hwid|"
    r"mod[ -]?menu|free[ -]?download|unlocker|bypass|loader",
    re.IGNORECASE,
)


@dataclass
class Repo:
    full_name: str
    url: str
    description: str = ""
    stars: int = 0
    language: str = ""
    topics: list[str] = field(default_factory=list)
    created_at: str = ""
    pushed_at: str = ""
    summary: str = ""  # one-line zh-TW summary / reason, filled by the LLM

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_api(cls, item: dict) -> "Repo":
        return cls(
            full_name=item["full_name"],
            url=item["html_url"],
            description=(item.get("description") or "").strip(),
            stars=int(item.get("stargazers_count") or 0),
            language=item.get("language") or "",
            topics=list(item.get("topics") or []),
            created_at=item.get("created_at") or "",
            pushed_at=item.get("pushed_at") or "",
        )


def looks_like_lure(repo: Repo) -> bool:
    return bool(_LURE_RE.search(f"{repo.full_name} {repo.description}"))


class GitHubClient:
    API = "https://api.github.com/search/repositories"

    def __init__(self, token: str = "", min_interval_seconds: float = 6.5) -> None:
        self.token = token
        # Unauthenticated search allows 10 requests/minute.
        self.min_interval = 0.0 if token else min_interval_seconds
        self._last = 0.0

    def search(self, query: str, per_page: int = 30, sort: str = "stars") -> list[Repo]:
        wait = self._last + self.min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        params = urllib.parse.urlencode(
            {"q": query, "sort": sort, "order": "desc", "per_page": per_page})
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "aiops-github-digest",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(f"{self.API}?{params}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        finally:
            self._last = time.time()
        return [Repo.from_api(item) for item in data.get("items", [])]


def first(items: list[Repo], n: int, skip: Optional[set[str]] = None) -> list[Repo]:
    skip = skip or set()
    out = []
    for repo in items:
        if repo.full_name in skip or looks_like_lure(repo):
            continue
        out.append(repo)
        if len(out) >= n:
            break
    return out
