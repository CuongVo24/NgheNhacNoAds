"""Command-line entry point.

Only ``--version`` exists during milestone M0. The real commands arrive in M1/M2
(see docs/SPEC.md, section 6.14).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from nghenhacnoads import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nghenhacnoads",
        description="Ad-free YouTube Music player for the terminal (in development).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
