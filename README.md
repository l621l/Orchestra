# Orchestra

Run coding agents in several local projects at once. Each project has its own model choices, budget, Git permissions, and run history.

## Desktop

On Windows, install Python and Git, then run:

```powershell
py -m pip install -r requirements-qt.txt
py -B orchestra_qt.py
```

On Ubuntu or Debian, including WSL with GUI support, run:

```bash
bash start-linux.sh
```

The Linux launcher creates a virtual environment and installs missing dependencies. It may ask for your sudo password to install system packages.

1. In **Global Settings**, configure a provider, its API key, and model prices.
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

## Tests

Install `requirements-qt.txt` to include the Qt tests, then run:

```bash
python -B -m unittest discover -q
```

Tests run without live model calls.
