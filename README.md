# ASIC TraceGate

ASIC TraceGate is a dependency-free command-line tool for checking release traceability and evidence. It reads a JSON manifest, builds an artifact graph, applies configurable rules, and reports missing links, stale evidence, unsupported claims, release-baseline gaps, and downstream change impact.

It works with local files and local Git history. It does not connect to Jira, source-control hosting, RTL or FPGA tools, documentation systems, or an LLM.

## Quick start

Requires Python 3.10 or newer. No third-party packages are needed.

```powershell
python tracegate.py
```

The sample intentionally reports `BLOCK`: it contains a stale design specification, stale verification, an incomplete baseline, and one unsupported claim. This gives the checks something to report.

Check a different manifest or rules file:

```powershell
python tracegate.py check path\to\release.json --rules path\to\rules.json
```

## Reports and release gates

The text report is the default. Write a machine-readable JSON report or a self-contained HTML report with:

```powershell
python tracegate.py check --format json --output output\report.json
python tracegate.py check --format html --output output\report.html
```

Findings are labeled `BLOCK` or `WARN` according to `blocking_findings` in the rules file. Filter the displayed findings without changing the overall gate or counts:

```powershell
python tracegate.py check --severity block
python tracegate.py check --severity warn
```

For scripts and CI, `--fail-on-block` returns exit code 1 for a blocked release. `--fail-on-warn` returns 1 for either a warning or a block. Without either option, the command writes the report and exits successfully unless it cannot read or process its inputs.

## Importing artifacts

All import commands merge local data into a copy of the sample manifest. Choose a new output path to preserve the original.

### Jira JSON export

The importer reads issue keys, summaries, statuses, and requirement IDs from `requirement_ids` or `REQ-...` labels. It does not call Jira.

```powershell
python tracegate.py import-jira data\jira_export.json --output data\release_with_jira.json
```

### Local Git history

Commit subjects or bodies containing a requirement ID such as `REQ-104` create requirement-to-change links. If they do not include IDs, link the imported commits to a requirement explicitly.

```powershell
python tracegate.py import-git path\to\local\repository --output data\release_with_git.json
python tracegate.py import-git path\to\local\repository --requirement-id REQ-104 --output data\release_with_git.json
```

Git history is read locally with the installed `git` command; no remote repository is fetched.

### Normalized JSON artifacts

Merge an export that already contains `artifacts` and `edges` arrays:

```powershell
python tracegate.py import-artifacts path\to\export.json --output data\release_with_artifacts.json
```

The export does not need a release name. Artifact IDs referenced by edges must exist in the merged manifest.

### Document, model, verification, and build evidence

Use the normalized evidence adapter for records with `id`, `kind`, `source_id`, and `relation` fields:

```powershell
python tracegate.py import-evidence data\evidence_export.json --output data\release_with_evidence.json
```

Supported links are:

- `design_spec` from a `requirement` using `documented_by`
- `reference_model` from a `requirement` using `modeled_by`
- `verification` from an `rtl_change` using `verified_by`
- `fpga_build` from an `rtl_change` using `built_as`

Other record fields, including `status`, `rtl_commit`, and `requirement_revision`, are kept. See [`schemas/evidence-export.schema.json`](schemas/evidence-export.schema.json) and the fictional example at [`data/evidence_export.json`](data/evidence_export.json).

### Flat CSV evidence

Map CSV headers to TraceGate artifact fields with a JSON field-map file. The evidence kind determines the valid source kind and relationship.

```powershell
python tracegate.py import-csv-evidence data\verification_export.csv --kind verification --id-column run_id --source-id-column rtl_id --field-map data\verification_field_map.json --output data\release_with_csv.json
```

The included map converts `rtl_revision`, `result`, and `summary` to `rtl_commit`, `status`, and `title`. The adapter also accepts `design_spec`, `reference_model`, and `fpga_build` kinds. It rejects missing ID/source columns and invalid kind-to-relationship combinations.

## Explore the impact graph

Open [`viewer.html`](viewer.html) in a browser, load a manifest such as [`data/release_candidate.json`](data/release_candidate.json), and select a requirement. The viewer shows its reachable artifacts and relationships; selecting a node displays its manifest fields. The manifest is read locally in the browser.

## Manifest and rule files

- `data/release_candidate.json` is the sample release manifest. Its `schema_version` is `1`; artifacts have unique IDs and kinds, and edges define directed links.
- `rules.json` defines required relationships, revision checks, evidence checks, baseline coverage, and which finding codes block release.
- `schemas/` contains JSON Schemas for manifests, rules, and normalized evidence records.

The built-in loader checks required fields, basic types, duplicate artifact IDs, and dangling edge references. It does not run a full JSON Schema validator or require a JSON Schema package.

Before using TraceGate with another team's data, map that team's artifact kinds, edge directions, field names, status values, and revision semantics into the rules and import mappings. `blocking_findings` codes produce `BLOCK`; other findings produce `WARN`; no findings produce `PASS`. If `blocking_findings` is omitted, all findings block by default.

## Limitations

The sample manifest and exports are fictional. Importers read local JSON, CSV, or Git history and do not authenticate, fetch data, or confirm that records are genuine. Vendor-specific export formats need field mapping before import. Review the schemas, rules, and mappings before applying the gate to another team's workflow.
