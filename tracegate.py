#!/usr/bin/env python3
"""Standalone release traceability checks and local artifact importers."""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "data" / "release_candidate.json"
DEFAULT_RULES = ROOT / "rules.json"
DEFAULT_OUTPUT = ROOT / "data" / "imported_release.json"


class DatasetError(ValueError):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot load {path}: {exc}") from exc


def load_dataset(path: Path, require_release: bool = True) -> dict[str, Any]:
    data = load_json(path)
    if not isinstance(data, dict):
        raise DatasetError("manifest root must be a JSON object")
    version = data.get("schema_version", 1)
    if version != 1:
        raise DatasetError(f"unsupported manifest schema_version: {version!r}; supported version is 1")
    if require_release and (not isinstance(data.get("release"), str) or not data["release"].strip()):
        raise DatasetError("manifest must contain a non-empty release name")
    if "release" in data and (not isinstance(data["release"], str) or not data["release"].strip()):
        raise DatasetError("release must be a non-empty string when provided")
    if not isinstance(data.get("artifacts"), list) or not isinstance(data.get("edges"), list):
        raise DatasetError("manifest must contain artifact and edge arrays")
    for index, artifact in enumerate(data["artifacts"]):
        if not isinstance(artifact, dict):
            raise DatasetError(f"artifacts[{index}] must be an object")
        if not isinstance(artifact.get("id"), str) or not artifact["id"].strip():
            raise DatasetError(f"artifacts[{index}].id must be a non-empty string")
        if not isinstance(artifact.get("kind"), str) or not artifact["kind"].strip():
            raise DatasetError(f"artifacts[{index}].kind must be a non-empty string")
        if "evidence_ids" in artifact and (
            not isinstance(artifact["evidence_ids"], list)
            or any(not isinstance(item, str) for item in artifact["evidence_ids"])
        ):
            raise DatasetError(f"artifact {artifact['id']} evidence_ids must be an array of strings")
    for index, edge in enumerate(data["edges"]):
        if not isinstance(edge, dict) or any(not isinstance(edge.get(key), str) or not edge[key].strip()
                                             for key in ("from", "to", "type")):
            raise DatasetError(f"edges[{index}] must contain non-empty string from, to, and type fields")
    return data


def load_rules(path: Path) -> dict[str, Any]:
    rules = load_json(path)
    if not isinstance(rules, dict):
        raise DatasetError("rules root must be a JSON object")
    version = rules.get("schema_version", 1)
    if version != 1:
        raise DatasetError(f"unsupported rules schema_version: {version!r}; supported version is 1")
    for key in ("required_relations", "revision_checks", "evidence_checks"):
        if not isinstance(rules.get(key), list):
            raise DatasetError(f"rules file must contain a {key} array")
    for index, rule in enumerate(rules["required_relations"]):
        if not isinstance(rule, dict) or any(not isinstance(rule.get(key), str) or not rule[key]
                                             for key in ("relation", "kind", "finding")):
            raise DatasetError(f"required_relations[{index}] needs relation, kind, and finding strings")
        if "required" in rule and not isinstance(rule["required"], bool):
            raise DatasetError(f"required_relations[{index}].required must be true or false")
    for list_key, keys in (
        ("revision_checks", ("relation", "kind", "requirement_revision_field",
                             "evidence_revision_field", "finding", "message")),
        ("evidence_checks", ("source_relation", "source_kind", "relation", "kind",
                             "status_field", "required_status", "source_revision_field",
                             "evidence_revision_field", "mismatch_finding", "missing_finding",
                             "mismatch_message", "missing_message")),
    ):
        for index, rule in enumerate(rules[list_key]):
            if not isinstance(rule, dict) or any(not isinstance(rule.get(key), str) or not rule[key]
                                                 for key in keys):
                raise DatasetError(f"{list_key}[{index}] is missing one or more required string fields")
    for key in ("requirement_kind", "claim_kind", "baseline_kind", "baseline_relation",
                "baseline_finding", "claim_finding", "claim_message"):
        if key in rules and not isinstance(rules[key], str):
            raise DatasetError(f"rules field {key} must be a string")
    for key in ("claim_evidence_kinds", "baseline_artifact_kinds"):
        if key in rules and (not isinstance(rules[key], list) or any(not isinstance(x, str) for x in rules[key])):
            raise DatasetError(f"rules field {key} must be an array of strings")
    if "blocking_findings" in rules and (
        not isinstance(rules["blocking_findings"], list)
        or any(not isinstance(code, str) or not code for code in rules["blocking_findings"])
    ):
        raise DatasetError("rules field blocking_findings must be an array of non-empty strings")
    return rules


