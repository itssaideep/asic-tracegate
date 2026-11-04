# ASIC TraceGate

ASIC TraceGate is a standalone command-line tool for checking whether an engineering release has the links and evidence it needs. It reads a JSON release manifest and checks traceability across requirements, work items, RTL changes, design documents, reference models, verification runs, FPGA builds, and release baselines. Release checks are configurable in `rules.json`.

The tool builds an auditable artifact graph and applies deterministic release rules. It reports missing links, outdated documents, verification or builds tied to an older RTL revision, incomplete baselines, and claims without current supporting evidence. Input formats are JSON and the command-line tool uses only the Python standard library.

## Run the example

Requires Python 3.10 or newer. No third-party packages are required.

```powershell
python tracegate.py
```

Pass another JSON dataset with:

```powershell
python tracegate.py path\to\release.json
```

To use a different rule set:

```powershell
python tracegate.py check path\to\release.json --rules path\to\rules.json
```

The command prints a release quality report with a PASS, WARN, or BLOCK decision, traceability coverage, evidence completeness, unsupported-claim rate, findings, and change-impact paths. Export it as JSON or standalone HTML:

```powershell
python tracegate.py check --format json --output output\report.json
python tracegate.py check --format html --output output\report.html
```

Use `--fail-on-block` to return exit code `1` when the gate is BLOCK, for example in a local script or CI job.

## What the example demonstrates

- One synthetic feature requirement linked to a work item and implementation artifacts.
- A stale design specification and verification record tied to an older RTL commit.
- An FPGA build for the current RTL revision.
- One claim supported by current reference-model evidence and one unsupported claim.
- A release baseline that omits one linked item.
- A fictional Jira export that can be imported into the manifest.

The example is deliberately imperfect so you can see the checks working. All artifact names and data are fictional.

## Design

- `schema_version`: manifest format version; the current format is version `1`.
- `artifacts`: typed nodes with revisions and evidence metadata. Each needs a unique `id` and a `kind`; teams can add their own fields.
- `edges`: explicit, directional relationships between artifacts.
- `tracegate.py`: validates the manifest, traverses the graph, applies release rules, and prints the report.
- `rules.json`: required artifact links, revision checks, evidence checks, baseline coverage, and which finding codes block a release.
- `schemas/`: JSON Schema files for manifests, rules, and normalized evidence exports.
- `viewer.html`: a local browser interface for browsing the downstream artifact graph of each requirement.

The built-in loader checks required fields, basic types, duplicate artifact IDs, and dangling edge references. It does not install or require a JSON Schema package. Keep `schema_version` at `1` when adding fields; make a deliberate format change before changing its value.

## Adapt the release rules

Before applying TraceGate to another team's workflow, map the team's artifact names and link directions to `rules.json`. For example, change `requirement_kind`, `required_relations`, revision-field names, evidence status values, and baseline artifact kinds to match the team's exports. Add a relation check only when that relationship is meaningful in the target workflow.

`blocking_findings` controls the release gate. Codes in this list produce `BLOCK`; findings outside it produce `WARN`; no findings produce `PASS`. The sample makes every configured finding blocking. Set the list to the codes that must stop a release, and review the effect with representative manifests before adopting it.

Manifests should use stable unique artifact IDs and explicit edges. Claims list supporting artifact IDs in `evidence_ids`; evidence is considered current only when it passes the configured revision/status checks. A missing or outdated item may therefore affect both the gate and the evidence-supported claim rate.

## Import local exports

Merge a Jira JSON export into a copy of the sample manifest. The importer reads the Jira export's issue key, summary, status, and requirement IDs (or `REQ-...` labels); it does not contact Jira:

```powershell
python tracegate.py import-jira data\jira_export.json --output data\release_with_jira.json
```

Import local Git commits. Commit messages or bodies containing a requirement ID such as `REQ-104` create requirement-to-change links:

```powershell
python tracegate.py import-git path\to\local\repository --output data\release_with_git.json
```

If the commit messages do not include requirement IDs, provide the link explicitly:

```powershell
python tracegate.py import-git path\to\local\repository --requirement-id REQ-104 --output data\release_with_git.json
```

Normalized exports from other tools can be merged when they contain `artifacts` and `edges` arrays; they do not need a release name:

```powershell
python tracegate.py import-artifacts path\to\evidence_export.json --output data\release_with_evidence.json
```

For documentation, reference-model, verification, or FPGA build exports, use the normalized record adapter:

```powershell
python tracegate.py import-evidence data\evidence_export.json --output data\release_with_evidence.json
```

Each record needs `id`, `kind`, `source_id`, and `relation`. Supported link pairs are `design_spec` from a `requirement` via `documented_by`, `reference_model` from a `requirement` via `modeled_by`, `verification` from an `rtl_change` via `verified_by`, and `fpga_build` from an `rtl_change` via `built_as`. Other fields on a record, such as `status`, `rtl_commit`, and `requirement_revision`, are preserved. Adapt source exports into this format and check `schemas/evidence-export.schema.json`.

Open `viewer.html` in a browser and load a manifest such as `data/release_candidate.json`. Select a requirement to see all downstream artifacts and links. The viewer reads the chosen file locally in the browser.

## Current limitations

Jira import currently reads exported JSON files; Git import reads local history through the installed Git command. The tool does not connect directly to Jira, source-control hosting, RTL tools, documentation platforms, FPGA build systems, or an LLM. It does not authenticate, fetch data, or verify that imported records are genuine. The sample manifest and Jira export are fictional. Review and adapt the schema, field mappings, relationship rules, status values, revision semantics, and blocking findings before using it in another team's workflow.

## Scope

The report exporter, normalized evidence adapter, and local graph viewer are included. Vendor-specific APIs and export formats still need explicit field mapping; TraceGate does not fetch or authenticate to those systems.
