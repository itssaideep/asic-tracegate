#!/usr/bin/env python3
"""Deterministic traceability and release-quality checks for a synthetic ASIC dataset."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


REQUIRED_RELATIONS = {
    "tracked_by": "work_item",
    "implemented_by": "rtl_change",
    "documented_by": "design_spec",
    "modeled_by": "reference_model",
}
ACCEPTABLE_CLAIM_EVIDENCE = {
    "design_spec",
    "reference_model",
    "verification",
    "fpga_build",
}


class DatasetError(ValueError):
    pass


def load_dataset(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot load {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DatasetError("dataset root must be a JSON object")
    if not isinstance(data.get("artifacts"), list) or not isinstance(data.get("edges"), list):
        raise DatasetError("dataset must contain artifact and edge arrays")
    return data


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
        if not all(edge.get(key) for key in ("from", "to", "type")):
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
            target = edge["to"]
            if target not in visited:
                visited.add(target)
                stack.append(target)
    visited.discard(start)
    return sorted(visited)


def analyze(data: dict[str, Any]) -> dict[str, Any]:
    artifacts, adjacency = index_dataset(data)
    findings: list[dict[str, str]] = []
    trace_total = 0
    trace_present = 0
    evidence_total = 0
    evidence_complete = 0
    valid_evidence_ids: set[str] = set()
    requirements = [a for a in data["artifacts"] if a["kind"] == "requirement"]
    impact: dict[str, list[str]] = {}

    def add(code: str, artifact_id: str, message: str) -> None:
        findings.append({"code": code, "artifact_id": artifact_id, "message": message})

    for req in requirements:
        req_id = req["id"]
        linked_by_relation: dict[str, list[str]] = {}
        for relation, kind in REQUIRED_RELATIONS.items():
            linked = [aid for aid in targets(adjacency, req_id, relation) if artifacts[aid]["kind"] == kind]
            linked_by_relation[relation] = linked
            trace_total += 1
            if linked:
                trace_present += 1
            else:
                add("TRACE_LINK_MISSING", req_id, f"Requirement has no {kind} linked by {relation}.")

        rtl_ids = linked_by_relation["implemented_by"]
        related_ids = set(graph_reachable(adjacency, req_id)) | {req_id}
        impacted_baselines: set[str] = set()
        for baseline in (a for a in data["artifacts"] if a["kind"] == "release_baseline"):
            included = set(targets(adjacency, baseline["id"], "includes"))
            expected_in_baseline = related_ids & {
                aid for aid, artifact in artifacts.items()
                if artifact["kind"] in {
                    "requirement", "work_item", "rtl_change", "design_spec",
                    "reference_model", "verification", "fpga_build"
                }
            }
            missing_from_baseline = expected_in_baseline - included
            if missing_from_baseline:
                add(
                    "BASELINE_ARTIFACT_MISSING",
                    baseline["id"],
                    "Release baseline omits linked artifacts: " + ", ".join(sorted(missing_from_baseline)) + ".",
                )
            if included & related_ids:
                impacted_baselines.add(baseline["id"])
        impact[req_id] = sorted((related_ids - {req_id}) | impacted_baselines)

        evidence_total += 4
        docs = [artifacts[aid] for aid in linked_by_relation["documented_by"]]
        docs_current = [doc for doc in docs if doc.get("requirement_revision") == req.get("revision")]
        if docs_current:
            evidence_complete += 1
            valid_evidence_ids.update(doc["id"] for doc in docs_current)
        else:
            add("DOC_REVISION_STALE", req_id, "No linked design specification matches the current requirement revision.")

        models = [artifacts[aid] for aid in linked_by_relation["modeled_by"]]
        models_current = [model for model in models if model.get("requirement_revision") == req.get("revision")]
        if models_current:
            evidence_complete += 1
            valid_evidence_ids.update(model["id"] for model in models_current)
        else:
            add("MODEL_REVISION_STALE", req_id, "No linked reference model matches the current requirement revision.")

        valid_verification = []
        verification_mismatches = []
        valid_builds = []
        for rtl_id in rtl_ids:
            rtl = artifacts[rtl_id]
            for verification_id in targets(adjacency, rtl_id, "verified_by"):
                verification = artifacts[verification_id]
                if verification["kind"] == "verification" and verification.get("status") == "PASS" and verification.get("rtl_commit") == rtl.get("commit"):
                    valid_verification.append(verification_id)
                    valid_evidence_ids.add(verification_id)
                elif verification["kind"] == "verification":
                    verification_mismatches.append(verification_id)
                    add("VERIFICATION_RTL_MISMATCH", verification_id, f"Verification evidence does not match current RTL commit for {rtl_id}.")
            for build_id in targets(adjacency, rtl_id, "built_as"):
                build = artifacts[build_id]
                if build["kind"] == "fpga_build" and build.get("status") == "PASS" and build.get("rtl_commit") == rtl.get("commit"):
                    valid_builds.append(build_id)
                    valid_evidence_ids.add(build_id)
                elif build["kind"] == "fpga_build":
                    add("FPGA_BUILD_RTL_MISMATCH", build_id, f"FPGA build does not match current RTL commit for {rtl_id}.")

        if valid_verification:
            evidence_complete += 1
        else:
            if not verification_mismatches:
                add("VERIFICATION_MISSING", req_id, "No passing verification evidence matches the current RTL commit.")
        if valid_builds:
            evidence_complete += 1
        else:
            add("FPGA_BUILD_MISSING", req_id, "No passing FPGA build matches the current RTL commit.")

    claims = [a for a in data["artifacts"] if a["kind"] == "claim"]
    unsupported_claims: list[str] = []
    for claim in claims:
        support_ids = claim.get("evidence_ids", [])
        supported = bool(support_ids) and all(
            evidence_id in artifacts
            and artifacts[evidence_id]["kind"] in ACCEPTABLE_CLAIM_EVIDENCE
            and evidence_id in valid_evidence_ids
            for evidence_id in support_ids
        )
        if not supported:
            unsupported_claims.append(claim["id"])
            add("CLAIM_UNSUPPORTED", claim["id"], "Claim has no linked current evidence of an accepted type.")

    return {
        "release": data.get("release", "unnamed-release"),
        "requirements": [r["id"] for r in requirements],
        "traceability_coverage": trace_present / trace_total if trace_total else 1.0,
        "traceability_links_present": trace_present,
        "traceability_links_expected": trace_total,
        "evidence_completeness": evidence_complete / evidence_total if evidence_total else 1.0,
        "evidence_items_current": evidence_complete,
        "evidence_items_expected": evidence_total,
        "unsupported_claim_rate": len(unsupported_claims) / len(claims) if claims else 0.0,
        "unsupported_claims": unsupported_claims,
        "findings": findings,
        "change_impact": impact,
        "gate": "BLOCK" if findings else "PASS",
    }


def render_report(report: dict[str, Any]) -> str:
    lines = [
        f"ASIC TraceGate report: {report['release']}",
        f"Release gate: {report['gate']}",
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


def main() -> int:
    default_data = Path(__file__).parent / "data" / "release_candidate.json"
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_data
    try:
        report = analyze(load_dataset(input_path))
    except DatasetError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
