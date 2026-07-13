"""``hermes eval`` subcommand parser."""

from __future__ import annotations

import argparse


def build_eval_parser(subparsers) -> None:
    """Attach the ``eval`` subcommand to ``subparsers``."""
    eval_parser = subparsers.add_parser(
        "eval",
        help="Run local Hermes evaluation and trace utilities",
        description=(
            "Local evaluation utilities for Hermes agent and gateway behavior. "
            "Use `hermes eval trace` to snapshot active profile work and write "
            "a deterministic trace artifact."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    hermes eval trace
    hermes eval trace --profiles kurumi asuna rikku yuna
    hermes eval trace --since-minutes 30 --log-lines 800
    hermes eval trace --json
""",
    )
    eval_sub = eval_parser.add_subparsers(dest="eval_command")

    trace_parser = eval_sub.add_parser(
        "trace",
        help="Snapshot active profile work and write a local eval trace",
    )
    trace_parser.add_argument(
        "--profiles",
        nargs="*",
        default=None,
        help="Profiles to include. Defaults to every named profile on disk.",
    )
    trace_parser.add_argument(
        "--since-minutes",
        type=int,
        default=120,
        help="Recent-log window for event scoring (default: 120).",
    )
    trace_parser.add_argument(
        "--log-lines",
        type=int,
        default=500,
        help="Gateway log tail lines to inspect per profile (default: 500).",
    )
    trace_parser.add_argument(
        "--message-limit",
        type=int,
        default=24,
        help="Recent active messages to include per routed session (default: 24).",
    )
    trace_parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for trace artifacts (default: <HERMES_HOME>/eval_traces).",
    )
    trace_parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print the summary without writing JSON/Markdown artifacts.",
    )
    trace_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full JSON trace to stdout instead of the compact summary.",
    )

    def _run_trace(args):  # noqa: ANN001
        from hermes_cli.eval_trace import cmd_trace

        return cmd_trace(args)

    trace_parser.set_defaults(func=_run_trace)

    def _print_help(args):  # noqa: ANN001
        eval_parser.print_help()
        return 0

    eval_parser.set_defaults(func=_print_help)
