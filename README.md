# Multi-Repository AI Bug-Finding and Fixing Agent

A resilient, multi-repository Python automation agent designed to scan GitHub repositories for candidate bug issues, analyze defects with evidence-based AI reasoning, manage human approvals via Gmail, generate isolated code patches in disposable workspaces, execute sandboxed regression tests, and submit draft pull requests upon explicit human publication authorization.

---

## 1. Problem Statement & Overview

Open-source project maintainers spend significant time triaging issues, reproducing reported bugs, validating potential fixes, and testing diffs. Fully automated bots often generate low-quality, unsolicited, or broken pull requests that consume more maintainer attention than they save.

This agent implements a **strict human-in-the-loop, defense-in-depth architecture**:
- **Zero unsolicited pull requests**: Every patch requires dual human authorization (Investigation Approval + separate Publication Approval).
- **Draft PRs only**: When authorized, the agent creates a **draft PR** on a dedicated branch; it **never** merges directly to default branches (`main`/`master`).
- **Untrusted data isolation**: Repository issues, comments, diffs, and incoming emails are treated as untrusted data with strict path traversal, drive-letter, and prompt-injection safeguards.
- **Sandboxed test execution**: Diffs are executed in disposable workspaces with Docker container isolation, network cutoff (`--network none`), resource caps, and credential redaction.

---

## 2. Architecture & Lifecycle State Machine

The agent enforces an explicit, unidirectional 12-state SQLite-backed state machine:

```
[DISCOVERED] ──> [ANALYZED] ──> [AWAITING_APPROVAL] ──> [APPROVED]
                                        │
                                        └──> [REJECTED]
                                        │
                                        └──> [EXPIRED]

[APPROVED] ──> [PATCH_READY] ──> [TESTED] ──> [AWAITING_PUBLICATION_APPROVAL] ──> [PUBLISHED]
                      │              │                         │
                      └──> [FAILED]  └──> [NEEDS_REVIEW]       └──> [REJECTED]
```

### Core Architecture Components

```
github_agent/
├── .github/workflows/ci.yml # Continuous Integration (Ubuntu & Windows matrix)
├── .env.example             # Template environment variables (no secrets)
├── repos.example.json       # Template target repository configuration
├── repos.json               # Active repository configuration
├── pyproject.toml           # Packaging metadata, entrypoints & pytest config
├── run_all_tests.py         # Self-contained test suite runner (zero extra deps)
├── src/bug_agent/
│   ├── ai_analyzer.py       # AI provider abstraction, prompt assembly & validator
│   ├── approval_manager.py  # Inbound email command validation & state transitions
│   ├── candidate_filter.py  # Bug candidate filtering, bot rejection & deduplication
│   ├── cli.py               # Command-line interface
│   ├── config.py            # Runtime configuration parsing and validation
│   ├── copilot_adapter.py   # GitHub Copilot CLI adapter & availability probe
│   ├── github_client.py     # Resilient, read-only GitHub REST API client
│   ├── gmail_client.py      # Gmail REST API client (least privilege) & mock client
│   ├── issue_scanner.py     # Multi-repository scanner with stage diagnostics
│   ├── models.py            # Domain models (Issue, Task, AnalysisResult, etc.)
│   ├── patch_generator.py   # Disposable workspace & unified diff generator
│   ├── publisher.py         # Draft PR publisher with drift & branch protection
│   ├── task_store.py        # SQLite persistence, transactions & state transitions
│   ├── test_runner.py       # Container sandboxing, diff hashing & evidence logging
│   └── workflow.py          # Defect analysis workflow engine
└── tests/                   # 13 comprehensive unit, integration & security test suites
```

---

## 3. Prerequisites & Requirements

- **Operating System**: Windows 10/11, macOS, or Linux.
- **Python**: **Python 3.10+** (tested on 3.10, 3.11, 3.12, and 3.14).
- **Git**: Installed and available in your system `PATH`.
- **Docker Desktop** *(Optional, for containerized sandbox testing)*: Required only when running real test execution without `--mock-sandbox`.

---

## 4. Installation & Setup (Windows & Cross-Platform)

### Step 1: Clone and Navigate
```powershell
cd github_agent
```

### Step 2: Create and Activate Virtual Environment
On Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
*(If script execution is disabled in PowerShell, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first, or use `cmd.exe`: `.\.venv\Scripts\activate.bat`)*

