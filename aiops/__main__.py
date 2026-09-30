"""Command-line entrypoint: `python -m aiops`."""
from __future__ import annotations

import argparse
import logging

from .app import Agent
from .config import Config


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
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    _configure_logging(args.log_level)

    config = Config.from_env()
    if args.dry_run:
        config.dry_run = True
    if args.no_auto_fix:
        config.auto_fix = False

    kube = None
    if args.demo:
        from .fakes import FakeKubeClient

        kube = FakeKubeClient()
        # Keep the demo snappy: skip the real stability wait.
        config.validation_stability_seconds = 0
        config.validation_poll_seconds = 0

    agent = Agent(config, kube=kube)
    if args.once or args.demo:
        agent.tick()
    else:
        agent.run_forever()


if __name__ == "__main__":
    main()
