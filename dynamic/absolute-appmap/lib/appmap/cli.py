"""appmap command-line entry point."""
import argparse
import asyncio
import json
import logging
import shlex
import sys
from dataclasses import replace
from pathlib import Path

from appmap import __version__

COMMANDS = ["init", "auth", "crawl", "probe", "approvals", "build", "status", "query"]
IMPLEMENTED = {"init", "auth", "crawl", "probe"}

log = logging.getLogger("appmap.cli")


def _q(path) -> str:
    """Shell-quote a path for a copy-pasteable ``next`` command."""
    return shlex.quote(str(path))


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
    p = sub.add_parser("init", help="create a run directory from a config file")
    p.add_argument("--config", required=True, help="run configuration YAML")
    p.add_argument("--out", default=".", help="parent directory for the run directory")
    p = sub.add_parser("auth", help="log in each role and save its session")
    p.add_argument("--run", required=True, help="run directory")
    p.add_argument("--role", help="only this role")
    p.add_argument("--manual-login", action="store_true",
                   help="log in by hand in a headed browser (SSO/MFA), then press Enter")
    p = sub.add_parser("crawl", help="browser crawl (resumable; stops at the budget)")
    p.add_argument("--run", required=True, help="run directory")
    p.add_argument("--role", help="only this role")
    p.add_argument("--budget-minutes", type=float, default=8.0,
                   help="stop cleanly after this long (default 8); rerun to resume")
    p.add_argument("--approved-only", action="store_true",
                   help="only execute actions approved in approvals.yaml")
    p = sub.add_parser("probe", help="direct-request discovery and cross-role replay")
    p.add_argument("--run", required=True, help="run directory")
    p.add_argument("--phase", required=True, choices=["passive", "active"],
                   help="passive: robots, sitemaps, well-known, OpenAPI, GraphQL introspection, "
                        "JS bundles; active: API replay, forced browsing (if enabled), "
                        "cross-role replay")
    p.add_argument("--role", help="probe as this role (default: the first configured role; "
                                  "active: API replay for every role)")
    p.add_argument("--budget-minutes", type=float, default=8.0,
                   help="stop cleanly after this long (default 8); rerun to resume")
    for name in COMMANDS:
        if name not in IMPLEMENTED:
            sub.add_parser(name, help=f"{name} (not yet implemented)")
    return parser


# -- run-directory plumbing ------------------------------------------------------
class _RedactingFormatter(logging.Formatter):
    def __init__(self, redactor):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.text(super().format(record))


def _attach_log(rd, redactor) -> logging.Handler:
    """Append a human-oriented, redacted log to <run>/crawl.log."""
    handler = logging.FileHandler(rd.log_path, encoding="utf-8")
    handler.setFormatter(_RedactingFormatter(redactor))
    root = logging.getLogger("appmap")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return handler


def _detach_log(handler: logging.Handler | None) -> None:
    if handler is not None:
        logging.getLogger("appmap").removeHandler(handler)
        handler.close()


def _open_run(path: str):
    from appmap.config import load_config
    from appmap.rundir import open_run_dir

    rd = open_run_dir(Path(path))
    try:
        meta = json.loads((rd.root / "run.json").read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"{rd.root}: run.json missing or unreadable ({e}); run 'appmap init'")
    cfg_path = meta.get("config_path")
    if not cfg_path:
        raise ValueError(f"{rd.root}/run.json has no config_path; run 'appmap init'")
    return rd, load_config(Path(cfg_path)), meta


def _write_meta(rd, cfg, meta: dict, status: str) -> None:
    from appmap.rundir import write_run_meta

    write_run_meta(rd, cfg, status=status, extra={"config_path": meta["config_path"]})


# -- commands -------------------------------------------------------------------
def cmd_init(args) -> int:
    from appmap.config import ConfigError, load_config
    from appmap.crawl import make_redactor
    from appmap.rundir import create_run_dir, write_run_meta
    from appmap.store import Store

    cfg_path = Path(args.config).resolve()
    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        emit_status("init", ok=False, status="config_error", errors=[str(e)])
        return 1
    rd = create_run_dir(cfg, Path(args.out).absolute())
    handler = _attach_log(rd, make_redactor(cfg))
    try:
        store = Store(rd.db_path)
        store.init_schema()
        store.close()
        write_run_meta(rd, cfg, status="initialized", extra={"config_path": str(cfg_path)})
        log.info("init: run %s for %s (profile %s, roles %s)", rd.run_id, cfg.base_urls,
                 cfg.profile, [r.name for r in cfg.roles])
    finally:
        _detach_log(handler)
    print(rd.root)
    emit_status("init", ok=True, status="initialized",
                counts={"roles": len(cfg.roles)}, next=f"appmap auth --run {_q(rd.root)}")
    return 0


