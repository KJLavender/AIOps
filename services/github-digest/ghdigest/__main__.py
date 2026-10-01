"""Entrypoint: `python -m ghdigest` (MODE=daily-top or research-picks)."""
from __future__ import annotations

import logging
import threading

from .config import Config
from .github import GitHubClient
from .notify import Listener
from .service import DigestService
from .store import Store
from .web import serve


def main() -> None:
    logging.basicConfig(level=logging.INFO, datefmt="%Y-%m-%dT%H:%M:%S",
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    config = Config.from_env()
    svc = DigestService(config, Store(config.db_path), GitHubClient(config.github_token))
    serve(svc, config.port)
    if config.ntfy_url:
        listener = Listener(config.ntfy_url, config.ntfy_topic, svc.handle_text)
        threading.Thread(target=listener.run_forever, name="ntfy", daemon=True).start()
    svc.run_forever()


if __name__ == "__main__":
    main()