def index_dataset(data: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, str]]]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for artifact in data["artifacts"]:
        if not isinstance(artifact, dict) or not artifact.get("id") or not artifact.get("kind"):
            raise DatasetError("each artifact needs an id and kind")
        artifact_id = artifact["id"]
        if artifact_id in artifacts:
            raise DatasetError(f"duplicate artifact id: {artifact_id}")
        artifacts[artifact_id] = artifact

    adjacency: dict[str, list[dict[str, str]]] = defaultdict(list)
    for edge in data["edges"]:
        if not isinstance(edge, dict) or not all(edge.get(key) for key in ("from", "to", "type")):
            raise DatasetError("each edge needs from, to, and type")
        if edge["from"] not in artifacts or edge["to"] not in artifacts:
            raise DatasetError(f"edge references unknown artifact: {edge}")
        adjacency[edge["from"]].append(edge)
    return artifacts, adjacency


def targets(adjacency: dict[str, list[dict[str, str]]], source: str, relation: str) -> list[str]:
    return [edge["to"] for edge in adjacency.get(source, []) if edge["type"] == relation]


def graph_reachable(adjacency: dict[str, list[dict[str, str]]], start: str) -> list[str]:
    visited: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        for edge in adjacency.get(node, []):
            if edge["to"] not in visited:
                visited.add(edge["to"])
                stack.append(edge["to"])
    visited.discard(start)
    return sorted(visited)