On macOS/Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Step 3: Install Package & Dependencies
Install the package in editable mode with development dependencies:
```powershell
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```
*(To install optional live Gmail OAuth libraries, run `python -m pip install -e ".[all]"`)*

### Step 4: Configure Repositories
Copy the example configuration:
```powershell
copy repos.example.json repos.json
```
Edit `repos.json` with your target repositories:
```json
{
  "repositories": [
    "owner/repository"
  ],
  "issues_per_repository": 30,
  "include_pull_requests": false,
  "request_timeout_seconds": 15
}
```

### Step 5: Environment Variables (Optional)
Copy `.env.example` to `.env`:
```powershell
copy .env.example .env
```
Key variables:
- `GITHUB_TOKEN`: Recommended to raise rate limits (unauthenticated: 60 req/hr; authenticated: 5,000 req/hr). Use a fine-grained token with read-only access to **Issues** and **Metadata**.
- `DATABASE_PATH`: SQLite database file path (default: `tasks.db`).
- `LOG_LEVEL`: Logging verbosity (`INFO`, `DEBUG`, `WARNING`, `ERROR`).

---

## 5. Verification & Test Suite Execution

The test suite requires **no external credentials, no GitHub tokens, no live Gmail OAuth, and no Copilot subscription**. All network calls are mocked.

Run using pytest:
```powershell
python -m pytest -v
```

Or run using the self-contained standard library test runner:
```powershell
python run_all_tests.py
```
*Current test suite status: **84 passed across 13 test modules** with 0 failures.*

---

## 6. Operational Workflows & CLI Usage

### Step 1: Scan Configured Repositories
Discovers candidate issues, filters out bots and pull requests, and records new candidates in `tasks.db` with status `DISCOVERED`:
```powershell
python -m bug_agent.cli --config repos.json
```
*Diagnostic stage summaries display exact counts of raw items fetched, pull requests excluded, and candidate filter reasons.*

### Step 2: Inspect Database Status
Inspect all stored tasks and their lifecycle approval states without triggering a remote GitHub scan:
```powershell
python -m bug_agent.cli --status
```
*(You can also target specific databases: `python -m bug_agent.cli --db-path reports/pilot_tasks.db --status`)*

### Step 3: Run AI Defect Analysis
Analyze all `DISCOVERED` issues and transition eligible defects to `ANALYZED`:
```powershell
# Using deterministic Mock AI provider (offline, safe, simulated):
python -m bug_agent.cli --config repos.json --analyze --ai-provider mock
```
> **Important Note on Mock AI**: Mock AI analyses are deterministic simulations for pipeline verification and dry runs; they do not establish that an issue is an actual defect in the source repository.

### Step 4: Coordinate Human Approvals (Gmail)
To send approval requests for `ANALYZED` tasks:
```powershell
# Mock Gmail mode (simulated queue, no live credentials required):
python -m bug_agent.cli --request-approvals --mock-gmail --approver dev@example.com

# Check for inbound approval replies:
python -m bug_agent.cli --check-approvals --mock-gmail
```

In production Gmail mode (`--approver approver@example.com` without `--mock-gmail`), the approver receives an email containing defect evidence and replies with:
- `APPROVE TASK-owner-repo-123` to authorize patch generation.
- `REJECT TASK-owner-repo-123` to reject the task.

### Step 5: Isolated Patch Generation
Generates a candidate code patch in a disposable workspace for an `APPROVED` task and transitions state to `PATCH_READY`:
```powershell
python -m bug_agent.cli --generate-patch TASK-owner-repo-123
```
*(Add `--dry-run` to preview the unified diff without committing changes to disk or database.)*

### Step 6: Sandboxed Test Execution
Executes unit and regression tests in a strictly isolated container sandbox:
```powershell
# Using deterministic mock sandbox (for testing without Docker daemon):
python -m bug_agent.cli --run-tests TASK-owner-repo-123 --mock-sandbox

# Using live Docker sandbox:
python -m bug_agent.cli --run-tests TASK-owner-repo-123
```
*Tasks that pass test assertions transition to `TESTED`. If tests fail, the task transitions to `FAILED`.*

### Step 7: Request Publication Approval
Tasks in `TESTED` state **cannot** be published automatically. A separate publication authorization is mandatory:
```powershell
# Request publication approval:
python -m bug_agent.cli --request-publication --mock-gmail

# Process publication reply:
python -m bug_agent.cli --check-approvals --mock-gmail
```
The approver must explicitly reply with:
- `APPROVE PUBLISH TASK-owner-repo-123`
- `REJECT PUBLISH TASK-owner-repo-123`

