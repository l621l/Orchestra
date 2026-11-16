# Orchestra

Run coding agents in several local projects at once. Each project has its own model choices, budget, Git permissions, and run history.

## Desktop quick start

Run the launcher from the Orchestra directory.

On Windows, run:

```powershell
.\start-windows.cmd
```

On Ubuntu or Debian, including WSL with GUI support, run:

```bash
bash start-linux.sh
```

Both launchers create a virtual environment and install Python packages. The Windows launcher can install Python and Git through WinGet. The Linux launcher uses APT for system packages.

### Manual setup

**Windows.** Install Python 3.11 or newer with the Python launcher and make sure Git is on PATH. Then run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-qt.txt
.\.venv\Scripts\python.exe -B orchestra_qt.py
```

**Ubuntu or Debian.** Use a graphical session or WSL with GUI support, then run:

```bash
sudo apt update
sudo apt install git python3 python3-venv libxcb-cursor0
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-qt.txt
.venv/bin/python -B orchestra_qt.py
```

### First run

1. In **Global Settings → Model Catalog**, check the providers and models you want to use. Double-click a provider to configure its connection and API key. Edit custom rates under **Model Pricing**.
2. Create or add a project. In **Project Settings**, select the executor and reviewer, set limits, and configure Git permissions.
3. Start a task. Switch projects to run another task in parallel. Use **Continue** to resume a saved task or **Debug Only** to review an existing project.

The app shows live logs and any reasoning the provider sends. **Usage** shows estimated cost, tokens, and cache hit rate by project and model. Automatic commits require a successful check after the latest edit. Push also requires permission in the project's Git settings.

By default, the task runs implementation followed by review. **With Debug** controls that review step. An incomplete stage stops the run, and a budget stop leaves the task available to resume.

## Console

Install dependencies and copy `config.example.json` to `config.local.json`. Enter your provider key in the local copy.

```bash
python -m pip install -r requirements.txt
python -B orchestra.py --workspace /path/to/project
```

Use `N: <task>` for a new task, `C` to continue, `S` for status, and `Q` to quit. Pass `--data-dir` to use the desktop app's settings and history.

Desktop data lives in `%LOCALAPPDATA%\Orchestra` on Windows and `~/.local/share/Orchestra` on Linux by default.