def manual_login_roles(roles, *, explicit: bool):
    """``--manual-login``: an explicitly named role is converted whatever its type;
    otherwise only interactive roles (form/manual), never none/storage_state."""
    return [replace(r, login=replace(r.login, type="manual"))
            if explicit or r.login.type in ("form", "manual") else r for r in roles]


async def _auth(cfg, rd, roles, headed: bool) -> dict[str, bool]:
    from playwright.async_api import async_playwright

    from appmap.browser.session import login, open_role_session
    from appmap.crawl import build_runtime

    rt = build_runtime(cfg, rd)
    results: dict[str, bool] = {}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            try:
                for role in roles:
                    sess = await open_role_session(browser, cfg, rd, role, rt.recorder)
                    try:
                        results[role.name] = await login(sess, cfg, rt.events)
                        log.info("auth: role %s (%s) -> %s", role.name, role.login.type,
                                 "ok" if results[role.name] else "FAILED")
                    finally:
                        await rt.recorder.drain()
                        await sess.context.close()
                        rt.recorder.finalize_har(role.name)
            finally:
                await browser.close()
    finally:
        rt.store.close()
    return results


def cmd_auth(args) -> int:
    from appmap.crawl import make_redactor, select_roles

    try:
        rd, cfg, meta = _open_run(args.run)
        roles = select_roles(cfg, [args.role] if args.role else None)
    except Exception as e:
        emit_status("auth", ok=False, status="error", errors=[str(e)])
        return 1
    if args.manual_login:
        roles = manual_login_roles(roles, explicit=bool(args.role))
    headed = any(r.login.type == "manual" for r in roles)
    handler = _attach_log(rd, make_redactor(cfg))
    try:
        results = asyncio.run(_auth(cfg, rd, roles, headed))
        ok = bool(results) and all(results.values())
        _write_meta(rd, cfg, meta, "authenticated" if ok else "auth_failed")
    except Exception as e:
        log.exception("auth failed")
        emit_status("auth", ok=False, status="error", errors=[str(e)])
        return 1
    finally:
        _detach_log(handler)
    failed = [n for n, v in results.items() if not v]
    emit_status("auth", ok=ok, status="authenticated" if ok else "auth_failed",
                counts={"roles": len(results), "roles_ok": len(results) - len(failed)},
                errors=[f"role {n}: login failed" for n in failed],
                next=f"appmap crawl --run {_q(rd.root)}" if ok else None)
    return 0 if ok else 1


def cmd_crawl(args) -> int:
    from appmap.crawl import make_redactor, run_crawl, select_roles

    try:
        rd, cfg, meta = _open_run(args.run)
        names = [r.name for r in select_roles(cfg, [args.role] if args.role else None)]
    except Exception as e:
        emit_status("crawl", ok=False, status="error", errors=[str(e)])
        return 1
    handler = _attach_log(rd, make_redactor(cfg))
    try:
        stats = asyncio.run(run_crawl(cfg, rd, roles=names, budget_minutes=args.budget_minutes,
                                      approved_only=args.approved_only))
        _write_meta(rd, cfg, meta, stats.status)
    except Exception as e:
        log.exception("crawl failed")
        emit_status("crawl", ok=False, status="error", errors=[str(e)])
        return 1
    finally:
        _detach_log(handler)
    errors = list(stats.warnings)
    run = _q(rd.root)
    again = f"appmap crawl --run {run}" + (f" --role {_q(args.role)}" if args.role else "")
    nxt = {
        "in_progress": again,
        "done": (f"appmap approvals --run {run} write" if stats.queued_approvals
                 else f"appmap probe --run {run} --phase active"),
        "limit_reached": f"appmap build --run {run}",
        "paused_circuit": None,
        "not_implemented": None,
    }[stats.status]
    ok = stats.status in ("done", "in_progress", "limit_reached")
    if stats.status == "paused_circuit":
        errors.append("circuit breaker tripped (too many 429/5xx responses); "
                      "check the target before resuming")
    emit_status("crawl", ok=ok, status=stats.status,
                counts={"states": stats.states, "transitions": stats.transitions,
                        "forms": stats.forms, "exchanges": stats.exchanges,
                        "queued_approvals": stats.queued_approvals},
                pending_approvals=stats.queued_approvals, errors=errors, next=nxt)
    return 0 if ok else 1


