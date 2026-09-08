from __future__ import annotations

import json
from pathlib import Path

from wikic.core import (
    build_catalog,
    build_graph,
    build_llms_text,
    build_okf_readiness,
    build_repair_plan,
    build_summary,
    load_config,
    run_doctor,
    split_frontmatter,
)
from wikic.core import build_okf_readiness as core_build_okf_readiness


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_catalog_discovers_markdown_pages_with_frontmatter_and_links(tmp_path: Path) -> None:
    write(
        tmp_path / "concepts" / "llm-wiki.md",
        """---
title: LLM Wiki
type: concept
status: active
---
# LLM Wiki

Links to [[tools/wikic]] and [[missing page]].
""",
    )
    write(
        tmp_path / "tools" / "wikic.md",
        """---
title: Wikic
type: tool
---
# Wikic
""",
    )

    catalog = build_catalog(tmp_path)

    assert catalog["page_count"] == 2
    page = catalog["pages"]["concepts/llm-wiki"]
    assert page["title"] == "LLM Wiki"
    assert page["type"] == "concept"
    assert page["status"] == "active"
    assert page["outlinks"] == ["tools/wikic", "missing-page"]
    assert len(page["sha256"]) == 64


def test_summary_reports_compact_health_snapshot(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps(
            {
                "naming_policy_exemptions": [
                    "README.md",
                    "SCHEMA.md",
                    "research/github-repos/repos/*--*.md",
                ]
            }
        ),
    )
    write(
        tmp_path / "index.md",
        "# Home\n[[tools/wikic]]\n[[research/github-repos/repos/NousResearch--Hermes-Agent]]\n",
    )
    write(
        tmp_path / "tools" / "wikic.md",
        "---\ntype: tool\nstatus: active\n---\n# Wikic\n",
    )
    write(tmp_path / "scratch" / "Bad Name.md", "# Bad Name\n")
    write(tmp_path / "README.md", "# Conventional README\n[[index]]\n")
    write(tmp_path / "SCHEMA.md", "# Conventional schema policy\n[[index]]\n")
    write(
        tmp_path / "research" / "github-repos" / "repos" / "NousResearch--Hermes-Agent.md",
        "---\ntype: reference\nstatus: active\n---\n# Repo card\n",
    )

    summary = build_summary(tmp_path)

    assert summary["catalog"]["page_count"] == 6
    assert summary["doctor"]["issue_counts"] == {"WK002": 1}
    assert summary["top_level_counts"]["tools"] == 1
    assert summary["frontmatter_by_top_level"]["tools"]["coverage_percent"] == 100.0
    assert summary["frontmatter_gaps"] == [
        {"path": "README.md", "top_level": "."},
        {"path": "SCHEMA.md", "top_level": "."},
        {"path": "index.md", "top_level": "."},
        {"path": "scratch/Bad Name.md", "top_level": "scratch"},
    ]
    assert summary["naming_policy_violation_count"] == 1
    assert summary["naming_policy_violations"] == [
        {"path": "scratch/Bad Name.md", "expected_slug": "scratch/bad-name"}
    ]
    assert "okf_readiness" not in summary


def test_okf_readiness_audits_curated_pages_and_source_provenance(tmp_path: Path) -> None:
    write(tmp_path / "projects" / "missing-type.md", "# Missing Type\n")
    write(tmp_path / "concepts" / "bad-type.md", "---\ntype: youtube-review\n---\n# Bad Type\n")
    write(
        tmp_path / "tools" / "unknown-field.md",
        "---\ntype: tool\ncustom: value\n---\n# Unknown Field\n",
    )
    write(
        tmp_path / "source-notes" / "article.md",
        "---\ntype: source-note\n---\n# Article\n",
    )
    write(
        tmp_path / "source-notes" / "with-source.md",
        "---\ntype: source-note\nsource_url: https://example.test\n---\n# Article\n",
    )
    write(tmp_path / "raw" / "ignored.md", "# Ignored raw\n")
    write(tmp_path / "scratch" / "ignored.md", "# Ignored scratch\n")

    readiness = build_okf_readiness(tmp_path)

    assert readiness["total_checked_pages"] == 5
    assert readiness["pages_missing_type"] == 1
    assert readiness["missing_type_pages"] == [
        {"path": "projects/missing-type.md", "suggested_type": "project"}
    ]
    assert readiness["invalid_type_pages"] == [
        {"path": "concepts/bad-type.md", "type": "youtube-review"}
    ]
    assert readiness["provenance_gap_pages"] == [
        {"path": "source-notes/article.md", "type": "source-note"}
    ]
    assert readiness["unknown_fields"] == [
        {"field": "custom", "count": 1, "paths": ["tools/unknown-field.md"]}
    ]


