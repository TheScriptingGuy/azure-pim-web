# azure-pim-web

Browser UI for Azure PIM group activation and approval — thin FastAPI wrapper around [`azure-pim-cli`](https://pypi.org/project/azure-pim-cli/).

[![CI](https://github.com/TheScriptingGuy/azure-pim-web/actions/workflows/ci.yml/badge.svg)](https://github.com/TheScriptingGuy/azure-pim-web/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/azure-pim-web)](https://pypi.org/project/azure-pim-web/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

## Features

- Table of eligible role assignments, default-sorted by group name
- Click any column header to sort asc/desc (Group, Role, Max duration, Expires, Env)
- Environment pill derived from role name via regex — `DEV` / `TST` / `ACC` / `PRD`
- Live search filter across name, description, and environment
- Click a row to select it with duration pre-set to the policy maximum; adjust the dropdown before activating
- Pending approvals tab

## Requirements

- Python 3.12+
- Azure CLI logged in (`az login`) — used by `azure-pim-cli` to fetch a Graph token

## Install

```bash
# Recommended: isolated install via uv
uv tool install azure-pim-web

# Or with pip
pip install azure-pim-web
```

## Run

### Option 1 — uv (no manual venv needed)

```bash
uv run python -m pim_web.main
```

Or if installed as a tool:

```bash
pim-web
```

### Option 2 — Script entrypoint (activated venv)

```bash
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install azure-pim-web
pim-web

# macOS / Linux
python -m venv .venv
source .venv/bin/activate
pip install azure-pim-web
pim-web
```

Both options open `http://127.0.0.1:8080` in your default browser automatically.

## How it works

### 1. Token

On startup the UI checks for a valid bearer token. Click **Get Token** to grab one via `azure-pim-cli`'s `GraphClient` (delegates to `az account get-access-token`). Alternatively, paste a token manually using the **Paste ▼** button.

The token status pill in the top bar shows: `● Valid` (green, with expiry + UPN), `● No token` (red), or `● Unknown` (grey).

### 2. Eligible roles

Click **↻ Refresh** on the *Eligible Roles* tab. The UI calls `GET /api/eligibilities`, which uses `azure_pim_cli.cli.fetch_eligibilities` to list your PIM-eligible group assignments from Microsoft Graph. Rows show:

| Column | Source |
|---|---|
| Group | `displayName` from the eligibility |
| Role | `accessId` → Member / Owner pill |
| Max duration | `policyMaxDurationHours` from the assignment policy |
| Eligibility expires | `endDateTime` (or *Permanent*) |
| Flags | NEW / MFA / Ticket requirements |
| Env | Derived via regex on name + description |

### 3. Sort / filter

- **Sort** — Click any sortable column header. First click = ascending (▲), second = descending (▼). Default on load is Group ascending.
- **Filter** — Type in the *Filter roles…* box. Matches substring in group name, description, or derived environment. Count badge shows `matched / total` when active.

### 4. Environment detection

A regex runs on `displayName` + `description` at load time. Patterns (case-insensitive):

| Pill | Matched tokens |
|---|---|
| PRD | `prd`, `prod`, `production` |
| ACC | `acc`, `acceptance`, `uat`, `stg`, `staging`, `preprod` |
| TST | `tst`, `test`, `qa` |
| DEV | `dev`, `development`, `sandbox`, `sbx` |

No match → `—`. The pattern requires the token to be preceded/followed by `-`, `_`, space, or word boundary to avoid false positives (e.g. `product` won't match `prd`).

### 5. Activate

1. Click a row (or its checkbox) to select it. The row highlights and an expand panel appears below it.
2. The **Duration** dropdown defaults to the policy maximum (`policyMaxDurationHours`). Adjust if you want a shorter window.
3. If the role requires **Justification** or a **Ticket number**, fill those in the expand panel.
4. Click **Activate selected (n)** to submit all selected rows in one request.
5. Each row's Status column updates live: `⏳ Activating…` → `✓ Provisioned` (green), `⏳ AwaitingApproval` (amber), or `✗ Failed` (red). An **Activation log** panel appears below the table.

### 6. Pending Approvals

Switch to the **Pending Approvals** tab. Click **↻ Refresh** to load approval requests from other users. Select rows, enter an **Approver justification**, and click **Approve selected**.

## Local development

```bash
git clone https://github.com/TheScriptingGuy/azure-pim-web.git
cd azure-pim-web

# Install with dev extras (uv resolves azure-pim-cli from PyPI)
uv sync --extra dev

# Run
uv run python -m pim_web.main

# Test / lint
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy src/pim_web
```

## Release

Releases are automated via GitHub Actions. Tag a commit on `main`:

```bash
git tag v0.x.y -m "Release v0.x.y"
git push origin v0.x.y
```

The `release.yml` workflow builds sdist + wheel and publishes to PyPI via Trusted Publishing (OIDC). No secrets needed in the repo.

## License

Apache-2.0 — see [LICENSE](LICENSE).
