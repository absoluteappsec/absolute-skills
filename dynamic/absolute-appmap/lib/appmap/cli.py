"""appmap command-line entry point."""
import argparse
import json
import sys

from appmap import __version__

COMMANDS = ["init", "auth", "crawl", "probe", "approvals", "build", "status", "query"]


def emit_status(
    command: str,
    *,
    ok: bool,
    status: str,
    counts: dict | None = None,
    pending_approvals: int = 0,
    errors: list[str] | None = None,
    next: str | None = None,
) -> None:
    """Print the machine-readable final line every subcommand must end with."""
    obj = {
        "command": command,
        "ok": ok,
        "status": status,
        "counts": counts or {},
        "pending_approvals": pending_approvals,
        "errors": errors or [],
        "next": next,
    }
    print("APPMAP_STATUS " + json.dumps(obj), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="appmap", description="Web application crawler and mapper")
    parser.add_argument("--version", action="version", version=f"appmap {__version__}")
    sub = parser.add_subparsers(dest="command")
    for name in COMMANDS:
        sub.add_parser(name, help=f"{name} (not yet implemented)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # argparse exits for --version / errors
        return e.code if isinstance(e.code, int) else 0
    if not args.command:
        parser.print_help()
        return 2
    emit_status(args.command, ok=False, status="not_implemented",
                errors=[f"'{args.command}' is not implemented yet"])
    return 1


if __name__ == "__main__":
    sys.exit(main())