def test_okf_readiness_reports_malformed_and_complex_frontmatter(tmp_path: Path) -> None:
    write(tmp_path / "projects" / "malformed.md", "---\ntype project\n---\n# Bad\n")
    write(
        tmp_path / "projects" / "complex.md",
        "---\ntype: project\nmetadata:\n  owner: Example User\n---\n# Complex\n",
    )

    readiness = build_okf_readiness(tmp_path)

    assert readiness["malformed_frontmatter_count"] == 1
    assert readiness["malformed_frontmatter_pages"] == [{"path": "projects/malformed.md"}]
    assert readiness["export_blocking_shape_count"] == 1
    assert readiness["export_blocking_shapes"] == [
        {"path": "projects/complex.md", "field": "metadata"}
    ]


def test_okf_profile_is_opt_in_for_summary_and_doctor(tmp_path: Path) -> None:
    write(tmp_path / "projects" / "missing-type.md", "# Missing Type\n")

    default_summary = build_summary(tmp_path)
    okf_summary = build_summary(tmp_path, profile="okf-compatible")
    default_doctor = run_doctor(tmp_path, ignore_orphans=True).to_dict()
    okf_doctor = run_doctor(tmp_path, ignore_orphans=True, profile="okf-compatible").to_dict()

    assert "okf_readiness" not in default_summary
    assert okf_summary["okf_readiness"]["pages_missing_type"] == 1
    assert default_doctor["issues"] == []
    assert any(issue["code"] == "WK010" for issue in okf_doctor["issues"])


def test_okf_unknown_fields_are_inventory_only_in_doctor(tmp_path: Path) -> None:
    write(tmp_path / "tools" / "wikic.md", "---\ntype: tool\ncustom: x\n---\n# Tool\n")

    report = run_doctor(tmp_path, ignore_orphans=True, profile="okf-compatible")

    assert report.ok is True
    assert report.exit_code == 0
    assert report.issue_count == 1
    assert report.issues[0]["code"] == "WK014"
    assert report.issues[0]["severity"] == "info"


def test_summary_reports_advisor_pressure_signals(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[tools/wikic]]\n[[projects/index]]\n")
    write(tmp_path / "projects" / "index.md", "# Projects\n")
    write(
        tmp_path / "projects" / "action-dashboard.md",
        "# Action Dashboard\nTODO: follow up\n- [ ] Ship Wikic summary fields\n",
    )
    write(tmp_path / "projects" / "old-plan.md", "# Old Plan\nThis is stale and outdated.\n")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n" + "word " * 1201)
    write(tmp_path / "tools" / "agent.md", "# Agent\n")

    summary = build_summary(tmp_path)

    assert summary["action_marker_hotspots"][0] == {
        "path": "projects/action-dashboard.md",
        "marker_count": 2,
    }
    assert summary["stale_marker_hotspots"][0] == {
        "path": "projects/old-plan.md",
        "marker_count": 2,
    }
    assert summary["index_coverage"] == [
        {
            "top_level": "projects",
            "page_count": 2,
            "has_index": True,
            "index_path": "projects/index.md",
        },
        {"top_level": "tools", "page_count": 2, "has_index": False, "index_path": None},
    ]
    assert summary["large_page_pressure"] == {
        "threshold_word_count": 1000,
        "page_count": 1,
        "pages": [
            {
                "slug": "tools/wikic",
                "path": "tools/wikic.md",
                "word_count": 1202,
            }
        ],
    }


