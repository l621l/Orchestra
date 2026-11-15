"""Interactive entry point for Orchestra."""
import argparse
from pathlib import Path

from workspace_status import format_status
from task_state import input_command
from orchestra_app import OrchestraApp
from orchestra_console import AgentDashboard, print_usage_summary
from workspace_lease import WorkspaceBusy


async def main(base):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=base / "workspace")
    parser.add_argument("--data-dir", type=Path, default=base)
    args = parser.parse_args()
    if args.workspace == base / "workspace":
        args.workspace.mkdir(exist_ok=True)
    app = OrchestraApp.from_directory(args.data_dir, args.workspace, dashboard=AgentDashboard())
    engine = app.engine
    print(f"Direct workspace: {engine.files.root}")
    for role in ("writer",):
        settings = engine.role_settings(role)
        print(f"{role}: {settings['provider']} / {settings['model']}")
    while True:
        print('\n'+format_status(app.status()))
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        command,payload=input_command(text)
        if command == 'exit':
            break
        if command == 'status':
            continue
        if command == 'new' and not payload:
            try:
                payload=input('New task: ').strip()
            except (EOFError,KeyboardInterrupt):
                break
            if not payload:continue
            text='N: '+payload
        if not text:
            continue
        try:
            result = await app.run(text)
        except WorkspaceBusy as error:
            print(error)
            continue
        if result['status'] == 'failed':
            print(f"Turn failed ({result['error_type']}): {result['error']}\n"
                  "Local file changes were NOT rolled back.")
        elif result['status'] == 'cancelled':
            print("\nTurn cancelled. Local file changes were NOT rolled back.")
        else:
            print(f"\nAssistant:\n{result['answer']}")
            if result['status'] == 'partial':
                print("\n[Partial result: inspect reported gaps and verification in the log.]")
        print_usage_summary(result['usage'], engine.estimated_cost)
        print(f"  Log: {result['log_directory']}")
