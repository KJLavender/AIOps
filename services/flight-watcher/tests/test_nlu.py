from datetime import date

from flightwatch import airports
from flightwatch.config import Config
from flightwatch.nlu import parse, rule_parse

TODAY = date(2026, 10, 1)


def _rules(text):
    return rule_parse(text, TODAY, "台北")


def test_resolve_city_alias_and_iata():
    assert airports.resolve("東京") == ("東京", ["NRT", "HND"])
    assert airports.resolve("kix") == ("大阪", ["KIX"])
    assert airports.resolve("Tokyo") == ("東京", ["NRT", "HND"])
    assert airports.resolve("火星") is None


def test_change_destination_adds_route():
    cmd = _rules("我想要更改目的地是台北到東京")
    assert cmd.action == "add"
    assert (cmd.origin, cmd.destination) == ("台北", "東京")


def test_destination_only_uses_default_origin():
    cmd = _rules("幫我找去大阪的便宜機票")
    assert (cmd.action, cmd.origin, cmd.destination) == ("add", "台北", "大阪")


def test_longest_alias_wins():
    cmd = _rules("台北松山飛首爾")
    assert airports.resolve(cmd.origin) == ("松山", ["TSA"])
    assert cmd.destination == "首爾"


def test_price_month_stay_and_nonstop():
    cmd = _rules("高雄飛大阪 12月 來回5天 5,000以下 直飛")
    assert (cmd.origin, cmd.destination) == ("高雄", "大阪")
    assert (cmd.date_from, cmd.date_to) == ("2026-12-01", "2026-12-31")
    assert cmd.stay_days == 5
    assert cmd.max_price == 5000
    assert cmd.nonstop is True


def test_past_month_rolls_to_next_year():
    cmd = _rules("台北到東京 3月")
    assert cmd.date_from == "2027-03-01"


def test_explicit_date_range():
    cmd = _rules("台北到首爾 12/20-12/28")
    assert (cmd.date_from, cmd.date_to) == ("2026-12-20", "2026-12-28")


def test_replace_list_remove_check_target_help():
    assert _rules("只追蹤台北到首爾").action == "replace"
    assert _rules("列表").action == "list"
    remove = _rules("刪除 #2")
    assert (remove.action, remove.watch_id) == ("remove", 2)
    assert _rules("立即查詢").action == "check"
    target = _rules("#1 目標 4000")
    assert (target.action, target.watch_id, target.max_price) == ("set_target", 1, 4000)
    assert _rules("說明").action == "help"


def test_iata_codes_in_text():
    cmd = _rules("TPE to NRT")
    assert airports.resolve(cmd.origin) == ("台北", ["TPE"])
    assert airports.resolve(cmd.destination) == ("東京", ["NRT"])


def test_latin_alias_needs_word_boundary():
    # "la" (Los Angeles) must not match inside other words.
    cmd = _rules("plan a trip to 東京")
    assert cmd.destination == "東京"
    assert cmd.origin == "台北"


def test_parse_without_llm_falls_back_to_rules():
    cmd = parse("台北到東京", TODAY, Config(ollama_endpoint=""))
    assert (cmd.action, cmd.parser) == ("add", "rules")


def test_parse_uses_llm_and_fills_gaps(monkeypatch):
    from flightwatch import nlu

    monkeypatch.setattr(nlu, "llm_parse", lambda text, today, config: nlu.Command(
        action="add", origin=None, destination="東京", parser="llm"))
    cmd = parse("想去東京玩 5000以下", TODAY, Config(ollama_endpoint="http://x"))
    assert cmd.parser == "llm"
    assert (cmd.origin, cmd.destination, cmd.max_price) == ("台北", "東京", 5000)


def test_parse_rejects_llm_hallucinated_place(monkeypatch):
    from flightwatch import nlu

    monkeypatch.setattr(nlu, "llm_parse", lambda text, today, config: nlu.Command(
        action="add", origin="台北", destination="亞特蘭提斯", parser="llm"))
    cmd = parse("台北到東京", TODAY, Config(ollama_endpoint="http://x"))
    assert (cmd.parser, cmd.destination) == ("rules", "東京")


def test_rule_dates_override_llm_dates(monkeypatch):
    from flightwatch import nlu

    monkeypatch.setattr(nlu, "llm_parse", lambda text, today, config: nlu.Command(
        action="add", origin="高雄", destination="大阪", date_from="2026-12-01",
        date_to="2027-01-01", stay_days=5, max_price=5000, parser="llm"))
    cmd = parse("高雄飛大阪 12月 來回5天 5000以下", TODAY, Config(ollama_endpoint="http://x"))
    assert (cmd.date_from, cmd.date_to) == ("2026-12-01", "2026-12-31")


def test_year_aware_months():
    assert _rules("2027/3月").date_from == "2027-03-01"
    assert (_rules("2027年3月").date_from, _rules("2027年3月").date_to) == ("2027-03-01", "2027-03-31")
    assert _rules("明年3月").date_from == "2027-03-01"
    assert _rules("台北到東京 2027/3").date_from == "2027-03-01"
    assert _rules("台北到首爾 2026-12-20~2026-12-28").date_from == "2026-12-20"  # ISO range still wins


def test_next_year_without_month_is_vague():
    assert _rules("明年的機票").vague_year is True
    assert _rules("明年3月").vague_year is False