def test_summary_reports_timeline_coverage_for_eligible_pages(tmp_path: Path) -> None:
    write(
        tmp_path / "tools" / "indexer.md",
        """---
type: tool
---
# Wikic

<!-- timeline -->

- **2026-06-05** | Indexer — Added timeline sentinel.
""",
    )
    write(
        tmp_path / "people" / "alice.md",
        """---
type: person
status: active
---
# Alice

- Notes: 2026-06-01 and 2026-06-02.
""",
    )
    write(
        tmp_path / "projects" / "roadmap.md",
        "---\ntype: project\nstatus: active\n---\n# Roadmap\n\nLong-term plan.\n",
    )
    write(
        tmp_path / "concepts" / "draft.md",
        "# Draft\n\n2026-06-03 this is an ordinary date note.\n",
    )
    write(
        tmp_path / "pai" / "operations.md",
        "---\ntype: profile\n---\n# PAI Operations\nActive page under pai with no timeline yet.\n",
    )
    write(
        tmp_path / "tools" / "empty-timeline.md",
        "---\ntype: tool\n---\n# Empty Timeline\n\n<!-- timeline -->\n\nNo dated entries yet.\n",
    )

    summary = build_summary(tmp_path)["timeline_coverage"]

    assert summary["eligible_types"] == [
        "company",
        "identity",
        "person",
        "priorities",
        "profile",
        "project",
        "system-design",
        "tool",
    ]
    assert summary["eligible_path_prefixes"] == ["pai/"]
    assert summary["eligible_pages"] == 5
    assert summary["pages_with_timeline"] == 1
    assert summary["pages_with_dates_outside_timeline"] == 1
    assert summary["coverage_ratio"] == 0.2
    assert summary["top_missing_candidates"] == [
        {"path": "people/alice.md", "type": "person", "date_count": 2},
    ]


def test_summary_reports_timeline_ratio_rounded_and_stable(tmp_path: Path) -> None:
    write(
        tmp_path / "tools" / "wikic.md",
        """---
type: tool
---
# Wikic

<!-- timeline -->

- **2026-06-01** | Wikic — Added timeline marker.
""",
    )
    write(
        tmp_path / "projects" / "alpha.md",
        "---\ntype: project\n---\n# Alpha\n\n2026-06-01 update\n",
    )
    write(
        tmp_path / "personas" / "mentor.md",
        "---\ntype: person\n---\n# Mentor\n\n"
        "2026-06-02 update\n2026-06-03 update\n2026-06-04 update\n",
    )

    summary = build_summary(tmp_path)["timeline_coverage"]

    assert summary["eligible_pages"] == 3
    assert summary["pages_with_timeline"] == 1
    assert summary["coverage_ratio"] == 0.333
    assert summary["pages_with_dates_outside_timeline"] == 2
    assert summary["top_missing_candidates"] == [
        {"path": "personas/mentor.md", "type": "person", "date_count": 3},
        {"path": "projects/alpha.md", "type": "project", "date_count": 1},
    ]


def test_summary_reports_generated_report_policy_violations(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps(
            {
                "generated_report_policy": {
                    "rules": [
                        {
                            "path_glob": "reports/advisor/*.md",
                            "rollup": "reports/index.md",
                            "require_frontmatter": True,
                        }
                    ]
                }
            }
        ),
    )
    write(
        tmp_path / "reports" / "index.md",
        "---\ntype: index\nstatus: active\n---\n# Reports\n[[reports/advisor/good]]\n",
    )
    write(
        tmp_path / "reports" / "advisor" / "good.md",
        "---\ntype: report\nstatus: active\n---\n# Good Report\n",
    )
    write(tmp_path / "reports" / "advisor" / "missing-frontmatter.md", "# Missing FM\n")
    write(
        tmp_path / "reports" / "advisor" / "missing-rollup.md",
        "---\ntype: report\nstatus: active\n---\n# Missing Rollup\n",
    )

    summary = build_summary(tmp_path)

    assert summary["generated_report_policy"] == {
        "rule_count": 1,
        "violation_count": 2,
        "rules": [
            {
                "path_glob": "reports/advisor/*.md",
                "rollup": "reports/index.md",
                "require_frontmatter": True,
                "matched_count": 3,
                "violation_count": 2,
            }
        ],
        "violations": [
            {
                "path": "reports/advisor/missing-frontmatter.md",
                "path_glob": "reports/advisor/*.md",
                "rollup": "reports/index.md",
                "missing_frontmatter": True,
                "missing_rollup_link": True,
            },
            {
                "path": "reports/advisor/missing-rollup.md",
                "path_glob": "reports/advisor/*.md",
                "rollup": "reports/index.md",
                "missing_frontmatter": False,
                "missing_rollup_link": True,
            },
        ],
    }


