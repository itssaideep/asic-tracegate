#!/usr/bin/env python3
"""Standalone release traceability checks and local artifact importers."""

from __future__ import annotations

import argparse
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


def write_dataset(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check release traceability and import local engineering artifacts.")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="evaluate a release manifest")
    check.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    check.add_argument("--rules", type=Path, default=DEFAULT_RULES)

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    if not args_in:
        args_in = ["check"]
    elif args_in[0] not in {"check", "import-jira", "import-git", "import-artifacts", "-h", "--help"}:
        args_in.insert(0, "check")
    args = make_parser().parse_args(args_in)
    try:
        if args.command == "check":
            report = analyze(load_dataset(args.manifest), load_rules(args.rules))
            print(render_report(report))
            return 0

        manifest = load_dataset(args.manifest)
        if args.command == "import-jira":
            merged, artifact_count, edge_count = import_jira_export(manifest, load_json(args.export))
        elif args.command == "import-git":
            merged, artifact_count, edge_count = import_git_history(
                manifest, args.repository, args.requirement_id, args.kind, args.relation, args.max_commits
            )
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
