"""Command-line interface for oremirror."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import TOOL_NAME, TOOL_VERSION
from .core import (
    ImageRef,
    OreError,
    build_plan,
    load_image_list,
    parse_image_list,
    parse_ref,
    pull_to_layout,
    push_layout,
    verify_layout,
)

# Bundled offline demo fixtures live next to the demo image list.
_DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "demos", "01-basic")


def _human_size(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.1f} {u}" if u != "B" else f"{int(size)} {u}"
        size /= 1024
    return f"{n} B"


def _load_fixtures(path: Optional[str]):
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _refs_from_args(args) -> List[ImageRef]:
    refs: List[ImageRef] = []
    if getattr(args, "imagelist", None):
        refs = load_image_list(args.imagelist)
    # Optional AI expansion (off unless a backend is configured + --ai given).
    if getattr(args, "ai", False) and getattr(args, "describe", None):
        from .ai import AIExpander
        exp = AIExpander()
        if exp.is_enabled():
            for ref_str in exp.expand(args.describe):
                try:
                    refs.append(parse_ref(ref_str))
                except OreError:
                    continue
        else:
            print("note: --ai requested but no AI backend configured "
                  "(set COGNIS_AI_BACKEND/ENDPOINT) — skipping expansion",
                  file=sys.stderr)
    if not refs:
        raise OreError("no images to operate on (provide an image list)")
    return refs


def _render_plan_table(plan) -> str:
    lines: List[str] = []
    lines.append(f"{TOOL_NAME} transfer plan — {len(plan.images)} image(s)")
    lines.append("=" * 72)
    for img in plan.images:
        if img.error:
            lines.append(f"[FAIL] {img.ref.canonical}")
            lines.append(f"       {img.error}")
            continue
        layers = len([b for b in img.blobs if b.kind == "layer"])
        lines.append(f"[ OK ] {img.ref.canonical}")
        lines.append(f"       manifest {img.manifest_digest}")
        lines.append(f"       {layers} layer(s), {len(img.blobs)} blob(s), "
                     f"{_human_size(img.total_size)}")
    lines.append("-" * 72)
    lines.append(
        f"TOTAL: {len(plan.images)} image(s), {plan.total_blobs} distinct blob(s), "
        f"{_human_size(plan.total_size)} to transfer")
    lines.append("RESULT: " + ("FAIL (unresolved refs)" if plan.failed else "OK"))
    return "\n".join(lines)


def _render_verify_table(res) -> str:
    lines: List[str] = []
    lines.append(f"{TOOL_NAME} verify — {res.layout}")
    lines.append("=" * 72)
    if res.passed:
        lines.append(f"All {res.ok}/{res.checked} digest(s) intact.")
    else:
        for p in res.problems:
            lines.append(f"[PROBLEM] {p}")
        lines.append("-" * 72)
        lines.append(f"{res.ok}/{res.checked} ok, {len(res.problems)} problem(s)")
    lines.append("RESULT: " + ("PASS" if res.passed else "FAIL"))
    return "\n".join(lines)


def _render_push_table(summary) -> str:
    lines: List[str] = []
    mode = "DRY-RUN (no network)" if summary["dry_run"] else "LIVE"
    lines.append(f"{TOOL_NAME} push — dest {summary['dest']} [{mode}]")
    lines.append("=" * 72)
    for act in summary["actions"]:
        if act["action"] == "blob":
            lines.append(f"  blob     {act['repository']}  {act['digest']}  "
                         f"({_human_size(act.get('size', 0))})")
        else:
            lines.append(f"  MANIFEST {act['repository']}:{act.get('tag','')}  "
                         f"{act['digest']}")
    lines.append("-" * 72)
    lines.append(f"{summary['manifests']} manifest(s), "
                 f"{len(summary['actions'])} action(s)")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="OCI registry mirror/sync for disconnected environments — "
                    "plan, pull to an OCI layout, verify, and push across the "
                    "air-gap.",
    )
    p.add_argument("--version", action="version",
                   version=f"{TOOL_NAME} {TOOL_VERSION}")
    sub = p.add_subparsers(dest="command")

    def _common_source(sp):
        sp.add_argument("imagelist", nargs="?",
                        help="Path to an image list (.txt or simple .yaml).")
        sp.add_argument("--ai", action="store_true",
                        help="Opt-in: expand --describe into image refs via a "
                             "configured local AI backend (default OFF).")
        sp.add_argument("--describe",
                        help="High-level app/stack name to expand when --ai is on.")
        sp.add_argument("--fixtures",
                        help="Offline manifest fixtures JSON (demo mode).")
        sp.add_argument("--insecure", action="store_true",
                        help="Use http:// instead of https:// for registries.")

    pl = sub.add_parser("plan", help="Resolve images to a transfer plan.")
    _common_source(pl)
    pl.add_argument("--format", choices=("table", "json"), default="table")
    pl.add_argument("--out", help="Write output to this file instead of stdout.")

    pu = sub.add_parser("pull", help="Download images into an OCI image-layout dir.")
    _common_source(pu)
    pu.add_argument("-o", "--out", required=True, help="Output OCI layout directory.")

    ve = sub.add_parser("verify", help="Recompute digests; confirm a layout is intact.")
    ve.add_argument("layout", help="Path to an OCI image-layout directory.")
    ve.add_argument("--format", choices=("table", "json"), default="table")

    ps = sub.add_parser("push", help="Upload an OCI layout to a destination registry.")
    ps.add_argument("layout", help="Path to an OCI image-layout directory.")
    ps.add_argument("--dest", required=True, help="Destination registry host.")
    ps.add_argument("--repository", help="Override destination repository name.")
    ps.add_argument("--dry-run", action="store_true",
                    help="Print the upload plan without touching the network.")
    ps.add_argument("--insecure", action="store_true")
    ps.add_argument("--format", choices=("table", "json"), default="table")

    sub.add_parser("mcp", help="Run as an MCP server (stdio JSON-RPC).")
    return p


def _emit(text: str, out: Optional[str]) -> None:
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        print(f"wrote {out}", file=sys.stderr)
    else:
        print(text)


def _run_plan(args) -> int:
    fixtures = _load_fixtures(args.fixtures)
    refs = _refs_from_args(args)
    plan = build_plan(refs, fixtures=fixtures, insecure=args.insecure)
    if args.format == "json":
        _emit(json.dumps(plan.to_dict(), indent=2), args.out)
    else:
        _emit(_render_plan_table(plan), args.out)
    return 1 if plan.failed else 0


def _run_pull(args) -> int:
    fixtures = _load_fixtures(args.fixtures)
    refs = _refs_from_args(args)
    summary = pull_to_layout(refs, args.out, fixtures=fixtures,
                             insecure=args.insecure,
                             log=lambda m: print(f"  {m}", file=sys.stderr))
    print(json.dumps(summary, indent=2))
    return 0


def _run_verify(args) -> int:
    res = verify_layout(args.layout)
    if args.format == "json":
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print(_render_verify_table(res))
    return 0 if res.passed else 1


def _run_push(args) -> int:
    summary = push_layout(args.layout, args.dest,
                          repository_override=args.repository,
                          dry_run=args.dry_run, insecure=args.insecure,
                          log=lambda m: print(f"  {m}", file=sys.stderr))
    if args.format == "json":
        print(json.dumps(summary, indent=2))
    else:
        print(_render_push_table(summary))
    return 0


def _run_mcp() -> int:
    from .mcp_server import run_mcp_server
    run_mcp_server()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return _run_plan(args)
        if args.command == "pull":
            return _run_pull(args)
        if args.command == "verify":
            return _run_verify(args)
        if args.command == "push":
            return _run_push(args)
        if args.command == "mcp":
            return _run_mcp()
    except (OSError, OreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
