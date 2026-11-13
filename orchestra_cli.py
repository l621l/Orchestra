"""Interactive entry point for Orchestra."""
import argparse
import json
from pathlib import Path

from autodev_console import brief, input_command
from orchestra import error_description
from orchestra_console import AgentDashboard
from session_state import SessionState
from turn_trace import TurnTrace


async def main(base):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=base / "workspace")
    args = parser.parse_args()
    with (base / "config.local.json").open(encoding="utf-8") as file:
        config = json.load(file)
    with (base / "settings.json").open(encoding="utf-8") as file:
        runtime_settings = json.load(file)
    with (base / "prompts.json").open(encoding="utf-8") as file:
        prompts = json.load(file)
    with (base / "pricing.json").open(encoding="utf-8") as file:
        pricing = json.load(file)
    if args.workspace == base / "workspace":
        args.workspace.mkdir(exist_ok=True)
    from direct_dev import DirectDevelopment

    app = DirectDevelopment(config, args.workspace, runtime_settings, prompts, pricing,
                            dashboard=AgentDashboard())
    print(f"Pipeline workspace: {app.files.root}")
    for role in ("writer", "strong"):
        settings = app.role_settings(role)
        print(f"{role}: {settings['provider']} / {settings['model']}")
    while True:
        print('\n'+brief(app))
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
        suggestion=SessionState.layout_suggestion(text)
        if suggestion:
            print('Похоже, включена неверная раскладка. Предполагаемый текст:')
            print(suggestion)
            try:
                accepted=input('Использовать этот текст? [y/да, иначе введи заново]: ').strip().lower()
            except (EOFError,KeyboardInterrupt):
                break
            if accepted not in {'y','yes','д','да'}:
                print('Задание не отправлено в API.')
                continue
            text=suggestion
        turn_usage = {}
        trace = TurnTrace(app.logs_path, text, app.files.root)
        root_task_id = trace.start_task("worker", text)
        app.dashboard.start(trace.turn_id, trace.topic,
                            output_limit=app.total_output_limit)
        turn_status = "failed"
        turn_error = None
        try:
            worker_result = await app.run_user_task(text, turn_usage, trace, root_task_id)
            trace.event('workspace_result', modified_files=worker_result['modified_files'],
                        verification=worker_result['verification'],
                        chain_state=worker_result.get('chain_state'),
                        blocker=worker_result.get('blocker'),
                        iterations=worker_result.get('iterations'))
            answer = worker_result["answer"]
            result_status = worker_result["status"]
            trace.save_final_answer(answer)
            trace.finish_task(root_task_id, result_status)
            activity = "Work incomplete; see report" if result_status == "partial" else "Task completed"
            dashboard_status = "partial" if result_status == "partial" else "done"
            app.dashboard.finish("worker", dashboard_status, activity)
            app.dashboard.stop()
            turn_status = result_status
            print(f"\nAssistant:\n{answer}")
            if result_status == "partial":
                print("\n[Partial result: inspect reported gaps and verification in the log.]")
        except Exception as error:
            turn_error = type(error).__name__
            description = error_description(error)
            trace.error_message = description
            trace.event('turn_error', error_type=turn_error, message=description)
            app.dashboard.finish("worker", "failed", turn_error)
            app.dashboard.stop()
            trace.finish_task(root_task_id, "failed", turn_error)
            print(f"Turn failed ({turn_error}): {description}\nLocal file changes were NOT rolled back.")
        except KeyboardInterrupt:
            turn_status = "cancelled"
            turn_error = "KeyboardInterrupt"
            app.dashboard.finish("worker", "failed", "cancelled")
            app.dashboard.stop()
            trace.finish_task(root_task_id, "cancelled", turn_error)
            print("\nTurn cancelled. Local file changes were NOT rolled back.")
        finally:
            app.dashboard.stop()
            trace.finish(turn_status, turn_error)
            app.print_usage_summary(turn_usage)
            print(f"  Log: {trace.directory}")
