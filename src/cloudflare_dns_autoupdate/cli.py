"""Argparse entry point with environment defaults and explicit CLI precedence."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import __version__
from .app import Application
from .logging_setup import configure_logging


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI without creating clients or loading configuration."""
    parser = argparse.ArgumentParser(description="Reconcile Cloudflare DNS with local values.")
    parser.add_argument("--config", type=Path, default=os.getenv("CONFIG_PATH", "./config.yml"))
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.getenv("DRY_RUN", "").lower() in ("true", "1"),
        help="log writes without sending them",
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    parser.add_argument("--log-format", default=os.getenv("LOG_FORMAT", "text"))
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main() -> None:
    """Configure logging, run the application, and return its process exit status."""
    parser = build_parser()
    args = parser.parse_args()
    if args.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        parser.error("--log-level must be DEBUG, INFO, WARNING, or ERROR")
    if args.log_format not in ("text", "json"):
        parser.error("--log-format must be text or json")
    redaction = configure_logging(args.log_level, args.log_format)
    redaction.add_token(os.getenv("CLOUDFLARE_TOKEN", "").strip())
    app = Application(args.config, once=args.once, dry_run=args.dry_run, redaction=redaction)
    try:
        code = asyncio.run(app.run())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)