def test_load_config_returns_default_excludes(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert "raw/**" in config["exclude"]
    assert "archive/**" in config["exclude"]


def test_load_config_merges_user_excludes(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        '{"exclude": ["scratch/**", "raw/**"]}',
    )
    config = load_config(tmp_path)

    assert config["exclude"].count("raw/**") == 1
    assert "scratch/**" in config["exclude"]


def test_summary_uses_configured_naming_policy_exemptions(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps(
            {
                "naming_policy_exemptions": [
                    "README.md",
                    "SCHEMA.md",
                    "research/github-repos/repos/*--*.md",
                    "Brand Folder/*.md",
                ]
            }
        ),
    )
    write(tmp_path / "README.md", "# Conventional README\n[[SCHEMA]]\n")
    write(tmp_path / "SCHEMA.md", "# Conventional schema policy\n[[README]]\n")
    write(tmp_path / "Brand Folder" / "Keep Case.md", "# Keep Case\n[[README]]\n")
    write(
        tmp_path / "research" / "github-repos" / "repos" / "NousResearch--Hermes-Agent.md",
        "# Repo card\n[[README]]\n",
    )
    write(tmp_path / "scratch" / "Bad Name.md", "# Bad Name\n[[README]]\n")

    summary = build_summary(tmp_path)

    assert summary["naming_policy_violation_count"] == 1
    assert summary["naming_policy_violations"] == [
        {"path": "scratch/Bad Name.md", "expected_slug": "scratch/bad-name"}
    ]


def test_invalid_naming_policy_exemption_config_errors(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        '{"naming_policy_exemptions": ["README.md", 42]}',
    )

    try:
        load_config(tmp_path)
    except ValueError as exc:
        assert "naming_policy_exemptions" in str(exc)
    else:
        raise AssertionError("expected invalid naming_policy_exemptions to fail")


def test_catalog_includes_basename_and_aliases(tmp_path: Path) -> None:
    write(
        tmp_path / "tools" / "wikic.md",
        """---
title: Wikic
type: tool
aliases: [bf, Forge]
---
# Wikic
""",
    )

    page = build_catalog(tmp_path)["pages"]["tools/wikic"]

    assert page["basename"] == "wikic"
    assert page["aliases"] == ["bf", "Forge"]


def test_catalog_uses_default_excludes(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n")
    write(tmp_path / "raw" / "source.md", "# Raw Source\n")
    write(tmp_path / "raw" / "sources" / "deep.md", "# Deep Raw Source\n")
    write(tmp_path / "archive" / "old.md", "# Old\n")
    write(tmp_path / "archives" / "year" / "old.md", "# Old Nested\n")

    catalog = build_catalog(tmp_path)

    assert set(catalog["pages"]) == {"index"}
    assert catalog["summary"]["excluded_files"] == 4


def test_catalog_uses_configured_excludes(tmp_path: Path) -> None:
    write(tmp_path / ".wikic" / "config.json", '{"exclude": ["scratch/**"]}')
    write(tmp_path / "index.md", "# Home\n")
    write(tmp_path / "scratch" / "draft.md", "# Draft\n")

    catalog = build_catalog(tmp_path)

    assert set(catalog["pages"]) == {"index"}
    assert catalog["summary"]["excluded_files"] == 1


def test_graph_resolves_unique_basename_links(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[wikic]]")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n")

    graph = build_graph(build_catalog(tmp_path))

    assert graph["edges"] == [
        {
            "source": "index",
            "raw_target": "wikic",
            "target": "tools/wikic",
            "resolved": True,
            "resolution": "basename",
            "candidates": [],
        }
    ]


def test_graph_exact_slug_wins_over_basename(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[wikic]]")
    write(tmp_path / "wikic.md", "# Root Wikic\n")
    write(tmp_path / "tools" / "wikic.md", "# Tool Wikic\n")

    edge = build_graph(build_catalog(tmp_path))["edges"][0]

    assert edge["target"] == "wikic"
    assert edge["resolution"] == "exact"
    assert edge["resolved"] is True


def test_graph_resolves_unique_alias_links(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[bf]]")
    write(
        tmp_path / "tools" / "wikic.md",
        """---
aliases: [bf]
---
# Wikic
""",
    )

    edge = build_graph(build_catalog(tmp_path))["edges"][0]

    assert edge["target"] == "tools/wikic"
    assert edge["resolved"] is True
    assert edge["resolution"] == "alias"


def test_graph_reports_ambiguous_alias_links(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[bf]]")
    write(
        tmp_path / "tools" / "wikic.md",
        """---
aliases: [bf]
---
# Wikic
""",
    )
    write(
        tmp_path / "concepts" / "wikic.md",
        """---
aliases: [bf]
---
# Wikic Concept
""",
    )

    edge = build_graph(build_catalog(tmp_path))["edges"][0]

    assert edge["resolved"] is False
    assert edge["resolution"] == "ambiguous"
    assert edge["candidates"] == ["concepts/wikic", "tools/wikic"]


def test_graph_marks_resolved_and_missing_edges(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n[[b]] [[missing target]]")
    write(tmp_path / "b.md", "# B\n[[a]]")

    graph = build_graph(build_catalog(tmp_path))

    assert {node["id"] for node in graph["nodes"]} == {"a", "b"}
    assert graph["edges"] == [
        {
            "source": "a",
            "raw_target": "b",
            "target": "b",
            "resolved": True,
            "resolution": "exact",
            "candidates": [],
        },
        {
            "source": "a",
            "raw_target": "missing-target",
            "target": None,
            "resolved": False,
            "resolution": "missing",
            "candidates": [],
        },
        {
            "source": "b",
            "raw_target": "a",
            "target": "a",
            "resolved": True,
            "resolution": "exact",
            "candidates": [],
        },
    ]


def test_doctor_reports_ambiguous_basename_links(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[wikic]]")
    write(tmp_path / "tools" / "wikic.md", "# Tool Wikic\n")
    write(tmp_path / "concepts" / "wikic.md", "# Concept Wikic\n")

    report = run_doctor(tmp_path)

    assert any(
        issue["code"] == "WK004"
        and issue["target"] == "wikic"
        and issue["candidates"] == ["concepts/wikic", "tools/wikic"]
        for issue in report.issues
    )
    assert not any(issue["code"] == "WK001" for issue in report.issues)
    assert report.summary["ambiguous_edges"] == 1


def test_doctor_reports_missing_links_orphans_and_nonzero_exit(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n[[missing]]")
    write(tmp_path / "orphan.md", "# Orphan\n")

    report = run_doctor(tmp_path)

    assert report.ok is False
    assert report.exit_code == 1
    assert any(issue["code"] == "WK001" for issue in report.issues)
    assert any(issue["code"] == "WK002" and issue["page"] == "orphan" for issue in report.issues)


def test_doctor_can_ignore_orphans(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n")

    report = run_doctor(tmp_path, ignore_orphans=True)

    assert report.ok is True
    assert report.exit_code == 0
    assert report.issues == []


def test_doctor_json_includes_actionable_work_queue(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[missing page]]")

    payload = run_doctor(tmp_path, ignore_orphans=True).to_dict()

    assert payload["work_queue"] == [
        {
            "id": "WK001:index:missing-page",
            "code": "WK001",
            "severity": "error",
            "action": "create_or_retarget_link",
            "page": "index",
            "target": "missing-page",
            "message": "Missing wikilink target: missing-page",
        }
    ]


def test_work_queue_includes_ambiguous_candidates(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[wikic]]")
    write(tmp_path / "tools" / "wikic.md", "# Tool Wikic\n")
    write(tmp_path / "concepts" / "wikic.md", "# Concept Wikic\n")

    payload = run_doctor(tmp_path, ignore_orphans=True).to_dict()

    assert payload["work_queue"][0]["code"] == "WK004"
    assert payload["work_queue"][0]["action"] == "disambiguate_link"
    assert payload["work_queue"][0]["candidates"] == [
        "concepts/wikic",
        "tools/wikic",
    ]


def test_missing_link_work_items_include_suggested_targets(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n[[wiki-c]]")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n")

    payload = run_doctor(tmp_path, ignore_orphans=True).to_dict()

    assert payload["work_queue"] == [
        {
            "id": "WK001:index:wiki-c",
            "code": "WK001",
            "severity": "error",
            "action": "create_or_retarget_link",
            "page": "index",
            "target": "wiki-c",
            "suggested_targets": ["tools/wikic"],
            "message": "Missing wikilink target: wiki-c",
        }
    ]


def test_doctor_groups_duplicate_missing_targets(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n[[wiki-c]]")
    write(tmp_path / "b.md", "# B\n[[wiki-c]]")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n")

    payload = run_doctor(tmp_path, ignore_orphans=True).to_dict()

    assert payload["work_queue_groups"] == [
        {
            "id": "WK001:wiki-c",
            "code": "WK001",
            "severity": "error",
            "action": "create_or_retarget_link",
            "target": "wiki-c",
            "count": 2,
            "pages": ["a", "b"],
            "suggested_targets": ["tools/wikic"],
            "message": "Missing wikilink target appears on 2 pages: wiki-c",
        }
    ]


def test_repair_plan_groups_review_required_retarget_operations(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n[[wiki-c]] and [[wiki-c|Wikic]]")
    write(tmp_path / "nested" / "b.md", "# B\n[[wiki-c#Install]]")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n")

    payload = build_repair_plan(tmp_path, ignore_orphans=True)

    assert payload["ok"] is True
    assert payload["summary"] == {
        "operation_count": 1,
        "retarget_operations": 1,
        "inspect_operations": 0,
        "review_required": 1,
    }
    operation = payload["operations"][0]
    assert operation["id"] == "repair:WK001:wiki-c"
    assert operation["issue_code"] == "WK001"
    assert operation["action"] == "retarget_wikilinks"
    assert operation["status"] == "review_required"
    assert operation["target"] == "wiki-c"
    assert operation["replacement_target"] == "tools/wikic"
    assert operation["affected_pages"] == [
        {"slug": "a", "path": "a.md"},
        {"slug": "nested/b", "path": "nested/b.md"},
    ]
    assert operation["occurrences"] == [
        {
            "path": "a.md",
            "slug": "a",
            "line": 2,
            "raw": "[[wiki-c]]",
            "target": "wiki-c",
            "suggested_replacement": "[[tools/wikic]]",
            "status": "review_required",
        },
        {
            "path": "a.md",
            "slug": "a",
            "line": 2,
            "raw": "[[wiki-c|Wikic]]",
            "target": "wiki-c",
            "suggested_replacement": "[[tools/wikic|Wikic]]",
            "status": "review_required",
        },
        {
            "path": "nested/b.md",
            "slug": "nested/b",
            "line": 2,
            "raw": "[[wiki-c#Install]]",
            "target": "wiki-c",
            "suggested_replacement": "[[tools/wikic#Install]]",
            "status": "review_required",
        },
    ]


def test_repair_plan_requires_inspection_without_single_suggestion(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n[[unknown-target]]")
    write(tmp_path / "b.md", "# B\n[[unknown-target]]")

    payload = build_repair_plan(tmp_path, ignore_orphans=True)

    assert payload["summary"] == {
        "operation_count": 1,
        "retarget_operations": 0,
        "inspect_operations": 1,
        "review_required": 1,
    }
    operation = payload["operations"][0]
    assert operation["id"] == "repair:WK001:unknown-target"
    assert operation["issue_code"] == "WK001"
    assert operation["action"] == "inspect_missing_target"
    assert operation["status"] == "review_required"
    assert operation["target"] == "unknown-target"
    assert operation["affected_pages"] == [
        {"slug": "a", "path": "a.md"},
        {"slug": "b", "path": "b.md"},
    ]
    assert operation["occurrences"] == [
        {
            "path": "a.md",
            "slug": "a",
            "line": 2,
            "raw": "[[unknown-target]]",
            "target": "unknown-target",
            "status": "review_required",
        },
        {
            "path": "b.md",
            "slug": "b",
            "line": 2,
            "raw": "[[unknown-target]]",
            "target": "unknown-target",
            "status": "review_required",
        },
    ]
    assert operation["reason"] == "No single deterministic replacement target was available."
    assert operation["notes"] == [
        "This plan is advisory only; Wikic did not edit files.",
        "Create the missing page, add an alias, or retarget links manually.",
    ]


def test_repair_plan_can_include_patch_preview(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\nLink [[wiki-c|Wikic]].\n")
    write(tmp_path / "b.md", "# B\nSee [[wiki-c]].\n")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n")

    payload = build_repair_plan(tmp_path, ignore_orphans=True, patch_preview=True)

    operation = payload["operations"][0]
    assert "patch_preview" in operation
    assert "--- a/a.md" in operation["patch_preview"]
    assert "+++ b/a.md" in operation["patch_preview"]
    assert "-Link [[wiki-c|Wikic]]." in operation["patch_preview"]
    assert "+Link [[tools/wikic|Wikic]]." in operation["patch_preview"]
    assert "-See [[wiki-c]]." in operation["patch_preview"]
    assert "+See [[tools/wikic]]." in operation["patch_preview"]


def test_llms_text_builds_agent_readable_index(tmp_path: Path) -> None:
    write(
        tmp_path / "tools" / "wikic.md",
        """---
title: Wikic
type: tool
summary: Deterministic LLM-wiki compiler.
---
# Wikic
""",
    )

    text = build_llms_text(build_catalog(tmp_path), title="Test Vault")

    assert text.startswith("# Test Vault")
    assert "- [Wikic](tools/wikic.md) — tool — Deterministic LLM-wiki compiler." in text


def test_catalog_json_is_serializable(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Home\n")

    catalog = build_catalog(tmp_path)

    json.dumps(catalog)


def test_split_frontmatter_parses_block_sequences():
    text = (
        "---\n"
        "title: Comparison\n"
        "sources:\n"
        "  - https://example.com/a\n"
        "  - https://example.com/b\n"
        "tags: [alpha, beta]\n"
        "---\n"
        "\nbody\n"
    )

    frontmatter, body = split_frontmatter(text)

    assert frontmatter["sources"] == ["https://example.com/a", "https://example.com/b"]
    assert frontmatter["tags"] == ["alpha", "beta"]
    assert body.strip() == "body"


def test_split_frontmatter_ignores_sequence_items_as_keys():
    """A block item containing a colon must not become its own key."""
    text = "---\nsources:\n  - https://example.com/a\n---\n\nbody\n"

    frontmatter, _ = split_frontmatter(text)

    assert list(frontmatter) == ["sources"]
    assert "- https" not in frontmatter


def test_split_frontmatter_leaves_nested_maps_empty():
    text = "---\ntitle: Page\nconfig:\n  nested: 1\n---\n\nbody\n"

    frontmatter, _ = split_frontmatter(text)

    assert frontmatter["config"] == ""
    assert "nested" not in frontmatter


def test_okf_suggests_from_the_most_specific_folder(tmp_path: Path) -> None:
    """A nested folder wins over its parents, and an unknown path suggests nothing."""
    write(tmp_path / "wiki" / "tools" / "ripgrep.md", "---\ntitle: rg\n---\n# rg\n")
    write(tmp_path / "wiki" / "intel" / "acme" / "widget.md", "---\ntitle: w\n---\n# w\n")

    readiness = core_build_okf_readiness(tmp_path)
    suggestions = {i["path"]: i.get("suggested_type") for i in readiness["missing_type_pages"]}

    assert suggestions["wiki/tools/ripgrep.md"] == "tool"
    assert suggestions.get("wiki/intel/acme/widget.md") is None