def analyze(data: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    artifacts, adjacency = index_dataset(data)
    findings: list[dict[str, str]] = []
    requirements = [item for item in data["artifacts"] if item["kind"] == rules.get("requirement_kind", "requirement")]
    impact: dict[str, list[str]] = {}
    trace_total = trace_present = evidence_total = evidence_complete = 0
    valid_evidence_ids: set[str] = set()

    def add(code: str, artifact_id: str, message: str) -> None:
        findings.append({"code": code, "artifact_id": artifact_id, "message": message})

    required_relations = rules["required_relations"]
    for req in requirements:
        req_id = req["id"]
        linked_by_relation: dict[str, list[str]] = {}
        for requirement in required_relations:
            relation, kind = requirement["relation"], requirement["kind"]
            linked = [target for target in targets(adjacency, req_id, relation) if artifacts[target]["kind"] == kind]
            linked_by_relation[relation] = linked
            trace_total += 1
            if linked:
                trace_present += 1
            elif requirement.get("required", True):
                add(requirement.get("finding", "TRACE_LINK_MISSING"), req_id,
                    requirement.get("message", f"Requirement has no {kind} linked by {relation}."))

        related_ids = set(graph_reachable(adjacency, req_id)) | {req_id}
        impacted_baselines: set[str] = set()
        baseline_kind = rules.get("baseline_kind", "release_baseline")
        baseline_relation = rules.get("baseline_relation", "includes")
        baseline_kinds = set(rules.get("baseline_artifact_kinds", []))
        for baseline in (item for item in data["artifacts"] if item["kind"] == baseline_kind):
            included = set(targets(adjacency, baseline["id"], baseline_relation))
            if baseline_kinds:
                expected = {aid for aid in related_ids if artifacts[aid]["kind"] in baseline_kinds}
                missing = expected - included
                if missing:
                    add(rules.get("baseline_finding", "BASELINE_ARTIFACT_MISSING"), baseline["id"],
                        "Release baseline omits linked artifacts: " + ", ".join(sorted(missing)) + ".")
            if included & related_ids:
                impacted_baselines.add(baseline["id"])
        impact[req_id] = sorted((related_ids - {req_id}) | impacted_baselines)

        evidence_total += len(rules["revision_checks"]) + len(rules["evidence_checks"])
        for check in rules["revision_checks"]:
            relation = check["relation"]
            linked = [artifacts[aid] for aid in linked_by_relation.get(relation, [])
                      if artifacts[aid]["kind"] == check["kind"]]
            current = [item for item in linked if item.get(check["evidence_revision_field"]) == req.get(check["requirement_revision_field"])]
            if current:
                evidence_complete += 1
                valid_evidence_ids.update(item["id"] for item in current)
            else:
                add(check["finding"], req_id, check["message"])

        for check in rules["evidence_checks"]:
            source_ids = [aid for aid in linked_by_relation.get(check["source_relation"], [])
                          if artifacts[aid]["kind"] == check["source_kind"]]
            matched: list[str] = []
            mismatched = False
            for source_id in source_ids:
                source = artifacts[source_id]
                for evidence_id in targets(adjacency, source_id, check["relation"]):
                    evidence = artifacts[evidence_id]
                    if evidence["kind"] != check["kind"]:
                        continue
                    is_current = (
                        evidence.get(check["status_field"]) == check["required_status"]
                        and evidence.get(check["evidence_revision_field"]) == source.get(check["source_revision_field"])
                    )
                    if is_current:
                        matched.append(evidence_id)
                        valid_evidence_ids.add(evidence_id)
                    else:
                        mismatched = True
                        add(check["mismatch_finding"], evidence_id,
                            check["mismatch_message"].format(source_id=source_id))
            if matched:
                evidence_complete += 1
            elif not mismatched:
                add(check["missing_finding"], req_id, check["missing_message"])

    claim_kind = rules.get("claim_kind", "claim")
    acceptable_claim_evidence = set(rules.get("claim_evidence_kinds", []))
    claims = [item for item in data["artifacts"] if item["kind"] == claim_kind]
    unsupported_claims: list[str] = []
    for claim in claims:
        support_ids = claim.get("evidence_ids", [])
        supported = bool(support_ids) and all(
            evidence_id in artifacts
            and artifacts[evidence_id]["kind"] in acceptable_claim_evidence
            and evidence_id in valid_evidence_ids
            for evidence_id in support_ids
        )
        if not supported:
            unsupported_claims.append(claim["id"])
            add(rules.get("claim_finding", "CLAIM_UNSUPPORTED"), claim["id"],
                rules.get("claim_message", "Claim has no linked current evidence of an accepted type."))

    blocking_codes = set(rules.get("blocking_findings", [item["code"] for item in findings]))
    blocking_count = sum(item["code"] in blocking_codes for item in findings)
    gate = "BLOCK" if blocking_count else ("WARN" if findings else "PASS")
    return {
        "release": data.get("release", "unnamed-release"),
        "requirements": [item["id"] for item in requirements],
        "traceability_coverage": trace_present / trace_total if trace_total else 1.0,
        "traceability_links_present": trace_present,
        "traceability_links_expected": trace_total,
        "evidence_completeness": evidence_complete / evidence_total if evidence_total else 1.0,
        "evidence_items_current": evidence_complete,
        "evidence_items_expected": evidence_total,
        "unsupported_claim_rate": len(unsupported_claims) / len(claims) if claims else 0.0,
        "unsupported_claims": unsupported_claims,
        "findings": findings,
        "blocking_finding_count": blocking_count,
        "change_impact": impact,
        "gate": gate,
    }


def render_report(report: dict[str, Any]) -> str:
    lines = [
        f"TraceGate report: {report['release']}",
        f"Release gate: {report['gate']}",
        f"Blocking findings: {report['blocking_finding_count']}",
        f"Traceability coverage: {report['traceability_coverage']:.0%} "
        f"({report['traceability_links_present']}/{report['traceability_links_expected']} required links)",
        f"Evidence completeness: {report['evidence_completeness']:.0%} "
        f"({report['evidence_items_current']}/{report['evidence_items_expected']} current evidence checks)",
        f"Unsupported-claim rate: {report['unsupported_claim_rate']:.0%}",
        "",
        "Findings:",
    ]
    if report["findings"]:
        lines.extend(f"- [{item['code']}] {item['artifact_id']}: {item['message']}" for item in report["findings"])
    else:
        lines.append("- None")
    lines.extend(["", "Change impact:"])
    for req_id, impacted in report["change_impact"].items():
        lines.append(f"- {req_id} -> {', '.join(impacted) if impacted else '(no linked artifacts)'}")
    return "\n".join(lines)


def render_html_report(report: dict[str, Any]) -> str:
    findings = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(item["code"]), html.escape(item["artifact_id"]), html.escape(item["message"])
        )
        for item in report["findings"]
    ) or '<tr><td colspan="3">No findings</td></tr>'
    impact = "".join(
        "<li><strong>{}</strong> → {}</li>".format(
            html.escape(req_id), html.escape(", ".join(ids) if ids else "No linked artifacts")
        )
        for req_id, ids in report["change_impact"].items()
    ) or "<li>No requirements in this manifest</li>"
    gate_class = report["gate"].lower()
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TraceGate report — {html.escape(report['release'])}</title>
<style>
:root{{color-scheme:light dark;font:16px/1.5 system-ui,sans-serif;--line:#87909c;--panel:color-mix(in srgb,Canvas 92%,#64748b 8%)}}
body{{max-width:1100px;margin:2rem auto;padding:0 1rem}}h1,h2{{line-height:1.2}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.8rem;margin:1.5rem 0}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:1rem}}.value{{font-size:1.5rem;font-weight:700}}.gate{{font-weight:800}}.block{{color:#d43b3b}}.warn{{color:#b87900}}.pass{{color:#21834a}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:.65rem;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}code{{overflow-wrap:anywhere}}li{{margin:.3rem 0}}
</style></head><body>
<h1>TraceGate report</h1><p>Release: <strong>{html.escape(report['release'])}</strong></p>
<div class="metrics">
<div class="card"><div>Release gate</div><div class="value gate {gate_class}">{html.escape(report['gate'])}</div><div>{report['blocking_finding_count']} blocking findings</div></div>
<div class="card"><div>Traceability coverage</div><div class="value">{report['traceability_coverage']:.0%}</div><div>{report['traceability_links_present']} / {report['traceability_links_expected']} links</div></div>
<div class="card"><div>Evidence completeness</div><div class="value">{report['evidence_completeness']:.0%}</div><div>{report['evidence_items_current']} / {report['evidence_items_expected']} checks</div></div>
<div class="card"><div>Unsupported claims</div><div class="value">{report['unsupported_claim_rate']:.0%}</div><div>{len(report['unsupported_claims'])} unsupported</div></div>
</div>
<h2>Findings</h2><table><thead><tr><th>Code</th><th>Artifact</th><th>Details</th></tr></thead><tbody>{findings}</tbody></table>
<h2>Change impact</h2><ul>{impact}</ul>
</body></html>"""


def merge_imports(manifest: dict[str, Any], incoming_artifacts: list[dict[str, Any]],
                  incoming_edges: list[dict[str, str]]) -> dict[str, Any]:
    merged = dict(manifest)
    artifact_map = {item["id"]: item for item in manifest["artifacts"]}
    for item in incoming_artifacts:
        existing = artifact_map.get(item["id"])
        if existing is not None and existing.get("kind") != item.get("kind"):
            raise DatasetError(f"artifact {item['id']} already exists with kind {existing.get('kind')!r}")
        artifact_map[item["id"]] = {**(existing or {}), **item}
    edge_keys = {(edge["from"], edge["to"], edge["type"]) for edge in manifest["edges"]}
    edges = list(manifest["edges"])
    for edge in incoming_edges:
        key = (edge["from"], edge["to"], edge["type"])
        if key not in edge_keys:
            edges.append(edge)
            edge_keys.add(key)
    merged["artifacts"] = list(artifact_map.values())
    merged["edges"] = edges
    index_dataset(merged)
    return merged


def _issue_requirement_ids(issue: dict[str, Any]) -> list[str]:
    fields = issue.get("fields") or {}
    explicit = issue.get("requirement_ids", fields.get("requirement_ids", []))
    if isinstance(explicit, str):
        explicit = [explicit]
    if explicit:
        return sorted(set(str(item) for item in explicit))
    labels = fields.get("labels", issue.get("labels", []))
    text = " ".join(str(item) for item in labels)
    return sorted(set(re.findall(r"\bREQ-[A-Za-z0-9-]+\b", text, flags=re.IGNORECASE)))


def import_jira_export(manifest: dict[str, Any], export: Any) -> tuple[dict[str, Any], int, int]:
    issues = export.get("issues") if isinstance(export, dict) else export
    if not isinstance(issues, list):
        raise DatasetError("Jira export must be an array of issues or an object with an issues array")
    requirement_ids = {item["id"] for item in manifest["artifacts"] if item["kind"] == "requirement"}
    artifacts: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        fields = issue.get("fields") or {}
        issue_id = issue.get("key") or issue.get("id")
        if not issue_id:
            continue
        summary = fields.get("summary") or issue.get("summary") or str(issue_id)
        status = fields.get("status") or issue.get("status") or "Unknown"
        if isinstance(status, dict):
            status = status.get("name", "Unknown")
        artifacts.append({
            "id": str(issue_id),
            "kind": "work_item",
            "title": str(summary),
            "status": str(status),
            "source": "jira_export",
        })
        for req_id in _issue_requirement_ids(issue):
            if req_id in requirement_ids:
                edges.append({"from": req_id, "to": str(issue_id), "type": "tracked_by"})
    return merge_imports(manifest, artifacts, edges), len(artifacts), len(edges)


def import_git_history(manifest: dict[str, Any], repo_path: Path,
                       requirement_id: str | None, artifact_kind: str,
                       relation: str, max_commits: int) -> tuple[dict[str, Any], int, int]:
    if max_commits < 1:
        raise DatasetError("--max-commits must be at least 1")
    if not artifact_kind.strip() or not relation.strip():
        raise DatasetError("--kind and --relation must be non-empty")
    try:
        output = subprocess.run(
            ["git", "-C", str(repo_path), "log", f"--max-count={max_commits}",
             "--format=%H%x1f%h%x1f%s%x1f%b%x00"],
            check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise DatasetError(f"cannot read Git history from {repo_path}: {detail.strip()}") from exc

    requirements = {item["id"] for item in manifest["artifacts"] if item["kind"] == "requirement"}
    if requirement_id and requirement_id not in requirements:
        raise DatasetError(f"requirement {requirement_id} is not in the manifest")
    artifacts: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for record in output.split("\x00"):
        fields = record.strip().split("\x1f", 3)
        if len(fields) < 3:
            continue
        commit, short_sha, subject = fields[:3]
        body = fields[3] if len(fields) == 4 else ""
        prefix = "RTL" if artifact_kind == "rtl_change" else artifact_kind.upper().replace("_", "-")
        artifact_id = f"{prefix}-{short_sha}"
        artifacts.append({
            "id": artifact_id,
            "kind": artifact_kind,
            "title": subject.strip(),
            "commit": commit,
            "source": "git_history",
        })
        linked_requirements = [requirement_id] if requirement_id else re.findall(
            r"\bREQ-[A-Za-z0-9-]+\b", subject + " " + body, flags=re.IGNORECASE
        )
        for req_id in sorted(set(linked_requirements)):
            if req_id in requirements:
                edges.append({"from": req_id, "to": artifact_id, "type": relation})
    return merge_imports(manifest, artifacts, edges), len(artifacts), len(edges)


EVIDENCE_LINKS = {
    "design_spec": ("requirement", "documented_by"),
    "reference_model": ("requirement", "modeled_by"),
    "verification": ("rtl_change", "verified_by"),
    "fpga_build": ("rtl_change", "built_as"),
}


def import_evidence_export(manifest: dict[str, Any], export: Any) -> tuple[dict[str, Any], int, int]:
    records = export.get("records") if isinstance(export, dict) else None
    if not isinstance(records, list):
        raise DatasetError("evidence export must contain a records array")
    known = {item["id"]: item for item in manifest["artifacts"]}
    artifacts: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise DatasetError(f"records[{index}] must be an object")
        artifact_id, kind = record.get("id"), record.get("kind")
        source_id, relation = record.get("source_id"), record.get("relation")
        if not all(isinstance(value, str) and value.strip()
                   for value in (artifact_id, kind, source_id, relation)):
            raise DatasetError(f"records[{index}] needs non-empty id, kind, source_id, and relation strings")
        if kind not in EVIDENCE_LINKS:
            raise DatasetError(f"records[{index}] has unsupported evidence kind {kind!r}")
        expected_source_kind, expected_relation = EVIDENCE_LINKS[kind]
        source = known.get(source_id)
        if source is None:
            raise DatasetError(f"records[{index}] refers to unknown source artifact {source_id!r}")
        if source.get("kind") != expected_source_kind or relation != expected_relation:
            raise DatasetError(
                f"{kind} records must link from {expected_source_kind} using {expected_relation}"
            )
        artifact = {key: value for key, value in record.items()
                    if key not in {"source_id", "relation"}}
        artifacts.append(artifact)
        edges.append({"from": source_id, "to": artifact_id, "type": relation})
        known[artifact_id] = artifact
    return merge_imports(manifest, artifacts, edges), len(artifacts), len(edges)


def write_dataset(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check release traceability and import local engineering artifacts.")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="evaluate a release manifest")
    check.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    check.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    check.add_argument("--format", choices=("text", "json", "html"), default="text")
    check.add_argument("--output", type=Path, help="write the report to this file instead of standard output")
    check.add_argument("--fail-on-block", action="store_true", help="exit with status 1 when the gate is BLOCK")

    jira = commands.add_parser("import-jira", help="merge a Jira JSON export into a manifest")
    jira.add_argument("export", type=Path)
    jira.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    jira.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    git = commands.add_parser("import-git", help="import local Git commits as engineering changes")
    git.add_argument("repository", type=Path)
    git.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    git.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    git.add_argument("--requirement-id")
    git.add_argument("--kind", default="rtl_change")
    git.add_argument("--relation", default="implemented_by")
    git.add_argument("--max-commits", type=int, default=100)

    artifacts = commands.add_parser("import-artifacts", help="merge a normalized artifacts-and-edges JSON export")
    artifacts.add_argument("export", type=Path)
    artifacts.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    artifacts.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    evidence = commands.add_parser(
        "import-evidence", help="import document, model, verification, or FPGA build records"
    )
    evidence.add_argument("export", type=Path)
    evidence.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    evidence.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if not args_in:
        args_in = ["check"]
    elif args_in[0] not in {"check", "import-jira", "import-git", "import-artifacts", "import-evidence", "-h", "--help"}:
        args_in.insert(0, "check")
    args = make_parser().parse_args(args_in)
    try:
        if args.command == "check":
            report = analyze(load_dataset(args.manifest), load_rules(args.rules))
            rendered = {
                "text": render_report(report),
                "json": json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                "html": render_html_report(report),
            }[args.format]
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
                print(f"Wrote {args.format.upper()} report to {args.output}")
            else:
                print(rendered, end="" if rendered.endswith("\n") else "\n")
            return 1 if args.fail_on_block and report["gate"] == "BLOCK" else 0

        manifest = load_dataset(args.manifest)
        if args.command == "import-jira":
            merged, artifact_count, edge_count = import_jira_export(manifest, load_json(args.export))
        elif args.command == "import-git":
            merged, artifact_count, edge_count = import_git_history(
                manifest, args.repository, args.requirement_id, args.kind, args.relation, args.max_commits
            )
        elif args.command == "import-evidence":
            merged, artifact_count, edge_count = import_evidence_export(manifest, load_json(args.export))
        else:
            incoming = load_dataset(args.export, require_release=False)
            merged = merge_imports(manifest, incoming["artifacts"], incoming["edges"])
            artifact_count, edge_count = len(incoming["artifacts"]), len(incoming["edges"])
        write_dataset(args.output, merged)
        print(f"Imported or merged {artifact_count} artifacts and {edge_count} links into {args.output}")
        return 0
    except DatasetError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
