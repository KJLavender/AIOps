"""Entrypoint: `python -m flightwatch`."""
from __future__ import annotations

import logging
import threading

from .config import Config
from .listener import NtfyListener
from .notify import Notifier
from .search import FliSearcher
from .service import FlightWatcher
from .store import Store
from .web import serve


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # fli warns once per result about airports missing from its enum (e.g. OKA).
    logging.getLogger("fli.search._decoders").setLevel(logging.ERROR)
    config = Config.from_env()
    watcher = FlightWatcher(config, Store(config.db_path), FliSearcher(config), Notifier(config))
    serve(watcher, config.port)
    if config.ntfy_url:
        listener = NtfyListener(config.ntfy_url, config.ntfy_topic, watcher.handle_text)
        threading.Thread(target=listener.run_forever, name="ntfy", daemon=True).start()
    watcher.run_forever()


if __name__ == "__main__":
    main()
