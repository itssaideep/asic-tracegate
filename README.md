# ASIC TraceGate

ASIC TraceGate is a standalone command-line tool for checking whether an engineering release has the links and evidence it needs. It reads a JSON release manifest and checks traceability across requirements, work items, RTL changes, design documents, reference models, verification runs, FPGA builds, and release baselines.

The tool builds an auditable artifact graph and applies deterministic release rules. It reports missing links, outdated documents, verification or builds tied to an older RTL revision, incomplete baselines, and claims without current supporting evidence.

## Run the example

Requires Python 3.10 or newer. No third-party packages are required.

```powershell
python tracegate.py
```

Pass another JSON dataset with:

```powershell
python tracegate.py path\to\release.json
```

The command prints a release quality report with a PASS or BLOCK decision, traceability coverage, evidence completeness, unsupported-claim rate, findings, and change-impact paths.

## What the example demonstrates

- One synthetic feature requirement linked to a work item and implementation artifacts.
- A stale design specification and verification record tied to an older RTL commit.
- An FPGA build for the current RTL revision.
- One evidence-supported claim and one unsupported claim.
- A release baseline that omits one linked item.

The example is deliberately imperfect so you can see the checks working. All artifact names and data are fictional.

## Design

- `artifacts`: typed nodes with revisions and evidence metadata.
- `edges`: explicit, directional relationships between artifacts.
- `tracegate.py`: validates the manifest, traverses the graph, applies release rules, and prints the report.

## Current limitations

This is a standalone JSON-based tool with one sample manifest. It does not connect directly to Jira, source control, RTL tools, documentation platforms, FPGA build systems, or an LLM. Review and adapt the manifest schema and release rules before using them in another team's workflow.

## Next improvements

1. Add importers for Jira exports and Git history.
2. Add a JSON or HTML report export.
3. Add configurable release rules and required artifact types.
4. Add a small browser interface for exploring the impact graph.