async def _probe_passive(cfg, rd, role_name: str, budget_minutes: float) -> dict[str, int]:
    import functools
    import time

    from playwright.async_api import async_playwright

    from appmap.browser.session import state_path
    from appmap.crawl import build_runtime
    from appmap.direct import DirectClient
    from appmap.frontier import Frontier
    from appmap.probe.bundles import probe_bundles
    from appmap.probe.passive import probe_passive
    from appmap.probe.schemas import probe_schemas
    from appmap.recording import record_exchange

    deadline = time.monotonic() + budget_minutes * 60
    rt = build_runtime(cfg, rd)
    counts: dict[str, int] = {"timed_out": 0}
    try:
        known = [r["pattern"] for r in rt.store.conn.execute(
            "SELECT pattern FROM routes WHERE kind='spa_route'")]
        rec = functools.partial(record_exchange, rt.store, rt.events, rt.redactor, rd,
                                known_patterns=known)
        client = DirectClient(rt.gate, rec, rt.events)
        kwargs = {}
        saved = state_path(rd, role_name)
        if saved.exists():  # authenticated schemas/sitemaps need the role's session
            st = json.loads(saved.read_text(encoding="utf-8"))
            kwargs["storage_state"] = {"cookies": st.get("cookies", []),
                                       "origins": st.get("origins", [])}
        async with async_playwright() as pw:
            api = await pw.request.new_context(**kwargs)
            try:
                steps = []
                if cfg.discovery.passive_sources:
                    steps.append(lambda: probe_passive(client, api, cfg, rt.store,
                                                       Frontier(rt.store), role_name,
                                                       deadline=deadline))
                if cfg.discovery.schemas:
                    steps.append(lambda: probe_schemas(client, api, cfg, rt.store, role_name,
                                                       deadline=deadline))
                if cfg.discovery.bundle_mining:
                    steps.append(lambda: probe_bundles(client, api, cfg, rt.store,
                                                       Frontier(rt.store), role_name,
                                                       deadline=deadline))
                for step in steps:
                    for k, v in (await step()).items():
                        counts[k] = max(counts.get(k, 0), v) if k == "timed_out" \
                            else counts.get(k, 0) + v
            finally:
                await api.dispose()
    finally:
        rt.store.close()
    return counts


def cmd_probe(args) -> int:
    from appmap.crawl import make_redactor, select_roles
    from appmap.throttle import CircuitOpen

    try:
        rd, cfg, meta = _open_run(args.run)
        role = select_roles(cfg, [args.role or cfg.roles[0].name])[0]
    except Exception as e:
        emit_status("probe", ok=False, status="error", errors=[str(e)])
        return 1
    run = _q(rd.root)
    handler = _attach_log(rd, make_redactor(cfg))
    warnings: list[str] = []
    pending = 0
    try:
        if args.phase == "active":
            from appmap.probe.replay import run_active

            res = asyncio.run(run_active(cfg, rd, roles=[role.name] if args.role else None,
                                         probe_role=role.name,
                                         budget_minutes=args.budget_minutes))
            counts, warnings, pending = res.counts, res.warnings, res.pending_approvals
        else:
            counts = asyncio.run(_probe_passive(cfg, rd, role.name, args.budget_minutes))
    except CircuitOpen as e:
        log.warning("probe: %s", e)
        emit_status("probe", ok=False, status="paused_circuit",
                    errors=["circuit breaker tripped (too many 429/5xx responses); "
                            "check the target before resuming"])
        return 1
    except Exception as e:
        log.exception("probe failed")
        emit_status("probe", ok=False, status="error", errors=[str(e)])
        return 1
    finally:
        _detach_log(handler)
    status = "in_progress" if counts.get("timed_out") else "done"
    again = f"appmap probe --run {run} --phase {args.phase}" + (
        f" --role {_q(args.role)}" if args.role else "")
    if status == "in_progress":
        nxt = again
    elif args.phase == "passive":
        nxt = f"appmap crawl --run {run}"
    else:
        nxt = (f"appmap approvals --run {run} write" if pending
               else f"appmap build --run {run}")
    emit_status("probe", ok=True, status=status,
                counts={k: v for k, v in counts.items() if k != "timed_out"},
                pending_approvals=pending, errors=warnings, next=nxt)
    return 0


HANDLERS = {"init": cmd_init, "auth": cmd_auth, "crawl": cmd_crawl, "probe": cmd_probe}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:  # argparse exits for --version / errors
        return e.code if isinstance(e.code, int) else 0
    if not args.command:
        parser.print_help()
        return 2
    handler = HANDLERS.get(args.command)
    if handler is None:
        emit_status(args.command, ok=False, status="not_implemented",
                    errors=[f"'{args.command}' is not implemented yet"])
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
