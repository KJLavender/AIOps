from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ghdigest import jobs
from ghdigest.config import Config
from ghdigest.github import Repo, looks_like_lure
from ghdigest.notify import BOT_TAG, Listener
from ghdigest.service import DigestService
from ghdigest.store import Store
from ghdigest.web import state

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _repo(name, stars=100, desc="a tool", topics=()):
    return Repo(full_name=name, url=f"https://github.com/{name}", description=desc,
                stars=stars, language="Python", topics=list(topics))


class _Client:
    def __init__(self, results):
        self.results = results  # query substring -> list[Repo]
        self.queries = []

    def search(self, query, per_page=30, sort="stars"):
        self.queries.append(query)
        for key, repos in self.results.items():
            if key in query:
                return [Repo(**r.to_dict()) for r in repos]
        return []


def _cfg(**kw):
    return Config(ollama_endpoint="", ntfy_url="", **kw)


def test_lure_filter():
    assert looks_like_lure(_repo("x/Valorant-Cheat-2026"))
    assert looks_like_lure(_repo("x/tool", desc="Free download activator"))
    assert not looks_like_lure(_repo("x/k8s-healer", desc="self-healing operator"))


def test_daily_top_queries_last_24h_and_skips_lures():
    bait = _repo("x/Dlss-5-Medusa-Manager", 800, desc="Manager for games")
    bait.language = ""
    client = _Client({"created:>=": [_repo("a/cheat-menu", 900), bait, _repo("b/good", 500), _repo("c/fine", 300)]})
    repos = jobs.daily_top(client, _cfg(top_n=2), NOW)
    assert client.queries[0].startswith("created:>=2026-09-30T01:00:00Z")
    assert [r.full_name for r in repos] == ["b/good", "c/fine"]
    assert repos[0].summary == "a tool"  # no LLM -> description


def test_research_picks_round_robin_and_never_repeat():
    store = Store(":memory:")
    for t in ("aiops", "kubernetes llm"):
        store.add_topic(t)
    client = _Client({
        "aiops in:": [_repo("a/one", 900), _repo("a/two", 800), _repo("a/three", 700)],
        "kubernetes llm in:": [_repo("k/one", 50), _repo("a/one", 900)],
    })
    first = jobs.research_picks(client, _cfg(picks_n=3), store, NOW)
    assert [r.full_name for r in first] == ["a/one", "k/one", "a/two"]
    assert first[0].summary.startswith("［aiops］")
    second = jobs.research_picks(client, _cfg(picks_n=3), store, NOW)
    assert [r.full_name for r in second] == ["a/three"]  # nothing recommended twice


def test_format_digest_lists_repos():
    title, msg = jobs.format_digest("daily-top", [_repo("b/good", 1234)], "2026-10-01")
    assert title == "🔥 GitHub 今日新星 Top 1（10/01）"
    assert "1. b/good ★1,234 · Python" in msg


def test_topic_commands_and_defaults():
    svc = DigestService(_cfg(mode="research-picks"), Store(":memory:"), _Client({}))
    assert len(svc.store.topics()) == 5  # seeded from the research defaults
    assert "已新增主題「eBPF」" in svc.handle_text("新增主題 eBPF", "web")
    assert "eBPF" in svc.handle_text("主題", "web")
    topic_id = [i for i, q in svc.store.topics() if q == "eBPF"][0]
    assert "已刪除主題「eBPF」" in svc.handle_text(f"刪除主題 #{topic_id}", "web")


def test_schedule_and_catch_up():
    svc = DigestService(_cfg(run_at="09:00"), Store(":memory:"), _Client({}))
    assert svc.next_run(NOW.replace(hour=8)).hour == 9
    assert svc.next_run(NOW.replace(hour=10)).day == 2
    assert svc.due_on_start(NOW.replace(hour=10)) is True
    svc.store.save_digest("2026-10-01", [])
    assert svc.due_on_start(NOW.replace(hour=10)) is False
    assert svc.due_on_start(NOW.replace(hour=8)) is False


def test_run_saves_digest_and_state_has_dashboard_fields():
    client = _Client({"created:>=": [_repo("b/good", 1500)]})
    svc = DigestService(_cfg(), Store(":memory:"), client)
    svc.now = lambda: NOW
    svc.run("manual")
    st = state(svc)
    assert st["day"] == "2026-10-01"
    assert st["items"][0]["rank_name"] == "1. b/good"
    assert st["items"][0]["stars_text"] == "★1,500"


def test_listener_skips_own_messages():
    seen = []
    listener = Listener("http://ntfy", "repo-picks", lambda t, s: seen.append(t) or "")
    listener.on_line(b'{"event":"message","id":"1","message":"\\u4e3b\\u984c"}')
    listener.on_line(('{"event":"message","id":"2","message":"x","tags":["%s"]}' % BOT_TAG).encode())
    assert seen == ["主題"] and listener.since == "2"


def test_blurb_does_not_invent_without_description(monkeypatch):
    from ghdigest import llm

    monkeypatch.setattr(llm, "_ask", lambda prompt, config: "made up")
    cfg = Config(ollama_endpoint="http://x")
    assert llm.blurb(_repo("x/no-desc", desc=""), cfg) == "（作者沒有寫描述）"
    assert llm.blurb(_repo("x/has-desc", desc="a k8s tool"), cfg) == "made up"
