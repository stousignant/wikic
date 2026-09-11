from __future__ import annotations

import json
from pathlib import Path

import pytest

from wikic.core import (
    build_catalog,
    build_graph,
    build_llms_text,
    build_okf_readiness,
    build_repair_plan,
    build_summary,
    build_vault_policy_readiness,
    load_config,
    run_doctor,
    split_frontmatter,
)


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


def test_catalog_and_graph_resolve_relative_markdown_links(tmp_path: Path) -> None:
    write(
        tmp_path / "guides" / "index.md",
        "# Guides\n\n- [Child](child%20page.md)\n- [Home](../home.md#Start)\n"
        '- [Upper](upper.MD)\n- [Titled](title.md "Optional title")\n- [Dir](subdir/)\n'
        "- [External](https://example.test/page.md)\n![Image](diagram.md)\n",
    )
    write(tmp_path / "guides" / "child page.md", "---\ntype: guide\n---\n# Child\n")
    write(tmp_path / "home.md", "---\ntype: page\n---\n# Home\n")
    write(tmp_path / "guides" / "upper.MD", "---\ntype: page\n---\n# Upper\n")
    write(tmp_path / "guides" / "title.md", "---\ntype: page\n---\n# Title\n")
    write(tmp_path / "guides" / "subdir" / "index.md", "# Subdirectory\n")

    catalog = build_catalog(tmp_path)
    graph = build_graph(catalog)

    assert catalog["pages"]["guides/index"]["outlinks"] == [
        "guides/child-page",
        "home",
        "guides/upper",
        "guides/title",
        "guides/subdir/index",
    ]
    edges = [edge for edge in graph["edges"] if edge["source"] == "guides/index"]
    assert [(edge["target"], edge["resolved"]) for edge in edges] == [
        ("guides/child-page", True),
        ("home", True),
        ("guides/upper", True),
        ("guides/title", True),
        ("guides/subdir/index", True),
    ]


def test_catalog_reports_relative_links_that_escape_the_vault_as_missing(tmp_path: Path) -> None:
    write(tmp_path / "guides" / "index.md", "# Guides\n\n- [Escape](../../outside.md)\n")
    write(tmp_path / "outside.md", "---\ntype: page\n---\n# Outside\n")
    write(
        tmp_path / "invalid-outside-vault-link" / "outside.md",
        "---\ntype: page\n---\n# Collision probe\n",
    )

    catalog = build_catalog(tmp_path)
    graph = build_graph(catalog)

    page = catalog["pages"]["guides/index"]
    assert page["outlinks"] == []
    assert page["invalid_outlinks"] == [{"target": "../../outside.md", "reason": "outside_vault"}]
    edge = next(edge for edge in graph["edges"] if edge["source"] == "guides/index")
    assert edge["resolved"] is False
    assert edge["target"] is None
    assert edge["raw_target"] == "../../outside.md"
    assert edge["resolution"] == "outside_vault"
    report = run_doctor(tmp_path, ignore_orphans=True)
    assert report.ok is False
    assert report.exit_code == 1
    assert report.summary["invalid_edges"] == 1
    assert [(issue["code"], issue.get("reason")) for issue in report.issues] == [
        ("WK005", "outside_vault")
    ]


def test_markdown_links_use_raw_source_paths_and_markdown_escapes(tmp_path: Path) -> None:
    write(tmp_path / "punct" / "!!!" / "guide.md", "# Guide\n[Home](../home.md)\n")
    write(tmp_path / "punct" / "home.md", "# Nested home\n")
    write(tmp_path / "home.md", "# Root home\n")
    write(tmp_path / "escaped.md", "# Escaped\n[Parentheses](foo\\(bar\\).md)\n")
    write(tmp_path / "foo(bar).md", "# Parentheses\n")
    write(tmp_path / "collision.md", "# Collision\n[Victim](dir\\victim.md)\n")
    write(tmp_path / "dir" / "victim.md", "# Victim\n")

    catalog = build_catalog(tmp_path)
    graph = build_graph(catalog)

    assert catalog["pages"]["punct/guide"]["outlinks"] == ["punct/home"]
    assert catalog["pages"]["escaped"]["outlinks"] == ["foobar"]
    assert catalog["pages"]["collision"]["outlinks"] == []
    assert catalog["pages"]["collision"]["invalid_outlinks"] == [
        {"target": "dir\\victim.md", "reason": "invalid_path_separator"}
    ]
    edge = next(edge for edge in graph["edges"] if edge["source"] == "collision")
    assert edge["resolved"] is False
    assert edge["resolution"] == "invalid_path_separator"