### Step 8: Publish Draft Pull Request
Creates a dedicated branch `bugfix/task-owner-repo-123` and submits a **draft pull request** via the GitHub REST API:
```powershell
# Real publication (requires GITHUB_TOKEN with pull request write permission):
python -m bug_agent.cli --publish TASK-owner-repo-123

# Simulated / dry-run publication:
python -m bug_agent.cli --publish TASK-owner-repo-123 --dry-run
```
*Draft pull requests are tagged `[SIMULATED]` in dry-run mode and are never automatically merged.*

---

## 7. AI Provider Integration Status

### Mock AI Provider
- Fully functional, offline, deterministic, and bundled by default.
- Implements simulated timeouts, format errors, and API failures for robust error recovery testing.

### GitHub Copilot CLI Adapter (`copilot_adapter.py`)
- **Status: Unverified on this host / Requires local setup**.
- Standard GitHub Student and Individual subscriptions do not provide direct OpenAI-style API endpoints. Copilot programmatic access requires an authenticated GitHub Copilot CLI session.
- **Requirements**:
  1. Install GitHub CLI (`gh`) or Node.js / npm.
  2. Install Copilot extension: `gh extension install github/gh-copilot` (or `npm install -g @github/copilot-cli`).
  3. Authenticate interactively: `gh auth login --web` or `copilot auth login`.
  4. Ensure the binary is accessible on `PATH` or specify `COPILOT_CLI_PATH`.
- The adapter performs a live execution probe (`copilot --version`); if the CLI is absent or a non-functional shim is encountered, it raises a clear error with setup guidance rather than failing silently.

---

## 8. Gmail OAuth Setup & Permissions

When transitioning from `--mock-gmail` to production Gmail:
1. Create a project in the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Gmail API**.
3. Create OAuth 2.0 Desktop Application credentials and download as `credentials.json`.
4. The agent requests **least-privilege scopes**:
   - `https://www.googleapis.com/auth/gmail.send` (send approval requests only)
   - `https://www.googleapis.com/auth/gmail.readonly` (poll incoming decision replies only)
5. On first live run, an interactive browser authorization will generate `token.json`.
6. **Security Standard**: `credentials.json` and `token.json` are excluded in `.gitignore` and must never be committed.

---

## 9. Security Principles & Defenses

1. **Untrusted Data Boundary**:
   - Issue titles and bodies are capped (500 and 6,000 characters).
   - Prompt context is defended by a system safety prompt explicitly instructing models to ignore embedded instructions or prompt injections.
   - `is_fix_tested` is hard-coded to `False` by `ResponseValidator` regardless of LLM claims.
2. **Filesystem Protections**:
   - Disposable workspaces reject directory traversal (`..`), absolute paths, and Windows drive letters (`C:`).
   - Patches targeting protected files (`.github/workflows`, `.git`, `.env`, `credentials.json`, `token.json`) are rejected unconditionally.
3. **Approval Integrity**:
   - Approval commands verify sender email address against `APPROVER_EMAIL`. Display names are ignored.
   - Investigation commands (`APPROVE TASK-...`) cannot authorize publication.
   - Publication commands (`APPROVE PUBLISH TASK-...`) cannot authorize patch generation.
   - Replay protection stores message IDs; duplicate, expired, and conflicting decisions are rejected.
4. **Credential Scrubbing**:
   - Sandbox test runner output automatically redacts GitHub tokens (`ghp_...`, `github_pat_...`), Google OAuth tokens (`ya29....`), and generic API key patterns before persistence.
5. **No Automatic Merging**:
   - Publication strictly creates draft pull requests; no merge endpoints are implemented.

---

## 10. Known Limitations

- **Container Runtime**: Full sandboxed test execution requires a local Docker daemon. Systems without Docker must use `--mock-sandbox` or tests transition to `NEEDS_REVIEW`.
- **Copilot CLI Automation**: Programmatic batch generation without an interactive terminal depends on the installed Copilot CLI version supporting non-interactive mode.
- **Network Boundaries**: In Docker test mode, `--network none` prevents dependency downloads during test execution; dependencies must be pre-installed in the container base image.

---

## 11. License

*(License to be selected by repository owner prior to public release. Recommended: MIT License or Apache License 2.0.)*
