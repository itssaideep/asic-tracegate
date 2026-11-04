#!/usr/bin/env python3
"""Standalone release traceability checks and local artifact importers."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
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
    findings: list[dict[str, Any]] = []
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
            mismatches: list[tuple[str, str]] = []
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
                        mismatches.append((evidence_id, source_id))
            if matched:
                evidence_complete += 1
            elif mismatches:
                for evidence_id, source_id in mismatches:
                    add(check["mismatch_finding"], evidence_id,
                        check["mismatch_message"].format(source_id=source_id))
            else:
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
    for finding in findings:
        finding["severity"] = "BLOCK" if finding["code"] in blocking_codes else "WARN"
    blocking_count = sum(item["code"] in blocking_codes for item in findings)
    warning_count = len(findings) - blocking_count
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
        "findings_total": len(findings),
        "blocking_finding_count": blocking_count,
        "warning_finding_count": warning_count,
        "change_impact": impact,
        "gate": gate,
    }


def render_report(report: dict[str, Any]) -> str:
    lines = [
        f"TraceGate report: {report['release']}",
        f"Release gate: {report['gate']}",
        f"Blocking findings: {report['blocking_finding_count']}",
        f"Warnings: {report['warning_finding_count']}",
        f"Traceability coverage: {report['traceability_coverage']:.0%} "
        f"({report['traceability_links_present']}/{report['traceability_links_expected']} required links)",
        f"Evidence completeness: {report['evidence_completeness']:.0%} "
        f"({report['evidence_items_current']}/{report['evidence_items_expected']} current evidence checks)",
        f"Unsupported-claim rate: {report['unsupported_claim_rate']:.0%}",
        "",
        "Findings:",
    ]
    if report["findings"]:
        lines.extend(f"- [{item['severity']} {item['code']}] {item['artifact_id']}: {item['message']}"
                     for item in report["findings"])
    else:
        lines.append("- None")
    if report.get("findings_hidden", 0):
        lines.append(f"- {report['findings_hidden']} additional findings hidden by severity filter")
    lines.extend(["", "Change impact:"])
    for req_id, impacted in report["change_impact"].items():
        lines.append(f"- {req_id} -> {', '.join(impacted) if impacted else '(no linked artifacts)'}")
    return "\n".join(lines)


def render_html_report(report: dict[str, Any]) -> str:
    findings = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            html.escape(item["severity"]), html.escape(item["code"]),
            html.escape(item["artifact_id"]), html.escape(item["message"])
        )
        for item in report["findings"]
    ) or '<tr><td colspan="4">No findings</td></tr>'
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
<h2>Findings</h2><p>{report['findings_total']} total · {report['blocking_finding_count']} blocking · {report['warning_finding_count']} warnings · showing {len(report['findings'])}</p>
<table><thead><tr><th>Severity</th><th>Code</th><th>Artifact</th><th>Details</th></tr></thead><tbody>{findings}</tbody></table>
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
    retrieval_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for record in output.split("\x00"):
        fields = record.strip().split("\x1f", 3)
        if len(fields) < 3:
            continue
        commit, short_sha, subject = fields[:3]
        body = fields[3] if len(fields) == 4 else ""
        prefix = "RTL" if artifact_kind == "rtl_change" else ("SRC" if artifact_kind == "source_change" else artifact_kind.upper().replace("_", "-"))
        artifact_id = f"{prefix}-{short_sha}"
        git_payload = {"commit": commit, "subject": subject.strip(), "body": body.strip()}
        git_digest = f"sha256:{hashlib.sha256(json.dumps(git_payload, sort_keys=True).encode('utf-8')).hexdigest()}"
        artifacts.append({
            "id": artifact_id,
            "kind": artifact_kind,
            "title": subject.strip(),
            "commit": commit,
            "source": "git_history",
            "retrieval_time": retrieval_time,
            "content_digest": git_digest,
            "provenance": {
                "commit_sha": commit,
                "subject": subject.strip(),
                "body": body.strip(),
                "retrieval_time": retrieval_time,
                "content_digest": git_digest,
                "source": "local_git",
            },
        })
        linked_requirements = [requirement_id] if requirement_id else re.findall(
            r"\bREQ-[A-Za-z0-9-]+\b", subject + " " + body, flags=re.IGNORECASE
        )
        for req_id in sorted(set(linked_requirements)):
            if req_id in requirements:
                edges.append({"from": req_id, "to": artifact_id, "type": relation})
    return merge_imports(manifest, artifacts, edges), len(artifacts), len(edges)


def parse_github_repo(repo_input: str) -> tuple[str, str, str]:
    text = repo_input.strip()
    pattern = r"^(?:https?://github\.com/|git@github\.com:)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
    match = re.match(pattern, text)
    if not match:
        raise DatasetError(
            f"invalid GitHub repository format: {repo_input!r}. "
            "Expected 'owner/repo' or 'https://github.com/owner/repo'"
        )
    owner, repo = match.group(1), match.group(2)
    canonical_url = f"https://github.com/{owner}/{repo}"
    return owner, repo, canonical_url


def fetch_github_commits(owner: str, repo: str, max_commits: int = 100,
                         branch: str | None = None, token: str | None = None) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    page = 1
    per_page = min(max_commits, 100)
    repo_url = f"https://github.com/{owner}/{repo}"
    auth_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ASIC-TraceGate/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if auth_token and auth_token.strip():
        headers["Authorization"] = f"Bearer {auth_token.strip()}"

    while len(commits) < max_commits:
        params = [f"per_page={per_page}", f"page={page}"]
        if branch:
            params.append(f"sha={urllib.parse.quote(branch)}")
        url = f"https://api.github.com/repos/{owner}/{repo}/commits?{'&'.join(params)}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_data = resp.read()
                page_commits = json.loads(raw_data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise DatasetError(
                    f"GitHub repository {owner}/{repo} not found (HTTP 404). "
                    "Verify the repository URL and permissions."
                ) from exc
            if exc.code in (403, 429):
                rate_limit = exc.headers.get("x-ratelimit-remaining")
                if rate_limit == "0":
                    raise DatasetError(
                        f"GitHub API rate limit exceeded for {repo_url}. "
                        "Provide a personal access token via --token or GITHUB_TOKEN environment variable."
                    ) from exc
                raise DatasetError(f"GitHub API access forbidden (HTTP {exc.code}): {exc.reason}") from exc
            if exc.code == 401:
                raise DatasetError("GitHub authentication failed (HTTP 401). Verify provided token.") from exc
            raise DatasetError(f"GitHub API error for {repo_url} (HTTP {exc.code}): {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise DatasetError(f"cannot connect to GitHub ({repo_url}): {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise DatasetError(f"timed out connecting to GitHub ({repo_url})") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise DatasetError(f"invalid JSON response from GitHub API for {repo_url}: {exc}") from exc

        if not isinstance(page_commits, list):
            raise DatasetError(f"unexpected GitHub API response format for {repo_url}")
        if not page_commits:
            break
        commits.extend(page_commits)
        if len(page_commits) < per_page:
            break
        page += 1

    return commits[:max_commits]


def compute_commit_digest(commit_obj: dict[str, Any]) -> str:
    commit_data = commit_obj.get("commit") or {}
    author = commit_data.get("author") or {}
    committer = commit_data.get("committer") or {}
    tree = commit_data.get("tree") or {}
    parents = [p.get("sha") for p in commit_obj.get("parents") or [] if isinstance(p, dict) and p.get("sha")]

    canonical_payload = {
        "author": {
            "date": author.get("date"),
            "email": author.get("email"),
            "name": author.get("name"),
        },
        "committer": {
            "date": committer.get("date"),
            "email": committer.get("email"),
            "name": committer.get("name"),
        },
        "message": commit_data.get("message", ""),
        "parents": sorted(parents),
        "sha": commit_obj.get("sha", ""),
        "tree_sha": tree.get("sha", ""),
    }
    canonical_bytes = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical_bytes).hexdigest()}"


def import_github_history(manifest: dict[str, Any], repository: str,
                          requirement_id: str | None, artifact_kind: str,
                          relation: str, max_commits: int,
                          branch: str | None = None, token: str | None = None,
                          prefix: str | None = None) -> tuple[dict[str, Any], int, int]:
    """Retrieve commits from a public or authenticated GitHub repository with cryptographic provenance.

    Limits and guarantees:
    - Verifies repository history provenance (URL, commit SHA, retrieval time, content digest).
    - Does NOT verify requirements, test outcomes, build integrity, or the truth of engineering claims.
    - Does NOT infer unevidenced requirement links: links are created only if --requirement-id is provided
      or if commit messages explicitly mention known REQ-... identifiers.
    """
    if max_commits < 1:
        raise DatasetError("--max-commits must be at least 1")
    if not artifact_kind.strip() or not relation.strip():
        raise DatasetError("--kind and --relation must be non-empty")

    owner, repo, repo_url = parse_github_repo(repository)
    commits_data = fetch_github_commits(owner, repo, max_commits=max_commits, branch=branch, token=token)

    requirements = {item["id"] for item in manifest["artifacts"] if item["kind"] == "requirement"}
    if requirement_id and requirement_id not in requirements:
        raise DatasetError(f"requirement {requirement_id} is not in the manifest")

    retrieval_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if prefix is None or not prefix.strip():
        if artifact_kind == "rtl_change":
            id_prefix = "RTL"
        elif artifact_kind == "source_change":
            id_prefix = "SRC"
        elif artifact_kind == "work_item":
            id_prefix = "WORK"
        else:
            id_prefix = artifact_kind.upper().replace("_", "-")
    else:
        id_prefix = prefix.strip().rstrip("-")

    artifacts: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    for item in commits_data:
        sha = item.get("sha")
        if not sha:
            continue
        short_sha = sha[:7]
        commit_data = item.get("commit") or {}
        message = commit_data.get("message") or ""
        subject = message.splitlines()[0].strip() if message else short_sha
        body = "\n".join(message.splitlines()[1:]).strip() if "\n" in message else ""
        author = commit_data.get("author") or {}
        committer = commit_data.get("committer") or {}
        tree = commit_data.get("tree") or {}
        parents = [p.get("sha") for p in item.get("parents") or [] if isinstance(p, dict) and p.get("sha")]
        verification = commit_data.get("verification") or {}
        html_url = item.get("html_url") or f"{repo_url}/commit/{sha}"

        digest = compute_commit_digest(item)
        artifact_id = f"{id_prefix}-{short_sha}"

        author_name = author.get("name")
        author_email = author.get("email")
        author_str = f"{author_name} <{author_email}>" if author_name and author_email else (author_name or author_email or "Unknown")

        committer_name = committer.get("name")
        committer_email = committer.get("email")
        committer_str = f"{committer_name} <{committer_email}>" if committer_name and committer_email else (committer_name or committer_email or "Unknown")

        artifact: dict[str, Any] = {
            "id": artifact_id,
            "kind": artifact_kind,
            "title": subject,
            "commit": sha,
            "source": "github_history",
            "repository_url": repo_url,
            "retrieval_time": retrieval_time,
            "content_digest": digest,
            "provenance": {
                "repository_url": repo_url,
                "commit_sha": sha,
                "retrieval_time": retrieval_time,
                "content_digest": digest,
                "author": author_str,
                "author_name": author.get("name"),
                "author_email": author.get("email"),
                "authored_date": author.get("date"),
                "committer": committer_str,
                "committer_name": committer.get("name"),
                "committer_email": committer.get("email"),
                "committed_date": committer.get("date"),
                "message": message,
                "tree_sha": tree.get("sha"),
                "parents": sorted(parents),
                "html_url": html_url,
                "verified": verification.get("verified", False),
            },
        }
        artifacts.append(artifact)

        # Explicit linking only: user requirement-id or explicit REQ-... tags in commit message
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


def import_csv_evidence_export(manifest: dict[str, Any], path: Path, kind: str,
                               id_column: str, source_id_column: str,
                               relation: str | None, field_map_path: Path | None
                               ) -> tuple[dict[str, Any], int, int]:
    if kind not in EVIDENCE_LINKS:
        raise DatasetError(f"unsupported evidence kind {kind!r}")
    relation = relation or EVIDENCE_LINKS[kind][1]
    expected_relation = EVIDENCE_LINKS[kind][1]
    if relation != expected_relation:
        raise DatasetError(f"{kind} records must use relation {expected_relation}")
    field_map: dict[str, str] = {}
    if field_map_path:
        raw_mapping = load_json(field_map_path)
        if not isinstance(raw_mapping, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_mapping.items()
        ):
            raise DatasetError("CSV field map must be a JSON object of artifact-field to CSV-column strings")
        field_map = raw_mapping
    reserved = {"id", "kind", "source_id", "relation"}
    if reserved & field_map.keys():
        raise DatasetError("CSV field map cannot override id, kind, source_id, or relation")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            headers = reader.fieldnames or []
            required_columns = {id_column, source_id_column, *field_map.values()}
            missing = sorted(required_columns - set(headers))
            if missing:
                raise DatasetError("CSV is missing columns: " + ", ".join(missing))
            records = []
            for row_number, row in enumerate(reader, start=2):
                artifact_id = (row.get(id_column) or "").strip()
                source_id = (row.get(source_id_column) or "").strip()
                if not artifact_id or not source_id:
                    raise DatasetError(f"CSV row {row_number} has an empty artifact ID or source ID")
                record: dict[str, Any] = {
                    "id": artifact_id,
                    "kind": kind,
                    "source_id": source_id,
                    "relation": relation,
                }
                for artifact_field, column in field_map.items():
                    value = row.get(column)
                    if value is not None and value.strip():
                        record[artifact_field] = value.strip()
                records.append(record)
    except OSError as exc:
        raise DatasetError(f"cannot read CSV export {path}: {exc}") from exc
    except UnicodeError as exc:
        raise DatasetError(f"CSV export {path} must be UTF-8 encoded: {exc}") from exc
    return import_evidence_export(manifest, {"records": records})


def write_dataset(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def init_manifest(release_name: str, output_path: Path, notes: str | None = None) -> dict[str, Any]:
    if not release_name or not release_name.strip():
        raise DatasetError("release name must be a non-empty string")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "release": release_name.strip(),
        "artifacts": [],
        "edges": [],
    }
    if notes and notes.strip():
        manifest["notes"] = notes.strip()
    write_dataset(output_path, manifest)
    return manifest


def inspect_dataset(manifest: dict[str, Any]) -> dict[str, Any]:
    artifacts = manifest.get("artifacts", [])
    edges = manifest.get("edges", [])
    by_kind: dict[str, int] = defaultdict(int)
    by_source: dict[str, int] = defaultdict(int)
    provenance_count = 0
    with_commit = 0
    for a in artifacts:
        by_kind[a.get("kind", "unknown")] += 1
        by_source[a.get("source", "unspecified")] += 1
        if a.get("commit"):
            with_commit += 1
        if "provenance" in a or "content_digest" in a:
            provenance_count += 1
    by_relation: dict[str, int] = defaultdict(int)
    for e in edges:
        by_relation[e.get("type", "unknown")] += 1
    return {
        "release": manifest.get("release", "unnamed"),
        "schema_version": manifest.get("schema_version", 1),
        "fictional": bool(manifest.get("fictional", False)),
        "notes": manifest.get("notes"),
        "total_artifacts": len(artifacts),
        "artifacts_by_kind": dict(sorted(by_kind.items())),
        "artifacts_by_source": dict(sorted(by_source.items())),
        "artifacts_with_commits": with_commit,
        "provenance_tracked_artifacts": provenance_count,
        "total_edges": len(edges),
        "edges_by_relation": dict(sorted(by_relation.items())),
    }


def render_inspect(summary: dict[str, Any]) -> str:
    lines = [
        f"Manifest inspection: {summary['release']}",
        f"Schema version: {summary['schema_version']}",
        f"Sample classification: {'Fictional demonstration sample' if summary['fictional'] else 'Real / production manifest'}",
    ]
    if summary.get("notes"):
        lines.append(f"Notes: {summary['notes']}")
    lines.append(f"Total artifacts: {summary['total_artifacts']}")
    for kind, count in summary["artifacts_by_kind"].items():
        lines.append(f"  - {kind}: {count}")
    source_strs = [f"{src} ({cnt})" for src, cnt in summary["artifacts_by_source"].items()]
    lines.append(f"Sources: {', '.join(source_strs) if source_strs else 'None'}")
    lines.append(f"Artifacts with commit SHAs: {summary['artifacts_with_commits']}")
    lines.append(f"Artifacts with cryptographic provenance: {summary['provenance_tracked_artifacts']}")
    lines.append(f"Total links: {summary['total_edges']}")
    for rel, count in summary["edges_by_relation"].items():
        lines.append(f"  - {rel}: {count}")
    return "\n".join(lines)


def verify_manifest_digests(manifest: dict[str, Any]) -> tuple[int, int, list[dict[str, Any]]]:
    """Verify cryptographic content digests for all artifacts in a manifest."""
    artifacts = manifest.get("artifacts", [])
    total_digests = 0
    verified = 0
    mismatches: list[dict[str, Any]] = []

    for artifact in artifacts:
        digest = artifact.get("content_digest")
        if not digest:
            continue
        total_digests += 1
        source = artifact.get("source")
        prov = artifact.get("provenance") or {}

        if source == "github_history":
            if artifact.get("commit") and artifact.get("commit") != prov.get("commit_sha"):
                mismatches.append({
                    "id": artifact.get("id"),
                    "recorded": digest,
                    "reason": "Top-level commit SHA does not match provenance commit_sha",
                })
                continue
            prov_subject = (prov.get("message") or "").splitlines()[0].strip() if prov.get("message") else ""
            if prov_subject and artifact.get("title") and artifact.get("title").strip() != prov_subject:
                mismatches.append({
                    "id": artifact.get("id"),
                    "recorded": digest,
                    "reason": f"Artifact title was modified (expected {prov_subject!r}, found {artifact.get('title')!r})",
                })
                continue

            reconstructed_commit = {
                "sha": prov.get("commit_sha") or artifact.get("commit", ""),
                "commit": {
                    "message": prov.get("message") or artifact.get("title", ""),
                    "author": {
                        "name": prov.get("author_name"),
                        "email": prov.get("author_email"),
                        "date": prov.get("authored_date"),
                    },
                    "committer": {
                        "name": prov.get("committer_name"),
                        "email": prov.get("committer_email"),
                        "date": prov.get("committed_date"),
                    },
                    "tree": {"sha": prov.get("tree_sha", "")},
                },
                "parents": [{"sha": p} if isinstance(p, str) else p for p in prov.get("parents", [])],
            }
            expected = compute_commit_digest(reconstructed_commit)
            if digest == expected and digest == prov.get("content_digest"):
                verified += 1
            else:
                mismatches.append({
                    "id": artifact.get("id"),
                    "recorded": digest,
                    "expected": expected,
                    "reason": "Cryptographic digest mismatch with provenance payload",
                })
        elif source == "git_history":
            commit = prov.get("commit_sha") or artifact.get("commit", "")
            subject = prov.get("subject", artifact.get("title", ""))
            body = prov.get("body", "")
            payload = {"commit": commit, "subject": subject.strip(), "body": body.strip()}
            expected = f"sha256:{hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()}"
            if digest == expected and digest == prov.get("content_digest"):
                verified += 1
            else:
                mismatches.append({
                    "id": artifact.get("id"),
                    "recorded": digest,
                    "expected": expected,
                    "reason": "Digest mismatch with local git commit payload",
                })
        else:
            if digest.startswith("sha256:") and len(digest) == 71:
                verified += 1
            else:
                mismatches.append({
                    "id": artifact.get("id"),
                    "recorded": digest,
                    "reason": "Invalid digest format",
                })

    return total_digests, verified, mismatches


def launch_viewer(manifest_path: Path | None = None, port: int = 0, no_browser: bool = False) -> int:
    viewer_path = ROOT / "viewer.html"
    if not viewer_path.exists():
        raise DatasetError(f"viewer file not found at {viewer_path}")

    if port > 0:
        import functools
        import http.server
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        url = f"http://127.0.0.1:{port}/viewer.html"
        print(f"Serving ASIC TraceGate impact explorer at {url}")
        print("Press Ctrl+C to stop.")
        if not no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped server.")
        return 0
    else:
        url = viewer_path.as_uri()
        print(f"Opening impact explorer in browser: {url}")
        if not no_browser:
            webbrowser.open(url)
        return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracegate",
        description="Check release traceability, verify cryptographic supply-chain provenance, and import engineering artifacts.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_cmd = commands.add_parser("init", help="create a new empty release manifest for real project data")
    init_cmd.add_argument("--release", required=True, help="name of the release (e.g. v1.0.0)")
    init_cmd.add_argument("--output", type=Path, default=Path("release.json"), help="manifest output path (default: release.json)")
    init_cmd.add_argument("--notes", help="optional release description or provenance notes")

    inspect_cmd = commands.add_parser("inspect", help="summarize manifest contents, artifact types, and provenance")
    inspect_cmd.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    inspect_cmd.add_argument("--format", choices=("text", "json"), default="text")

    verify_cmd = commands.add_parser("verify-digests", help="verify cryptographic content digests across manifest artifacts")
    verify_cmd.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)

    view_cmd = commands.add_parser("view", help="open the interactive impact graph explorer in your web browser")
    view_cmd.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST, help="manifest file to view")
    view_cmd.add_argument("--port", type=int, default=0, help="serve on a local HTTP port (e.g. 8080)")
    view_cmd.add_argument("--no-browser", action="store_true", help="do not automatically launch a browser")

    check = commands.add_parser("check", help="evaluate a release manifest")
    check.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    check.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    check.add_argument("--format", choices=("text", "json", "html"), default="text")
    check.add_argument("--output", type=Path, help="write the report to this file instead of standard output")
    check.add_argument("--severity", choices=("all", "block", "warn"), default="all",
                       help="show all findings, only blockers, or only warnings")
    ci_status = check.add_mutually_exclusive_group()
    ci_status.add_argument("--fail-on-block", action="store_true", help="exit with status 1 when the gate is BLOCK")
    ci_status.add_argument("--fail-on-warn", action="store_true", help="exit with status 1 for WARN or BLOCK")

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

    github = commands.add_parser(
        "import-github", help="retrieve GitHub repository history with provenance"
    )
    github.add_argument("repository", help="GitHub repository URL (https://github.com/owner/repo) or owner/repo")
    github.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="base manifest to merge into")
    github.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="path to write merged manifest")
    github.add_argument("--requirement-id", help="link imported commits to this requirement ID")
    github.add_argument("--kind", default="source_change", help="artifact kind (default: source_change)")
    github.add_argument("--relation", default="implemented_by", help="relationship type when linked to a requirement")
    github.add_argument("--prefix", help="custom artifact ID prefix (default: SRC, RTL, or derived from kind)")
    github.add_argument("--branch", help="branch or commit ref to retrieve (default: repository default branch)")
    github.add_argument("--max-commits", type=int, default=100, help="maximum commits to retrieve (default: 100)")
    github.add_argument("--token", help="GitHub Personal Access Token (or set GITHUB_TOKEN environment variable)")

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

    csv_evidence = commands.add_parser(
        "import-csv-evidence", help="import flat CSV exports for documents, models, verification, or builds"
    )
    csv_evidence.add_argument("export", type=Path)
    csv_evidence.add_argument("--kind", required=True, choices=tuple(EVIDENCE_LINKS))
    csv_evidence.add_argument("--id-column", default="id")
    csv_evidence.add_argument("--source-id-column", default="source_id")
    csv_evidence.add_argument("--relation", help="defaults to the standard relation for --kind")
    csv_evidence.add_argument("--field-map", type=Path, help="JSON map of artifact fields to CSV column names")
    csv_evidence.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    csv_evidence.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    known_commands = {
        "check", "init", "inspect", "verify-digests", "view",
        "import-jira", "import-git", "import-github", "import-artifacts",
        "import-evidence", "import-csv-evidence", "-h", "--help",
    }
    if not args_in:
        args_in = ["check"]
    elif args_in[0] not in known_commands:
        args_in.insert(0, "check")
    args = make_parser().parse_args(args_in)
    try:
        if args.command == "init":
            init_manifest(args.release, args.output, args.notes)
            print(f"Initialized new release manifest '{args.release}' at {args.output}")
            return 0

        if args.command == "inspect":
            manifest = load_dataset(args.manifest)
            summary = inspect_dataset(manifest)
            if args.format == "json":
                print(json.dumps(summary, indent=2, ensure_ascii=False))
            else:
                print(render_inspect(summary))
            return 0

        if args.command == "verify-digests":
            manifest = load_dataset(args.manifest)
            total, verified, mismatches = verify_manifest_digests(manifest)
            if total == 0:
                print("No cryptographic digests found in manifest.")
                return 0
            if mismatches:
                print(f"Verification failed: {len(mismatches)} of {total} digests mismatched or tampered!")
                for m in mismatches:
                    print(f"- Artifact {m['id']}: {m['reason']} (recorded: {m['recorded']})")
                return 1
            print(f"Verified {verified}/{total} cryptographic content digests: all valid (0 tampered).")
            return 0

        if args.command == "view":
            return launch_viewer(args.manifest, args.port, args.no_browser)

        if args.command == "check":
            report = analyze(load_dataset(args.manifest), load_rules(args.rules))
            all_findings = report["findings"]
            if args.severity != "all":
                selected = "BLOCK" if args.severity == "block" else "WARN"
                report["findings"] = [item for item in all_findings if item["severity"] == selected]
                report["findings_hidden"] = len(all_findings) - len(report["findings"])
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
            if args.fail_on_warn and report["gate"] in {"WARN", "BLOCK"}:
                return 1
            return 1 if args.fail_on_block and report["gate"] == "BLOCK" else 0

        manifest = load_dataset(args.manifest)
        if args.command == "import-jira":
            merged, artifact_count, edge_count = import_jira_export(manifest, load_json(args.export))
        elif args.command == "import-git":
            merged, artifact_count, edge_count = import_git_history(
                manifest, args.repository, args.requirement_id, args.kind, args.relation, args.max_commits
            )
        elif args.command == "import-github":
            merged, artifact_count, edge_count = import_github_history(
                manifest, args.repository, args.requirement_id, args.kind, args.relation,
                args.max_commits, args.branch, args.token, args.prefix
            )
            write_dataset(args.output, merged)
            print(f"Imported or merged {artifact_count} artifacts and {edge_count} links with provenance into {args.output}")
            if edge_count == 0 and not args.requirement_id:
                print("Note: GitHub history records change provenance but does not verify requirements, tests, or builds. No requirement links were inferred without explicit identifiers.")
            return 0
        elif args.command == "import-evidence":
            merged, artifact_count, edge_count = import_evidence_export(manifest, load_json(args.export))
        elif args.command == "import-csv-evidence":
            merged, artifact_count, edge_count = import_csv_evidence_export(
                manifest, args.export, args.kind, args.id_column, args.source_id_column,
                args.relation, args.field_map
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
    except OSError as exc:
        print(f"File error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
