# ASIC TraceGate

ASIC TraceGate is a small, dependency-free research prototype for checking release traceability across synthetic ASIC feature artifacts. It models the path from requirements through work items, RTL changes, design documentation, reference models, verification evidence, FPGA builds, and release baselines.

The prototype combines a deterministic rule engine with an auditable artifact graph. It reports missing links, stale documents, verification/build evidence tied to an older RTL revision, and claims without acceptable supporting evidence. A synthetic gold-label set is included to calculate warning precision and recall for the example scenario.

## Run the example

Requires Python 3.10 or newer. No third-party packages are required.

```powershell
python tracegate.py
```

Pass another JSON dataset with:

```powershell
python tracegate.py path\to\release.json
```

The command prints a release quality report, including traceability coverage, evidence completeness, unsupported-claim rate, warning precision/recall, and change-impact paths.

## What the example demonstrates

- One synthetic feature requirement linked to a work item and implementation artifacts.
- A stale design specification and verification record tied to an older RTL commit.
- An FPGA build for the current RTL revision.
- One evidence-supported claim and one unsupported claim.
- Expected warning labels for evaluating the deterministic checks.

The fixture is intentionally imperfect so the report demonstrates how a release gate surfaces issues. It is fictional and contains no company data.

## Design

- `artifacts`: typed nodes with revisions and evidence metadata.
- `edges`: explicit, directional relationships between artifacts.
- `tracegate.py`: schema checks, graph traversal, deterministic quality rules, and report generation.
- `expected_warning_codes`: fixture-only reference labels used to calculate warning precision and recall.

The rule engine does not ask an LLM to decide whether a release passes. An LLM could later draft evidence-linked explanations, while deterministic rules retain control of the gate. This separation keeps each finding auditable and makes unsupported generated claims measurable.

## Current limitations

This is a local prototype using one hand-authored JSON fixture. It does not connect to Jira, a source-control system, RTL tools, documentation platforms, an FPGA build system, or an LLM. It does not establish production readiness or validate real ASIC workflows. The schema and rules would need to be reviewed with domain engineers before using representative data.

## Possible research extensions

1. Add a dataset loader for sanitized Jira and Git history exports.
2. Define version-aware schemas for requirements, RTL revisions, design documents, reference models, verification runs, and builds.
3. Add controlled missing-link and stale-evidence cases, then measure warning precision, recall, and evidence completeness.
4. Add an optional LLM explanation stage that can only cite artifact IDs and rule findings; measure unsupported-claim rate.
5. Run a small practitioner review of report usefulness and warning severity.
