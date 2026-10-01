"""City name / IATA code -> airport codes that Google Flights understands."""
from __future__ import annotations

import re
from typing import Optional

# (display name, airport codes, aliases). Chinese names first: that's how the
# user types. Codes are real airports (Google Flights has no city codes here).
_CITIES: list[tuple[str, list[str], list[str]]] = [
    ("台北", ["TPE"], ["臺北", "桃園", "taipei", "tpe"]),
    ("松山", ["TSA"], ["台北松山", "tsa"]),
    ("高雄", ["KHH"], ["kaohsiung", "khh"]),
    ("台中", ["RMQ"], ["臺中", "taichung", "rmq"]),
    ("東京", ["NRT", "HND"], ["tokyo", "成田", "羽田"]),
    ("大阪", ["KIX"], ["osaka", "關西", "关西", "京都"]),
    ("名古屋", ["NGO"], ["nagoya"]),
    ("福岡", ["FUK"], ["fukuoka"]),
    ("札幌", ["CTS"], ["sapporo", "北海道", "新千歲"]),
    ("仙台", ["SDJ"], ["sendai"]),
    ("廣島", ["HIJ"], ["广岛", "hiroshima"]),
    ("首爾", ["ICN", "GMP"], ["首尔", "seoul", "仁川", "金浦"]),
    ("釜山", ["PUS"], ["busan"]),
    ("濟州", ["CJU"], ["济州", "jeju"]),
    ("香港", ["HKG"], ["hong kong", "hk"]),
    ("澳門", ["MFM"], ["澳门", "macau"]),
    ("上海", ["PVG", "SHA"], ["shanghai"]),
    ("北京", ["PEK", "PKX"], ["beijing"]),
    ("曼谷", ["BKK", "DMK"], ["bangkok"]),
    ("清邁", ["CNX"], ["清迈", "chiang mai"]),
    ("普吉島", ["HKT"], ["普吉", "phuket"]),
    ("新加坡", ["SIN"], ["singapore"]),
    ("吉隆坡", ["KUL"], ["kuala lumpur"]),
    ("峇里島", ["DPS"], ["巴厘島", "峇里", "bali"]),
    ("峴港", ["DAD"], ["岘港", "da nang", "danang"]),
    ("胡志明市", ["SGN"], ["胡志明", "ho chi minh", "saigon"]),
    ("河內", ["HAN"], ["河内", "hanoi"]),
    ("馬尼拉", ["MNL"], ["马尼拉", "manila"]),
    ("宿霧", ["CEB"], ["宿雾", "cebu"]),
    ("雪梨", ["SYD"], ["悉尼", "sydney"]),
    ("墨爾本", ["MEL"], ["墨尔本", "melbourne"]),
    ("洛杉磯", ["LAX"], ["洛杉矶", "los angeles", "la"]),
    ("舊金山", ["SFO"], ["旧金山", "san francisco"]),
    ("西雅圖", ["SEA"], ["西雅图", "seattle"]),
    ("紐約", ["JFK", "EWR"], ["纽约", "new york", "nyc"]),
    ("溫哥華", ["YVR"], ["温哥华", "vancouver"]),
    ("倫敦", ["LHR"], ["伦敦", "london"]),
    ("巴黎", ["CDG"], ["paris"]),
    ("阿姆斯特丹", ["AMS"], ["amsterdam"]),
    ("法蘭克福", ["FRA"], ["法兰克福", "frankfurt"]),
]

_ALIASES: dict[str, tuple[str, list[str]]] = {}
for _name, _codes, _aliases in _CITIES:
    for _key in [_name, *_aliases]:
        _ALIASES[_key.lower()] = (_name, _codes)

_IATA_RE = re.compile(r"^[A-Za-z]{3}$")


def resolve(text: str) -> Optional[tuple[str, list[str]]]:
    """'東京' -> ('東京', ['NRT', 'HND']); 'kix' -> ('大阪', ['KIX']); unknown -> None."""
    key = (text or "").strip().lower()
    if not key:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    if _IATA_RE.match(key):
        code = key.upper()
        for name, codes, _ in _CITIES:
            if code in codes:
                return name, [code]
        return code, [code]
    return None


def known_names() -> list[str]:
    """Every name/alias, longest first (so '台北松山' wins over '台北' when scanning text)."""
    return sorted(_ALIASES, key=len, reverse=True)


def supported_codes(codes: list[str]) -> list[str]:
    """Drop codes missing from fli's Airport enum (e.g. OKA); keep all if fli is absent."""
    try:
        from fli.models import Airport
    except ImportError:
        return list(codes)
    return [c for c in codes if hasattr(Airport, c)]
