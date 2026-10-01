"""The two daily digests."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .config import Config
from .github import GitHubClient, Repo, first, looks_like_lure
from .llm import blurb
from .store import Store

log = logging.getLogger("ghdigest.jobs")


def daily_top(client: GitHubClient, config: Config, now: datetime) -> list[Repo]:
    """Repos created in the last 24h with the most stars (lures filtered out)."""
    since = (now.astimezone(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Fetch extra so the lure filter still leaves top_n.
    candidates = client.search(f"created:>={since} archived:false", per_page=40)
    # A day-old repo with no detected language is a README + download link:
    # the usual shape of malware bait with bought stars.
    repos = first([r for r in candidates if r.language], config.top_n)
    for repo in repos:
        repo.summary = blurb(repo, config)
    return repos


def research_picks(client: GitHubClient, config: Config, store: Store, now: datetime) -> list[Repo]:
    """A few repos per day matching the research topics, never repeating one."""
    topics = [q for _, q in store.topics()]
    if not topics:
        return []
    utc = now.astimezone(timezone.utc)
    active = (utc - timedelta(days=60)).date().isoformat()
    young = (utc - timedelta(days=180)).date().isoformat()
    seen = store.seen()

    per_topic: dict[str, list[Repo]] = {}
    for topic in topics:
        scope = f"{topic} in:name,description,topics archived:false"
        rising = client.search(f"{scope} created:>={young} stars:>=10", per_page=15)
        established = client.search(f"{scope} pushed:>={active} stars:>=50", per_page=15)
        merged, names = [], set()
        # Rising first: newer projects are what a daily feed should surface.
        for repo in rising + established:
            if repo.full_name not in names and repo.full_name not in seen and not looks_like_lure(repo):
                names.add(repo.full_name)
                merged.append(repo)
        per_topic[topic] = merged

    # Round-robin across topics so one busy topic can't take every slot.
    picks: list[tuple[Repo, str]] = []
    chosen: set[str] = set()
    while len(picks) < config.picks_n and any(per_topic.values()):
        for topic in topics:
            queue = per_topic.get(topic) or []
            while queue and queue[0].full_name in chosen:
                queue.pop(0)
            if queue and len(picks) < config.picks_n:
                repo = queue.pop(0)
                chosen.add(repo.full_name)
                picks.append((repo, topic))

    repos = []
    for repo, topic in picks:
        repo.summary = f"［{topic}］{blurb(repo, config, topic)}"
        repos.append(repo)
    store.mark_seen([r.full_name for r in repos])
    return repos


def format_digest(mode: str, repos: list[Repo], day: str) -> tuple[str, str]:
    """(title, message) for the push notification."""
    md = day[5:].replace("-", "/")
    if mode == "daily-top":
        title = f"🔥 GitHub 今日新星 Top {len(repos)}（{md}）"
    else:
        title = f"🧭 今日研究推薦 {len(repos)} 個（{md}）"
    lines = []
    for i, r in enumerate(repos, 1):
        lang = f" · {r.language}" if r.language else ""
        lines.append(f"{i}. {r.full_name} ★{r.stars:,}{lang}\n   {r.summary}")
    return title, "\n".join(lines) or "今天沒有符合條件的 repo。"
