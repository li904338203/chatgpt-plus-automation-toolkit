# Control Panel Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a Windows desktop control panel for `chatgpt_assistant_source(1)` that manages API keys, proxies, card codes, phones, account pools, output pools, and can start PayPal workflows with live logs.

**Architecture:** Add a Tkinter GUI layer that edits existing TXT and `.env` files without changing the core automation modules. Add a small `panel_runner.py` CLI bridge so the GUI can launch workflow actions as subprocesses, stream logs safely, and emit structured result events for the output panel. Package with PyInstaller in `onedir` mode.

**Tech Stack:** Python, Tkinter, subprocess, pathlib, PyInstaller, existing project modules.

---

### Task 1: Add Panel File Registry

**Files:**
- Create: `control_panel/file_registry.py`
- Test: `tests/test_control_panel_file_registry.py`

**Step 1: Write the failing test**

Create tests for resolving known resource names to paths under the project root. Include `paypal_card_codes`, `paypal_cards`, `paypal_phones`, `proxy_default`, `proxy_jp`, `proxy_us`, `hotmail_accounts`, `paypal_links`, and `paypal_pending_auth`.

**Step 2: Implement registry**

Create a `PanelFile` dataclass with `key`, `label`, `path`, and `kind`. Add `get_panel_files(root: Path) -> dict[str, PanelFile]`.

**Step 3: Verify**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_control_panel_file_registry.py -v
```

Expected: all tests pass.

### Task 2: Add TXT Pool Service

**Files:**
- Create: `control_panel/text_pool_service.py`
- Test: `tests/test_text_pool_service.py`

**Step 1: Write tests**

Test read, save, append import lines, export text, clear, and deduplicate. Preserve non-empty lines and keep order during dedupe.

**Step 2: Implement service**

Implement functions:

- `read_text(path: Path) -> str`
- `save_text(path: Path, content: str) -> None`
- `import_txt(path: Path, imported: str, append: bool = True) -> int`
- `export_txt(path: Path) -> str`
- `clear_file(path: Path) -> None`
- `dedupe_lines(path: Path) -> int`

**Step 3: Verify**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_text_pool_service.py -v
```

Expected: all tests pass.

### Task 3: Add `.env` Service

**Files:**
- Create: `control_panel/env_service.py`
- Test: `tests/test_env_service.py`

**Step 1: Write tests**

Test parsing key-value pairs, preserving comments and unknown keys, updating known keys, and adding missing keys at the end.

**Step 2: Implement service**

Implement:

- `read_env(path: Path) -> dict[str, str]`
- `update_env(path: Path, updates: dict[str, str]) -> None`
- `get_known_env_fields() -> list[str]`

Known fields come from the design document.

**Step 3: Verify**

Run:

```powershell
.\.venv\Scripts\python -m pytest tests/test_env_service.py -v
```

Expected: all tests pass.

### Task 4: Add Workflow Runner Bridge

**Files:**
- Create: `panel_runner.py`
- Test: `tests/test_panel_runner_args.py`

**Step 1: Write argument parsing tests**

Test actions: `paypal-flow1`, `paypal-flow2`, `paypal-flow3`, `paypal-auto`. Test `--count` and `--workers`.

**Step 2: Implement CLI bridge**

Use `argparse`. Load config with existing `main.apply_env_config` and call existing PayPal module functions. Keep stdout line-oriented so the GUI can stream logs. Add a helper that emits JSONL result events using this shape:

```json
{"type":"result","flow":"paypal-flow2","account":"user@example.com","status":"failure","message":"CAPTCHA 未通过","path":""}
```

The first implementation may emit run-level summaries if per-account hooks are not available yet. The GUI must still parse existing text logs as a fallback.

**Step 3: Verify**

Run:

```powershell
.\.venv\Scripts\python panel_runner.py --help
.\.venv\Scripts\python -m pytest tests/test_panel_runner_args.py -v
```

Expected: help prints actions and tests pass.

### Task 5: Build Tkinter Shell

**Files:**
- Create: `control_panel_app.py`
- Create: `control_panel/__init__.py`

**Step 1: Create main window**

Use Tkinter with a left navigation list and a right content area. Add pages: Run, API Key, proxies, card codes, cards, phones, mail pools, links, pending auth, config, logs.

**Step 2: Add resource editor page**

Build one reusable text editor page for TXT files. It must support refresh, save, import TXT, export TXT, clear, and dedupe.

**Step 3: Add `.env` editor page**

Render known fields as labels and entries. Save updates via `env_service.update_env`.

**Step 4: Smoke test**

Run:

```powershell
.\.venv\Scripts\python control_panel_app.py
```

Expected: window opens and resource pages can read source1 files.

### Task 6: Add Workflow Run Page

**Files:**
- Modify: `control_panel_app.py`

**Step 1: Add controls**

Add count and worker inputs. Add buttons for Flow 1, Flow 2, Flow 3, and Auto.

**Step 2: Add subprocess execution**

Launch:

```powershell
.\.venv\Scripts\python panel_runner.py <action> --count N --workers W
```

Stream stdout/stderr into the log widget. Add Stop button that terminates the child process.

**Step 3: Add result output panel**

Add a separate results table below or beside the log widget. Columns:

- Time
- Flow
- Account
- Status
- Message
- Output Path

When a subprocess line is JSONL with `type=result`, insert it directly. Otherwise, parse common text logs containing `[OK]`, `[FAIL]`, `失败:`, `成功`, `link ok`, `支付成功`, and `授权成功` into best-effort result rows.

**Step 4: Add run summary**

At the end of each run, show counts for success and failure above the result table. Keep previous run results until the user clicks Clear Results.

**Step 5: Manual verification**

Run a harmless command first with `panel_runner.py --help`, then run one real action only when resources are ready.

### Task 7: Add PyInstaller Packaging

**Files:**
- Create: `build_panel.ps1`
- Create: `ChatGPTAssistantPanel.spec`
- Modify: `requirements.txt`

**Step 1: Add dependencies**

Add `pyinstaller` and `pytest` to `requirements.txt` if missing.

**Step 2: Create build script**

`build_panel.ps1` should run PyInstaller in `onedir` mode and copy required writable files and directories into `dist/ChatGPTAssistantPanel`.

**Step 3: Verify package**

Run:

```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
.\build_panel.ps1
.\dist\ChatGPTAssistantPanel\ChatGPTAssistantPanel.exe
```

Expected: packaged panel opens and can read `data/`, `.env`, and `config.yaml`.

### Task 8: Final Verification

**Files:**
- All files touched above

**Step 1: Run tests**

```powershell
.\.venv\Scripts\python -m pytest tests -v
```

Expected: all tests pass.

**Step 2: Compile Python files**

```powershell
.\.venv\Scripts\python -m py_compile control_panel_app.py panel_runner.py control_panel\file_registry.py control_panel\text_pool_service.py control_panel\env_service.py
```

Expected: no syntax errors.

**Step 3: Manual GUI check**

Open the panel, edit a test TXT pool, export it, import it back, and start `panel_runner.py --help` through the log view. Then run a short workflow dry path or controlled single-account run and verify the results table shows success or failure rows with the corresponding message.