def test_markdown_escapes_are_applied_before_url_classification(tmp_path: Path) -> None:
    write(tmp_path / "foo.md", "# Foo\n")
    write(
        tmp_path / "links.md",
        "# Links\n[Fragment](foo.md\\#Section)\n[Query](foo.md\\?view=1)\n"
        "[External](https\\://example.test/page.md)\n",
    )

    catalog = build_catalog(tmp_path)

    assert catalog["pages"]["links"]["outlinks"] == ["foo"]
    assert catalog["pages"]["links"]["invalid_outlinks"] == []


def test_markdown_links_ignore_code_and_comments(tmp_path: Path) -> None:
    body = (
        "# Examples\n\n```md\n[Code](code.md) [[WikiCode]]\n```\n\n"
        "`[Inline](inline.md) [[WikiInline]]`\n\n"
        "<!-- [Comment](comment.md) [[WikiComment]] -->\n"
    )
    write(tmp_path / "index.md", body)
    write(tmp_path / "long-fence.md", "~~~md\n[Fake](fake.md)\n\n~~~~\n")
    write(tmp_path / "unclosed-fence.md", "```md\n[Fake](fake.md)\n")
    write(tmp_path / "indented.md", "    [Fake](fake.md) [[WikiFake]]\n")
    write(tmp_path / "unclosed-comment.md", "<!-- [Fake](fake.md) [[WikiFake]]\n")
    write(tmp_path / "real.md", "# Real\n")
    write(
        tmp_path / "comment-in-fence.md",
        "```md\n<!--\n```\n[Real](real.md)\n",
    )
    write(tmp_path / "comment-inline.md", "`<!--`\n[Real](real.md)\n")
    write(tmp_path / "comment-indented.md", "    <!--\n[Real](real.md)\n")
    write(
        tmp_path / "fence-in-comment.md",
        "<!--\n```md\n[Hidden](hidden.md)\n-->\n[Real](real.md)\n",
    )
    write(
        tmp_path / "comment-in-closing-fence.md",
        "```md\n<!--\n```\n-->\n[Real](real.md)\n",
    )

    catalog = build_catalog(tmp_path)
    readiness = build_okf_readiness(tmp_path)

    assert catalog["pages"]["index"]["outlinks"] == []
    for slug in ("long-fence", "unclosed-fence", "indented", "unclosed-comment"):
        assert catalog["pages"][slug]["outlinks"] == []
    for slug in (
        "comment-in-fence",
        "comment-inline",
        "comment-indented",
        "fence-in-comment",
        "comment-in-closing-fence",
    ):
        assert catalog["pages"][slug]["outlinks"] == ["real"]
    assert readiness["invalid_index_files"] == [
        {"path": "index.md", "reason": "missing_markdown_link_entry"}
    ]


