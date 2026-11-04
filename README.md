# ASIC TraceGate

ASIC TraceGate is a dependency-free command-line tool for checking release traceability and evidence. It reads a JSON manifest, builds an artifact graph, applies configurable rules, and reports missing links, stale evidence, unsupported claims, release-baseline gaps, and downstream change impact.

It works with local files, local Git history, and public or authenticated GitHub repositories. It does not connect to Jira, documentation systems, or an LLM.

## Quick start

Requires Python 3.10 or newer. No third-party packages are needed.

Install locally as a standalone command-line tool:

```powershell
pip install .
```

Once installed, use `tracegate` directly (or run `python tracegate.py` without installation):

```powershell
tracegate check
```

The bundled sample intentionally reports `BLOCK`: it contains a stale design specification, stale verification, an incomplete baseline, and one unsupported claim. This gives the checks something to report.

Check a different manifest or rules file:

```powershell
tracegate check path\to\release.json --rules path\to\rules.json
```

## Manifest management and verification

### Initialize a new release manifest

Create a fresh, empty release manifest ready for real project data:

```powershell
tracegate init --release v1.0.0 --output release.json --notes "Production release candidate"
```

### Inspect manifest contents and provenance statistics

Inspect manifest artifacts, kinds, sources, and supply-chain provenance metrics:

```powershell
tracegate inspect data\real_github_manifest.json
tracegate inspect data\release_candidate.json --format json
```

### Verify cryptographic supply-chain digests

Check that all recorded SHA-256 content digests match their provenance payloads to detect tampering:

```powershell
tracegate verify-digests data\real_github_manifest.json
```

### Launch interactive impact graph viewer

Launch the browser-based impact explorer directly from the CLI:

```powershell
tracegate view
tracegate view --port 8080
```

## Reports and release gates

The text report is the default. Write a machine-readable JSON report or a self-contained HTML report with:

```powershell
tracegate check --format json --output output\report.json
tracegate check --format html --output output\report.html
```

Findings are labeled `BLOCK` or `WARN` according to `blocking_findings` in the rules file. Filter the displayed findings without changing the overall gate or counts:

```powershell
tracegate check --severity block
tracegate check --severity warn
```

For scripts and CI, `--fail-on-block` returns exit code 1 for a blocked release. `--fail-on-warn` returns 1 for either a warning or a block. Without either option, the command writes the report and exits successfully unless it cannot read or process its inputs.

## Importing artifacts

All import commands merge local data into a copy of the specified manifest. Choose a new output path to preserve the original.

### Jira JSON export

The importer reads issue keys, summaries, statuses, and requirement IDs from `requirement_ids` or `REQ-...` labels. It does not call Jira.

```powershell
tracegate import-jira data\jira_export.json --output data\release_with_jira.json
```

### Local Git history

Commit subjects or bodies containing a requirement ID such as `REQ-104` create requirement-to-change links. If they do not include IDs, link the imported commits to a requirement explicitly.

```powershell
tracegate import-git path\to\local\repository --output data\release_with_git.json
tracegate import-git path\to\local\repository --requirement-id REQ-104 --output data\release_with_git.json
```

Git history is read locally with the installed `git` command; each imported artifact records retrieval time and a cryptographic SHA-256 content digest.

### Public or authenticated GitHub repository history

Retrieve commit history directly from a remote GitHub repository without third-party dependencies:

```powershell
tracegate import-github https://github.com/itssaideep/asic-tracegate --output data\release_with_github.json
```

The importer accepts canonical URLs (`https://github.com/owner/repo`), `.git` endpoints, or short `owner/repo` identifiers. It records cryptographic provenance for every imported change:
- `repository_url`: The canonical repository URL (e.g. `https://github.com/itssaideep/asic-tracegate`).
- `commit`: The full 40-character commit SHA.
- `retrieval_time`: ISO 8601 UTC timestamp of when TraceGate retrieved the commit.
- `content_digest`: Deterministic SHA-256 digest (`sha256:...`) computed over the commit metadata (SHA, tree, author, committer, message, and parents).
- `provenance`: Detailed metadata object containing author, commit dates, tree SHA, parent commits, GitHub web link, and signature verification status.

