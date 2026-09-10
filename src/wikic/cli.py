from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .core import (
    OKF_PROFILE,
    VAULT_POLICY_PROFILE,
    build_catalog,
    build_graph,
    build_llms_text,
    build_repair_plan,
    build_summary,
    run_doctor,
    write_json,
)
from .workflow import (
    accept_candidate,
    ingest_source,
    lint_candidates,
    list_candidates,
    show_candidate,
)


def add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        "--vault",
        default=".",
        help="Markdown vault root. Defaults to current directory.",
    )


def add_vault_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vault", "--root", default=".")


def add_profile_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        choices=[OKF_PROFILE, VAULT_POLICY_PROFILE],
        help="Force-add an audit profile beyond checks enabled in .wikic/config.json.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wikic",
        description="Deterministic compiler/doctor for agent-readable Markdown LLM wikis.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Run deterministic vault health checks.")
    add_common_flags(doctor)
    doctor.add_argument("--json", action="store_true", help="Print machine-readable report.")
    doctor.add_argument(
        "--ignore-orphans",
        action="store_true",
        help="Do not report isolated pages.",
    )
    doctor.add_argument(
        "--require-vault-files",
        action="store_true",
        help="Require root files listed in .wikic/config.json.",
    )
    add_profile_flag(doctor)

    health = sub.add_parser("health", help="Alias for doctor.")
    add_common_flags(health)
    health.add_argument("--json", action="store_true", help="Print machine-readable report.")
    health.add_argument(
        "--ignore-orphans",
        action="store_true",
        help="Do not report isolated pages.",
    )
    health.add_argument(
        "--require-vault-files",
        action="store_true",
        help="Require root files listed in .wikic/config.json.",
    )
    add_profile_flag(health)

    repair_plan = sub.add_parser(
        "repair-plan",
        help="Generate a non-mutating, review-required repair plan from doctor groups.",
    )
    add_common_flags(repair_plan)
    repair_plan.add_argument("--json", action="store_true", help="Print machine-readable plan.")
    repair_plan.add_argument(
        "--ignore-orphans",
        action="store_true",
        help="Do not include orphan warnings when building the underlying doctor report.",
    )
    repair_plan.add_argument(
        "--require-vault-files",
        action="store_true",
        help="Require root files listed in .wikic/config.json.",
    )
    repair_plan.add_argument(
        "--patch-preview",
        action="store_true",
        help="Include unified diff previews for deterministic retarget operations.",
    )

    summary = sub.add_parser("summary", help="Print compact deterministic vault-health summary.")
    add_common_flags(summary)
    summary.add_argument("--json", action="store_true", help="Print machine-readable summary.")
    add_profile_flag(summary)

    catalog = sub.add_parser("catalog", help="Compile .wikic/catalog.json.")
    add_common_flags(catalog)
    catalog.add_argument(
        "--stdout",
        action="store_true",
        help="Print JSON instead of writing artifact.",
    )

    graph = sub.add_parser("graph", help="Compile .wikic/graph.json.")
    add_common_flags(graph)
    graph.add_argument(
        "--stdout",
        action="store_true",
        help="Print JSON instead of writing artifact.",
    )

    llms = sub.add_parser("llms", help="Compile llms.txt for agent navigation.")
    add_common_flags(llms)
    llms.add_argument("--title", help="Override llms.txt title.")
    llms.add_argument("--full", action="store_true", help="Include link detail.")
    llms.add_argument("--stdout", action="store_true", help="Print instead of writing llms.txt.")

    ingest = sub.add_parser("ingest", help="Capture a source file into .wikic/sources/.")
    ingest.add_argument("source")
    add_vault_flag(ingest)
    ingest.add_argument("--json", action="store_true")

    review = sub.add_parser("review", help="List, inspect, and accept candidate pages.")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_list = review_sub.add_parser("list", help="List candidate JSON files.")
    add_vault_flag(review_list)
    review_list.add_argument("--json", action="store_true")
    review_show = review_sub.add_parser("show", help="Show one candidate JSON file.")
    review_show.add_argument("candidate_id")
    add_vault_flag(review_show)
    review_show.add_argument("--json", action="store_true")
    review_accept = review_sub.add_parser("accept", help="Dry-run or apply one candidate.")
    review_accept.add_argument("candidate_id")
    add_vault_flag(review_accept)
    review_accept.add_argument("--dry-run", action="store_true")
    review_accept.add_argument("--apply", action="store_true")
    review_accept.add_argument("--json", action="store_true")

    lint = sub.add_parser("lint", help="Run deterministic lint checks.")
    add_vault_flag(lint)
    lint.add_argument("--candidates", action="store_true", help="Validate candidate JSON files.")
    lint.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command in {"doctor", "health"}:
        root = Path(args.root).resolve()
        report = run_doctor(
            root,
            ignore_orphans=args.ignore_orphans,
            require_vault_files=args.require_vault_files,
            profile=args.profile,
        )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            _print_doctor(report.to_dict())
        return report.exit_code

    if args.command == "repair-plan":
        root = Path(args.root).resolve()
        payload = build_repair_plan(
            root,
            ignore_orphans=args.ignore_orphans,
            require_vault_files=args.require_vault_files,
            patch_preview=args.patch_preview,
        )
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_repair_plan(payload)
        return 0

    if args.command == "summary":
        root = Path(args.root).resolve()
        payload = build_summary(root, profile=args.profile)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_summary(payload)
        return 0

    if args.command == "catalog":
        root = Path(args.root).resolve()
        payload = build_catalog(root)
        if args.stdout:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            out = root / ".wikic" / "catalog.json"
            write_json(out, payload)
            print(f"wrote {out}")
        return 0

    if args.command == "graph":
        root = Path(args.root).resolve()
        payload = build_graph(build_catalog(root))
        if args.stdout:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            out = root / ".wikic" / "graph.json"
            write_json(out, payload)
            print(f"wrote {out}")
        return 0

    if args.command == "llms":
        root = Path(args.root).resolve()
        text = build_llms_text(build_catalog(root), title=args.title, full=args.full)
        if args.stdout:
            print(text, end="")
        else:
            out = root / "llms.txt"
            out.write_text(text, encoding="utf-8")
            print(f"wrote {out}")
        return 0

    if args.command == "ingest":
        payload, exit_code = ingest_source(
            Path(args.vault).expanduser().resolve(),
            Path(args.source),
        )
        return _emit(payload, as_json=args.json, exit_code=exit_code)

    if args.command == "review":
        vault = Path(args.vault).expanduser().resolve()
        if args.review_command == "list":
            return _emit(list_candidates(vault), as_json=args.json)
        if args.review_command == "show":
            payload, exit_code = show_candidate(vault, args.candidate_id)
            return _emit(payload, as_json=args.json, exit_code=exit_code)
        if args.review_command == "accept":
            payload, exit_code = accept_candidate(
                vault,
                args.candidate_id,
                dry_run=args.dry_run,
                apply=args.apply,
            )
            return _emit(payload, as_json=args.json, exit_code=exit_code)

    if args.command == "lint":
        vault = Path(args.vault).expanduser().resolve()
        if args.candidates:
            payload, exit_code = lint_candidates(vault)
        else:
            report = run_doctor(vault)
            payload, exit_code = report.to_dict(), report.exit_code
        return _emit(payload, as_json=args.json, exit_code=exit_code)

    return 2