def test_summary_reports_compact_health_snapshot(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps(
            {
                "naming_policy_exemptions": [
                    "README.md",
                    "SCHEMA.md",
                    "catalog/repositories/*--*.md",
                ]
            }
        ),
    )
    write(
        tmp_path / "index.md",
        "# Home\n[[tools/wikic]]\n[[catalog/repositories/ExampleOrg--SampleTool]]\n",
    )
    write(
        tmp_path / "tools" / "wikic.md",
        "---\ntype: tool\nstatus: active\n---\n# Wikic\n",
    )
    write(tmp_path / "scratch" / "Bad Name.md", "# Bad Name\n")
    write(tmp_path / "README.md", "# Conventional README\n[[index]]\n")
    write(tmp_path / "SCHEMA.md", "# Conventional schema policy\n[[index]]\n")
    write(
        tmp_path / "catalog" / "repositories" / "ExampleOrg--SampleTool.md",
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


def test_okf_v02_accepts_unknown_types_extensions_and_nested_metadata(tmp_path: Path) -> None:
    write(
        tmp_path / "index.md",
        "---\nokf_version: '0.2'\n---\n# Concepts\n\n* [Widget](widget.md) - Example.\n",
    )
    write(
        tmp_path / "widget.md",
        "---\ntype: Domain-Specific Widget\ncustom:\n  nested: true\nsources:\n"
        "  - resource: https://example.test/source\n---\n# Widget\n",
    )
    write(
        tmp_path / "log.md",
        "# Update Log\n\n## 2026-06-02\n* Added widget.\n\n## 2026-06-01\n* Started.\n",
    )

    readiness = build_okf_readiness(tmp_path)

    assert readiness["conformant"] is True
    assert readiness["concept_document_count"] == 1
    assert readiness["reserved_file_count"] == 2
    assert readiness["invalid_type_count"] == 0


def test_okf_v02_reports_all_normative_conformance_failures(tmp_path: Path) -> None:
    write(tmp_path / "missing-frontmatter.md", "# Missing\n")
    write(tmp_path / "malformed.md", "---\ntype: [unterminated\n---\n# Bad\n")
    write(tmp_path / "missing-type.md", "---\ntitle: Missing Type\n---\n# Missing Type\n")
    write(tmp_path / "index.md", "---\ntitle: Not Allowed\n---\n# Index\n")
    write(tmp_path / "log.md", "# Log\n\n## June 1\n* Update.\n")

    readiness = build_okf_readiness(tmp_path)

    assert readiness["conformant"] is False
    assert readiness["missing_frontmatter_pages"] == [{"path": "missing-frontmatter.md"}]
    assert readiness["malformed_frontmatter_pages"] == [{"path": "malformed.md"}]
    assert readiness["invalid_type_pages"] == [{"path": "missing-type.md", "type": "None"}]
    assert readiness["invalid_index_files"] == [
        {"path": "index.md", "reason": "unsupported_root_frontmatter"}
    ]
    assert readiness["invalid_log_files"] == [{"path": "log.md", "reason": "invalid_date_heading"}]


def test_okf_v02_reports_non_utf8_markdown_without_crashing(tmp_path: Path) -> None:
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe")

    readiness = build_okf_readiness(tmp_path)

    assert readiness["conformant"] is False
    assert readiness["invalid_utf8_files"] == [{"path": "binary.md"}]


def test_okf_v02_checks_hidden_paths_and_respects_reserved_name_case(tmp_path: Path) -> None:
    write(tmp_path / ".hidden" / "bad.md", "# Missing metadata\n")
    write(tmp_path / "INDEX.md", "---\ntype: page\n---\n# Uppercase concept\n")

    readiness = build_okf_readiness(tmp_path)

    assert readiness["concept_document_count"] == 2
    assert readiness["reserved_file_count"] == 0
    assert readiness["missing_frontmatter_pages"] == [{"path": ".hidden/bad.md"}]


def test_okf_v02_handles_recursive_and_nonfinite_invalid_types(tmp_path: Path) -> None:
    write(tmp_path / "recursive.md", "---\ntype: &cycle [*cycle]\n---\n# Recursive\n")
    write(tmp_path / "nan.md", "---\ntype: .nan\n---\n# Nonfinite\n")

    readiness = build_okf_readiness(tmp_path)

    json.dumps(readiness, allow_nan=False)
    assert readiness["invalid_type_count"] == 2
    assert all(isinstance(item["type"], str) for item in readiness["invalid_type_pages"])


def test_okf_v02_accepts_supporting_index_sections_and_table_links(tmp_path: Path) -> None:
    write(
        tmp_path / "index.md",
        "# Index\n\n## Context\nSupporting prose.\n\n## Concepts\n"
        "| Concept | Purpose |\n|---|---|\n| [Page](page.md) | Example |\n\n"
        "## Notes\nThis section does not need its own link.\n",
    )

    readiness = build_okf_readiness(tmp_path)

    assert readiness["invalid_index_files"] == []


def test_okf_v02_requires_string_version_and_usable_relative_index_links(tmp_path: Path) -> None:
    write(
        tmp_path / "index.md",
        "---\nokf_version: 0.2\n---\n# Index\n\n- [Page](page.md)\n",
    )
    write(
        tmp_path / "external" / "index.md", "# External\n\n- [Web](https://example.test/page.md)\n"
    )
    write(tmp_path / "image" / "index.md", "# Image\n\n![Diagram](diagram.md)\n")
    write(tmp_path / "absolute" / "index.md", "# Absolute\n\n- [Page](/page.md)\n")
    write(tmp_path / "blank" / "index.md", "# Blank\n\n- [Page]( )\n")

    readiness = build_okf_readiness(tmp_path)

    assert readiness["invalid_index_files"] == [
        {"path": "absolute/index.md", "reason": "missing_markdown_link_entry"},
        {"path": "blank/index.md", "reason": "missing_markdown_link_entry"},
        {"path": "external/index.md", "reason": "missing_markdown_link_entry"},
        {"path": "image/index.md", "reason": "missing_markdown_link_entry"},
        {"path": "index.md", "reason": "wrong_okf_version"},
    ]


def test_okf_v02_rejects_index_without_markdown_link_and_log_prose(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "# Index\n\n## Concepts\n- [[page|Page]]\n")
    write(
        tmp_path / "log.md",
        "# Change Log\n\n## 2026-06-01\n  Indented prose only.\n",
    )

    readiness = build_okf_readiness(tmp_path)

    assert readiness["invalid_index_files"] == [
        {"path": "index.md", "reason": "missing_markdown_link_entry"}
    ]
    assert readiness["invalid_log_files"] == [{"path": "log.md", "reason": "invalid_date_entries"}]


def test_okf_v02_rejects_empty_root_frontmatter(tmp_path: Path) -> None:
    write(tmp_path / "index.md", "---\n---\n# Index\n\n- [Page](page.md)\n")

    readiness = build_okf_readiness(tmp_path)

    assert readiness["invalid_index_files"] == [
        {"path": "index.md", "reason": "unsupported_root_frontmatter"}
    ]


def test_okf_profile_summary_reports_non_utf8_instead_of_crashing(tmp_path: Path) -> None:
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe")

    summary = build_summary(tmp_path, profile="okf-v0.2")
    doctor = run_doctor(tmp_path, profile="okf-v0.2")

    assert summary["okf_readiness"]["invalid_utf8_count"] == 1
    assert any(issue["code"] == "WK015" for issue in doctor.issues)


def test_okf_profile_is_opt_in_for_summary_and_doctor(tmp_path: Path) -> None:
    write(tmp_path / "missing-type.md", "---\ntitle: Missing Type\n---\n# Missing Type\n")

    default_summary = build_summary(tmp_path)
    okf_summary = build_summary(tmp_path, profile="okf-v0.2")
    default_doctor = run_doctor(tmp_path, ignore_orphans=True).to_dict()
    okf_doctor = run_doctor(tmp_path, ignore_orphans=True, profile="okf-v0.2").to_dict()

    assert "okf_readiness" not in default_summary
    assert okf_summary["okf_readiness"]["invalid_type_count"] == 1
    assert default_doctor["issues"] == []
    assert any(issue["code"] == "WK011" for issue in okf_doctor["issues"])


def test_config_enables_okf_and_vault_policy_together(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps(
            {
                "okf": {"enabled": True, "version": "0.2", "exclude": []},
                "vault_policy": {
                    "allowed_types": ["reference"],
                    "curated_prefixes": ["knowledge/"],
                },
            }
        ),
    )
    write(tmp_path / "knowledge" / "missing.md", "# Missing\n")

    summary = build_summary(tmp_path)
    doctor = run_doctor(tmp_path, ignore_orphans=True).to_dict()

    assert summary["okf_readiness"]["missing_frontmatter_count"] == 1
    assert summary["vault_policy_readiness"]["pages_missing_type"] == 1
    assert {issue["code"] for issue in doctor["issues"]} == {"WK010", "WK020"}


def test_explicit_empty_vault_policy_stays_inactive(tmp_path: Path) -> None:
    write(tmp_path / ".wikic" / "config.json", json.dumps({"vault_policy": {}}))
    write(tmp_path / "page.md", "# Page\n")

    summary = build_summary(tmp_path)
    doctor = run_doctor(tmp_path, ignore_orphans=True).to_dict()

    assert "vault_policy_readiness" not in summary
    assert doctor["issues"] == []


def test_okf_exclusions_do_not_remove_files_from_wikic_catalog(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps({"okf": {"enabled": True, "version": "0.2", "exclude": ["work-notes/**"]}}),
    )
    write(tmp_path / "concept.md", "---\ntype: concept\n---\n# Concept\n")
    write(tmp_path / "work-notes" / "CONTRIBUTING.md", "# Contributing\n")

    summary = build_summary(tmp_path)
    catalog = build_catalog(tmp_path)

    assert summary["okf_readiness"]["conformant"] is True
    assert summary["okf_readiness"]["excluded_markdown_count"] == 1
    assert summary["okf_readiness"]["concept_document_count"] == 1
    assert catalog["page_count"] == 2


def test_okf_config_rejects_unsupported_version(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps({"okf": {"enabled": True, "version": "0.3", "exclude": []}}),
    )

    with pytest.raises(ValueError, match="only 0.2 is supported"):
        build_summary(tmp_path)


def test_okf_log_accepts_canonical_frontmatter_shape(tmp_path: Path) -> None:
    write(
        tmp_path / "log.md",
        """---
title: Knowledge Log
type: log
---
# Knowledge Log

## 2026-09-10
- Added a concept.
""",
    )

    readiness = build_okf_readiness(tmp_path)

    assert readiness["invalid_log_files"] == []
    assert readiness["conformant"] is True


def test_vault_policy_is_configured_separately_from_okf(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps(
            {
                "vault_policy": {
                    "allowed_types": ["reference"],
                    "allowed_frontmatter_fields": ["type", "source_url"],
                    "provenance_fields": ["source_url"],
                    "curated_prefixes": ["knowledge/"],
                    "excluded_patterns": ["knowledge/drafts/**"],
                    "source_derived_prefixes": ["knowledge/sources/"],
                    "provenance_expected_types": ["reference"],
                    "type_suggestions": {"knowledge": "reference"},
                }
            }
        ),
    )
    write(tmp_path / "knowledge" / "missing.md", "# Missing\n")
    write(tmp_path / "knowledge" / "sources" / "source.md", "---\ntype: reference\n---\n# S\n")
    write(tmp_path / "knowledge" / "drafts" / "ignored.md", "# Ignored\n")

    readiness = build_vault_policy_readiness(tmp_path)

    assert readiness["profile"] == "vault-policy"
    assert readiness["total_checked_pages"] == 2
    assert readiness["missing_type_pages"] == [
        {"path": "knowledge/missing.md", "suggested_type": "reference"}
    ]
    assert readiness["provenance_gap_pages"] == [
        {"path": "knowledge/sources/source.md", "type": "reference"}
    ]


def test_vault_policy_without_config_has_no_hidden_scope_or_triggers(tmp_path: Path) -> None:
    write(
        tmp_path / "source.md",
        """---
source_derived: true
source_expected: true
provenance_expected: true
---
# Source
""",
    )

    readiness = build_vault_policy_readiness(tmp_path)

    assert readiness["total_checked_pages"] == 0
    assert readiness["pages_missing_type"] == 0
    assert readiness["provenance_gaps"] == 0


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
        tmp_path / "notes" / "operations.md",
        "---\ntype: profile\n---\n# Operations\nA non-eligible page with no timeline.\n",
    )
    write(
        tmp_path / "tools" / "empty-timeline.md",
        "---\ntype: tool\n---\n# Empty Timeline\n\n<!-- timeline -->\n\nNo dated entries yet.\n",
    )

    summary = build_summary(tmp_path)["timeline_coverage"]

    assert summary["eligible_types"] == ["company", "person", "project", "tool"]
    assert summary["eligible_path_prefixes"] == []
    assert summary["eligible_pages"] == 4
    assert summary["pages_with_timeline"] == 1
    assert summary["pages_with_dates_outside_timeline"] == 1
    assert summary["coverage_ratio"] == 0.25
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


