"""Command-line entrypoint: `python -m aiops`."""
from __future__ import annotations

import argparse
import logging
import os
import tempfile
import time

from .app import Agent
from .config import Config

log = logging.getLogger("aiops")
_DEMO_FIRST_SCAN_DELAY = 10


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Ops Agent for Kubernetes")
    parser.add_argument("--once", action="store_true", help="run a single scan and exit")
    parser.add_argument("--dry-run", action="store_true", help="diagnose but do not apply patches")
    parser.add_argument("--no-auto-fix", action="store_true", help="recommendations only")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="run against a built-in fake cluster (no kubectl needed)",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="serve a live web dashboard (default http://localhost:8080)",
    )
    parser.add_argument("--port", type=int, help="dashboard port (overrides AIOPS_DASHBOARD_PORT)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    _configure_logging(args.log_level)

    config = Config.from_env()
    if args.dry_run:
        config.dry_run = True
    if args.no_auto_fix:
        config.auto_fix = False
    if args.port is not None:
        config.dashboard_port = args.port

    kube = None
    if args.demo:
        from .fakes import FakeKubeClient

        kube = FakeKubeClient()
        # Keep the demo snappy: skip the real stability wait.
        config.validation_stability_seconds = 0
        config.validation_poll_seconds = 0
        # Don't let demo runs append fake cases to the real knowledge base.
        if not os.getenv("AIOPS_KB_PATH"):
            config.kb_path = os.path.join(tempfile.gettempdir(), "aiops-demo-kb.jsonl")

    agent = Agent(config, kube=kube)

    if args.dashboard:
        from .dashboard import Dashboard

        Dashboard(agent, config.dashboard_host, config.dashboard_port).start()
        if args.demo:
            # Give the viewer a moment to open the page and see the broken pods first.
            log.info("demo: first scan in %ss", _DEMO_FIRST_SCAN_DELAY)
            time.sleep(_DEMO_FIRST_SCAN_DELAY)
        if args.once:
            agent.tick()
            log.info("scan done; dashboard stays up (Ctrl+C to exit)")
            while True:
                time.sleep(3600)
        agent.run_forever()
    elif args.once or args.demo:
        agent.tick()
    else:
        agent.run_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