def _emit(payload: dict[str, Any], *, as_json: bool, exit_code: int = 0) -> int:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        status = "OK" if payload.get("ok", True) else "ERROR"
        print(f"{status}: {payload.get('action', 'command')}")
        for issue in payload.get("issues", []):
            print(f"- {issue.get('code')}: {issue.get('path') or issue.get('message')}")
        if "error" in payload:
            print(payload["error"].get("message", payload["error"].get("code")))
    return exit_code


def _print_repair_plan(payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    print(
        "Wikic repair plan: {operation_count} operations "
        "({retarget_operations} retarget, {inspect_operations} inspect), "
        "all review_required".format(**summary)
    )
    for operation in payload["operations"]:
        replacement = ""
        if operation.get("replacement_target"):
            replacement = f" -> {operation['replacement_target']}"
        print(
            f"[{operation['status']}] {operation['action']} "
            f"{operation['target']}{replacement} on {len(operation['affected_pages'])} pages"
        )
        if operation.get("patch_preview"):
            end = "" if operation["patch_preview"].endswith("\n") else "\n"
            print(operation["patch_preview"], end=end)


def _print_summary(payload: dict[str, Any]) -> None:
    doctor = payload["doctor"]
    issue_counts = doctor.get("issue_counts", {})
    issue_text = ", ".join(f"{code}={count}" for code, count in issue_counts.items()) or "none"
    print(f"Wikic summary: {payload['catalog']['page_count']} pages, issues: {issue_text}")
    print(
        (
            "Graph: edges={edges} resolved={resolved_edges} "
            "missing={missing_edges} ambiguous={ambiguous_edges}"
        ).format(**payload["graph"])
    )
    top_counts = sorted(payload["top_level_counts"].items(), key=lambda item: (-item[1], item[0]))[
        :10
    ]
    print("Top-level folders: " + ", ".join(f"{name}={count}" for name, count in top_counts))
    naming_count = payload.get(
        "naming_policy_violation_count", len(payload["naming_policy_violations"])
    )
    if naming_count:
        print(f"Naming policy warnings: {naming_count}")


def _print_doctor(report: dict[str, Any]) -> None:
    status = "ok" if report["ok"] else "issues found"
    print(f"Wikic doctor: {status}")
    print(
        "pages={pages} edges={edges} resolved_edges={resolved_edges} "
        "missing_edges={missing_edges} ambiguous_edges={ambiguous_edges} "
        "orphans={orphans}".format(**report["summary"])
    )
    for issue in report["issues"]:
        target = f" -> {issue['target']}" if "target" in issue else ""
        subject = issue.get("page") or issue.get("path") or "vault"
        print(f"[{issue['severity']}] {issue['code']} {subject}{target}: {issue['message']}")


if __name__ == "__main__":
    raise SystemExit(main())