Options include:
- `--requirement-id REQ-104`: Explicitly link all imported commits to a known requirement.
- `--kind source_change` (default) or `--kind rtl_change`: Artifact kind for the imported commits.
- `--prefix SRC`: Custom ID prefix for generated artifact IDs (e.g. `SRC-51f0eec` or `RTL-51f0eec`).
- `--branch main`: Specific branch or commit ref to retrieve (defaults to the repository's default branch).
- `--max-commits 50`: Limit the number of retrieved commits (default: 100).
- `--token <PAT>`: Personal Access Token to avoid GitHub API rate limits or access private repositories (can also be provided via the `GITHUB_TOKEN` or `GH_TOKEN` environment variable).

Link inference policy:
- If a commit message explicitly references a requirement pattern (such as `REQ-104`) that exists in the manifest, an edge is created automatically.
- If `--requirement-id` is provided, that explicit requirement is linked.
- Otherwise, imported commits remain standalone artifacts. TraceGate **does not infer unevidenced requirement links** or invent relationships not supported by the source.

### Normalized JSON artifacts

Merge an export that already contains `artifacts` and `edges` arrays:

```powershell
tracegate import-artifacts path\to\export.json --output data\release_with_artifacts.json
```

The export does not need a release name. Artifact IDs referenced by edges must exist in the merged manifest.

### Document, model, verification, and build evidence

Use the normalized evidence adapter for records with `id`, `kind`, `source_id`, and `relation` fields:

```powershell
tracegate import-evidence data\evidence_export.json --output data\release_with_evidence.json
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
tracegate import-csv-evidence data\verification_export.csv --kind verification --id-column run_id --source-id-column rtl_id --field-map data\verification_field_map.json --output data\release_with_csv.json
```

The included map converts `rtl_revision`, `result`, and `summary` to `rtl_commit`, `status`, and `title`. The adapter also accepts `design_spec`, `reference_model`, and `fpga_build` kinds. It rejects missing ID/source columns and invalid kind-to-relationship combinations.

## Explore the impact graph

Open [`viewer.html`](viewer.html) in a browser or launch it with `tracegate view`. The explorer features:
- Interactive SVG dependency graph visualization with search and highlight filtering.
- Visual badges distinguishing fictional samples from real production manifests.
- Cryptographic provenance inspector card with direct GitHub commit links, authors, timestamps, and verified signature badges.
- Preset selector to instantly compare [`data/release_candidate.json`](data/release_candidate.json) against [`data/real_github_manifest.json`](data/real_github_manifest.json).

## Manifest and rule files

- `data/release_candidate.json` is the bundled sample release manifest. It is explicitly marked `"fictional": true`; artifacts have unique IDs and kinds, and edges define directed links.
- `data/real_github_manifest.json` is a real manifest imported from `github.com/itssaideep/asic-tracegate` recording authentic commits and cryptographic SHA-256 digests.
- `rules.json` defines required relationships, revision checks, evidence checks, baseline coverage, and which finding codes block release.
- `schemas/` contains JSON Schemas for manifests, rules, and normalized evidence records.

The built-in loader checks required fields, basic types, duplicate artifact IDs, and dangling edge references. It does not run a full JSON Schema validator or require a third-party JSON Schema package.

Before using TraceGate with another team's data, map that team's artifact kinds, edge directions, field names, status values, and revision semantics into the rules and import mappings. `blocking_findings` codes produce `BLOCK`; other findings produce `WARN`; no findings produce `PASS`. If `blocking_findings` is omitted, all findings block by default.

## Data limits and engineering claims

When integrating real repository data, TraceGate enforces clear evidentiary boundaries:

1. **What Git/GitHub history verifies**:
   - The existence and timing of code commits.
   - Author, committer, and tree hashes.
   - Content integrity via SHA-256 cryptographic digests recorded at import time.

2. **What Git/GitHub history does NOT verify**:
   - Whether engineering requirements have been satisfied or correctly implemented.
   - Whether simulation, formal verification, or unit test regressions have passed.
   - Whether FPGA synthesis or ASIC place-and-route builds completed without timing or design rule violations.
   - The truth or validity of claims made in commit summaries or issue trackers.

3. **Strict link policy**:
   - TraceGate **never infers or fabricates links** between commits and requirements unless an explicit requirement tag (such as `REQ-104`) is present in the source commit message or specified via `--requirement-id`.
   - Passing tests (`verification`), specifications (`design_spec`), and bitstreams (`fpga_build`) must be supplied through authoritative evidence exports (such as CI/CD test runners or build servers) rather than assumed from repository activity.

## Bundled fictional samples vs. real source data

- **Bundled fictional samples**: The files in `data/` (`release_candidate.json`, `jira_export.json`, `evidence_export.json`, `verification_export.csv`) are synthetic test fixtures explicitly labeled `"fictional": true` and `"sample_label": "fictional-sample"`. They are intentionally structured to trigger and demonstrate all gate checks (a blocked release gate, stale specification revisions, mismatched RTL commits, baseline gaps, and unsupported claims).
- **Real source data**: For live evaluation, import actual repository commits from public repositories such as [`github.com/itssaideep/asic-tracegate`](https://github.com/itssaideep/asic-tracegate) using `tracegate import-github https://github.com/itssaideep/asic-tracegate`. Real commits record provenance (`repository_url`, `commit`, `retrieval_time`, `content_digest`) and preserve true repository history without fictional labels. An example of authentic repository provenance is provided at [`data/real_github_manifest.json`](data/real_github_manifest.json).

## Running tests

Run the complete automated test suite (24 unit and live integration tests):

```powershell
python -m unittest discover tests
```

## Setup and constraints

- **Environment**: Python 3.10+ (dependency-free; uses standard library `urllib`, `hashlib`, `json`, `csv`, `webbrowser`, and `subprocess`).
- **Packaging**: Standard PEP 621 package via `pyproject.toml`. Install with `pip install .` or `pip install -e .`.
- **GitHub API Rate Limits**: Unauthenticated GitHub API requests are limited by GitHub to 60 calls per hour per IP. For high-volume imports or CI pipelines, pass `--token <PAT>` or set the `GITHUB_TOKEN` environment variable.
- **Data Privacy & Publishing**: TraceGate operates locally and writes merged manifests to user-specified output paths. Do not publish modified manifests or internal/private repository data to public endpoints without explicit authorization.
