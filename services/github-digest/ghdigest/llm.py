"""One-line Traditional Chinese blurbs via Ollama (falls back to the repo description)."""
from __future__ import annotations

import json
import logging
import urllib.request

from .config import Config
from .github import Repo

log = logging.getLogger("ghdigest.llm")

_WHAT = """用一句繁體中文（30 字以內）說明這個 GitHub 專案是做什麼的，只輸出那一句，不要加引號。
直接講它是什麼，不要用「這個專案」「這個 GitHub 專案」開頭，句型像「……的工具」「……的框架」。
只根據下面的資料，不要猜測沒寫到的功能。
名稱：{name}
描述：{desc}
主題標籤：{topics}
語言：{lang}"""

_WHY = """使用者在研究「用 LLM 讓 Kubernetes 自我修復的 AIOps」，這次的主題是「{topic}」。
用一句繁體中文（40 字以內）說明這個專案是什麼、對這個研究有什麼幫助，只輸出那一句，不要加引號。
直接講它是什麼，不要用「這個專案」開頭，句型像「……的平台，可參考……」。只根據下面的資料，不要猜測。
名稱：{name}
描述：{desc}
主題標籤：{topics}
語言：{lang}"""


def _ask(prompt: str, config: Config) -> str:
    payload = json.dumps({
        "model": config.ollama_model,
        "prompt": prompt,
        "stream": False,
        # Same num_ctx as the AIOps agent: Ollama reloads the model when it changes.
        "options": {"temperature": 0.2, "num_predict": 120, "num_ctx": 8192},
    }).encode("utf-8")
    req = urllib.request.Request(config.ollama_endpoint.rstrip("/") + "/api/generate",
                                 data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=config.llm_timeout_seconds) as resp:
        text = json.loads(resp.read().decode("utf-8")).get("response", "")
    return text.strip().strip("「」\"'").splitlines()[0][:120] if text.strip() else ""


def blurb(repo: Repo, config: Config, topic: str = "") -> str:
    """zh-TW one-liner: what it is (daily) or why it matters for the research (picks)."""
    fallback = repo.description or "（作者沒有寫描述）"
    # Nothing to summarise: an LLM would only invent a purpose from the name.
    if not config.ollama_endpoint or not (repo.description or repo.topics):
        return fallback
    template = _WHY if topic else _WHAT
    prompt = template.format(name=repo.full_name, desc=repo.description or "（無）",
                             topics=", ".join(repo.topics) or "（無）",
                             lang=repo.language or "（未知）", topic=topic)
    try:
        return _ask(prompt, config) or fallback
    except (OSError, ValueError) as exc:
        log.warning("LLM blurb failed for %s: %s", repo.full_name, exc)
        return fallback