def test_summary_handles_non_utf8_generated_report_matches(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps(
            {
                "generated_report_policy": {
                    "rules": [{"path_glob": "reports/*.md", "require_frontmatter": True}]
                }
            }
        ),
    )
    report = tmp_path / "reports" / "binary.md"
    report.parent.mkdir(parents=True)
    report.write_bytes(b"\xff\xfe")

    summary = build_summary(tmp_path, profile="okf-v0.2")

    assert summary["okf_readiness"]["invalid_utf8_files"] == [{"path": "reports/binary.md"}]
    assert summary["generated_report_policy"]["violation_count"] == 1


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


def test_load_config_can_replace_default_excludes(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        '{"exclude_mode": "replace", "exclude": ["drafts/**"]}',
    )

    config = load_config(tmp_path)

    assert config["exclude"] == ["drafts/**"]


def test_summary_uses_configured_timeline_and_large_page_policy(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps(
            {
                "timeline_policy": {
                    "eligible_types": ["milestone"],
                    "eligible_path_prefixes": ["operations/"],
                },
                "large_page_word_threshold": 3,
            }
        ),
    )
    write(tmp_path / "operations" / "status.md", "# Status\n2026-06-01 update\n")
    write(tmp_path / "notes" / "large.md", "# Large\none two three\n")

    summary = build_summary(tmp_path)

    assert summary["timeline_coverage"]["eligible_types"] == ["milestone"]
    assert summary["timeline_coverage"]["eligible_path_prefixes"] == ["operations/"]
    assert summary["timeline_coverage"]["eligible_pages"] == 1
    assert summary["large_page_pressure"]["threshold_word_count"] == 3
    assert summary["large_page_pressure"]["page_count"] == 2


def test_doctor_uses_safe_configured_required_root_files(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        '{"required_root_files": ["index.md", "policy/schema.md"]}',
    )
    write(tmp_path / "index.md", "# Index\n")

    report = run_doctor(tmp_path, ignore_orphans=True, require_vault_files=True)

    assert [issue["path"] for issue in report.issues if issue["code"] == "WK003"] == [
        "policy/schema.md"
    ]


def test_doctor_rejects_directory_as_required_root_file(tmp_path: Path) -> None:
    write(tmp_path / ".wikic" / "config.json", '{"required_root_files": ["required"]}')
    (tmp_path / "required").mkdir()

    report = run_doctor(tmp_path, ignore_orphans=True, require_vault_files=True)

    assert any(issue["code"] == "WK003" and issue["path"] == "required" for issue in report.issues)


def test_load_config_rejects_unsafe_required_root_files(tmp_path: Path) -> None:
    write(tmp_path / ".wikic" / "config.json", '{"required_root_files": ["../private.md"]}')

    try:
        load_config(tmp_path)
    except ValueError as exc:
        assert "safe and relative" in str(exc)
    else:
        raise AssertionError("expected unsafe required_root_files to fail")


@pytest.mark.parametrize(
    "payload,expected_text",
    [
        ({"exclude_mode": "merge"}, "exclude_mode"),
        ({"exclude_mode": []}, "exclude_mode"),
        ({"large_page_word_threshold": True}, "large_page_word_threshold"),
        ({"naming_policy_enabled": "yes"}, "naming_policy_enabled"),
        ({"timeline_policy": {"eligible_types": [1]}}, "timeline_policy.eligible_types"),
        ({"vault_policy": {"allowed_types": [1]}}, "vault_policy.allowed_types"),
        (
            {"vault_policy": {"provenance_trigger_fields": [1]}},
            "vault_policy.provenance_trigger_fields",
        ),
        ({"vault_policy": {"type_suggestions": {"projects": 1}}}, "type_suggestions"),
        ({"required_root_files": ["C:\\private.md"]}, "safe and relative"),
    ],
)
def test_load_config_rejects_invalid_policy_values(
    tmp_path: Path, payload: dict[str, object], expected_text: str
) -> None:
    write(tmp_path / ".wikic" / "config.json", json.dumps(payload))

    with pytest.raises(ValueError, match=expected_text):
        load_config(tmp_path)


def test_summary_uses_configured_naming_policy_exemptions(tmp_path: Path) -> None:
    write(
        tmp_path / ".wikic" / "config.json",
        json.dumps(
            {
                "naming_policy_exemptions": [
                    "README.md",
                    "SCHEMA.md",
                    "catalog/repositories/*--*.md",
                    "Brand Folder/*.md",
                ]
            }
        ),
    )
    write(tmp_path / "README.md", "# Conventional README\n[[SCHEMA]]\n")
    write(tmp_path / "SCHEMA.md", "# Conventional schema policy\n[[README]]\n")
    write(tmp_path / "Brand Folder" / "Keep Case.md", "# Keep Case\n[[README]]\n")
    write(
        tmp_path / "catalog" / "repositories" / "ExampleOrg--SampleTool.md",
        "# Repo card\n[[README]]\n",
    )
    write(tmp_path / "scratch" / "Bad Name.md", "# Bad Name\n[[README]]\n")

    summary = build_summary(tmp_path)

    assert summary["naming_policy_violation_count"] == 1
    assert summary["naming_policy_violations"] == [
        {"path": "scratch/Bad Name.md", "expected_slug": "scratch/bad-name"}
    ]


def test_summary_can_disable_naming_policy(tmp_path: Path) -> None:
    write(tmp_path / ".wikic" / "config.json", '{"naming_policy_enabled": false}')
    write(tmp_path / "Bad Name.md", "# Bad Name\n")

    summary = build_summary(tmp_path)

    assert summary["naming_policy_violation_count"] == 0
    assert summary["naming_policy_violations"] == []


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
            "message": "Missing internal link target: missing-page",
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
            "message": "Missing internal link target: wiki-c",
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
            "message": "Missing internal link target appears on 2 pages: wiki-c",
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


def test_repair_plan_keeps_markdown_link_repairs_inspection_only(tmp_path: Path) -> None:
    write(tmp_path / "a.md", "# A\n[Wikic](wiki-c.md)\n")
    write(tmp_path / "b.md", "# B\n[Wikic](wiki-c.md)\n")
    write(tmp_path / "tools" / "wikic.md", "# Wikic\n")

    payload = build_repair_plan(tmp_path, ignore_orphans=True, patch_preview=True)

    assert payload["summary"] == {
        "operation_count": 1,
        "retarget_operations": 0,
        "inspect_operations": 1,
        "review_required": 1,
    }
    operation = payload["operations"][0]
    assert operation["action"] == "inspect_missing_target"
    assert operation["occurrences"] == []
    assert "patch_preview" not in operation
    assert "replacement_target" not in operation
    assert "does not rewrite automatically" in operation["reason"]


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
