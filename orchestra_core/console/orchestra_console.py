"""Terminal dashboard and usage reporting."""
import time
from datetime import datetime

from rich import box
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from orchestra_core.runtime.orchestra_progress import clean, describe_tool
from orchestra_core.workspace.tool_contracts import EDIT_TOOL_NAMES


class AgentDashboard:
    def __init__(self):
        self.console = Console()
        self.live = None
        self.states = {}
        self.title = "Orchestra"
        self.output_used = 0
        self.output_limit = 20000
        self.output_estimated = False
        self.models = {}
        self.active_model = None
        self.cycle = ''

    def configure_models(self, models):
        self.models = dict(models)

    def resolve_label(self, label):
        return (self.active_model or 'writer') if self.models and label == 'worker' else label

    def activate_model(self, label='writer'):
        self.active_model = label
        self.cycle = 'Direct'
        self.update(label, activity='Preparing request')

    def record_usage(self, label, entry):
        label = self.resolve_label(label)
        if label not in self.states:
            self.update(label)
        self.states[label].update(calls=entry.get('api_calls',0),
            input_tokens=entry.get('prompt_tokens',0),
            output_tokens=entry.get('output_budget_tokens',entry.get('completion_tokens',0)),
            estimated=entry.get('output_budget_estimated',False))

    clean = staticmethod(clean)
    describe_tool = staticmethod(describe_tool)

    def action(self, label, message, failed=False):
        label = self.models.get(self.resolve_label(label), label)
        stamp = datetime.now().strftime('%H:%M:%S')
        self.console.print(Text(f'{stamp} [{label}] {self.clean(message, 650)}',
                                style='red' if failed else None))

    def tool_finished(self, label, name, args, output, duration):
        value = output.get('result', {})
        failed = not output['ok'] or (name in {'run_check','run_command'} and not value.get('passed', False))
        if not output['ok']:
            detail = f"{output.get('error')}: {output.get('message', '')}"
        elif name in {'run_check','run_command'}:
            detail = f"{'PASS' if value.get('passed') else 'FAIL'} (exit={value.get('returncode')})"
        elif name in EDIT_TOOL_NAMES:
            detail = f"changed {', '.join(value.get('files', []))}"
            memory=value.get('interface_memory')
            if memory:
                detail += f"; interfaces {'updated' if memory.get('updated') else 'NOT updated'}"
                detail += f"; notes saved={memory.get('notes_saved',0)}, stale={memory.get('notes_stale',0)}"
        elif name == 'read_file':
            detail = f"lines {value.get('returned_start_line')}-{value.get('returned_end_line')}"
        elif name == 'list_files':
            detail = f"{value.get('entries', 0)} entries"
        elif name in {'search_text', 'search_regex'}:
            detail = f"{value.get('matches_returned', 0)} matches"
        else:
            detail = 'OK'
        self.action(label, f'{self.describe_tool(name, args)} -> {detail} ({duration:.2f}s)', failed)
        diagnostic = output.get('diagnostic') or output.get('output') or value.get('output')
        if failed and diagnostic:
            # Keep the actual final traceback, with literal terminal text.
            self.console.print(Text(''.join(c for c in str(diagnostic)[-3000:]
                                           if c.isprintable() or c in '\n\t'), style='red'))
        self.update(label, activity=f'{self.describe_tool(name,args)} -> {detail}')

    def render(self):
        title = self.title + (f' | {self.cycle}' if self.cycle else '')
        table = Table(title=Text(title), box=box.ASCII, expand=True,
                      header_style="bold cyan", border_style="bright_black")
        table.add_column("Model", style="bold", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Doing now")
        table.add_column("Time", justify="right", no_wrap=True)
        table.add_column("Calls", justify="right", no_wrap=True)
        table.add_column("Input", justify="right", no_wrap=True)
        table.add_column("Output", justify="right", no_wrap=True)
        table.add_column("Spent", justify="right", no_wrap=True,
                         style="bright_green")
        total_cost = 0.0
        known_costs = False
        total_calls = total_input = 0
        def compact_tokens(value):
            return (f"{value / 1000:.1f}".rstrip('0').rstrip('.') + 'k') if value >= 1000 else str(value)
        for label, state in self.states.items():
            prefix = "  +-- " if state["depth"] else ""
            elapsed = state.get("duration")
            if elapsed is None:
                elapsed = state.get('accumulated',0) + time.monotonic() - state["started"]
            status = state["status"]
            status_style = {"running": "yellow", "done": "green",
                            "partial": "yellow", "failed": "red"}.get(status, "white")
            cost = state.get("cost")
            if cost is None:
                cost_text = "-"
            else:
                cost_text = f"${cost:.5f}"
                if state.get('estimated'):
                    cost_text = '~'+cost_text
                total_cost += cost
                known_costs = True
            calls = state.get('calls',0)
            total_calls += calls
            total_input += state.get('input_tokens',0)
            output = compact_tokens(state.get('output_tokens',0))
            if state.get('estimated'):
                output = '~'+output
            display_status = 'working' if status == 'running' else status
            table.add_row(Text(prefix + self.models.get(label,label)), f"[{status_style}]{display_status}[/{status_style}]",
                          Text(state["activity"]), f"{elapsed:.1f}s", str(calls),
                          compact_tokens(state.get('input_tokens',0)), output, cost_text)
        if self.states:
            table.add_section()
            tokens = compact_tokens(self.output_used)
            if self.output_estimated:
                tokens = '~'+tokens
            table.add_row("TOTAL", "", "Output tokens (incl. reasoning)", "", str(total_calls),
                          compact_tokens(total_input), tokens,
                          f"${total_cost:.5f}" if known_costs else "-")
        return table

    def update_output_budget(self, used, limit, estimated=False):
        self.output_used = used
        self.output_limit = limit
        self.output_estimated = estimated
        if self.live:
            self.live.update(self.render(), refresh=True)

    def start(self, turn_id, topic, output_limit=20000):
        self.states = {}
        self.output_used = 0
        self.output_limit = output_limit
        self.output_estimated = False
        self.active_model = None
        self.cycle = ''
        self.title = f"Orchestra | {topic}"
        for label in self.models:
            self.update(label, status='waiting', activity='Not called yet')
            self.states[label]['duration'] = 0
        self.live = Live(get_renderable=self.render, console=self.console, refresh_per_second=4,
                         transient=False)
        self.live.start()

    def stop(self):
        if self.live:
            self.live.update(self.render(), refresh=False)
            self.live.stop()
            self.live = None

    def update(self, label, depth=0, activity="waiting", chunks=0,
               status="running", cost=None):
        label = self.resolve_label(label)
        state = self.states.setdefault(label, {
            "depth": depth, "started": time.monotonic(), "duration": None,
            "status": "running", "activity": "starting", "chunks": 0,
            "cost": None,
            "accumulated": 0,
        })
        if state["status"] != "running" and status == "running":
            state["started"] = time.monotonic()
            state["duration"] = None
        state.update(depth=depth, activity=activity, chunks=chunks, status=status)
        if cost is not None:
            state["cost"] = cost
        if self.live:
            self.live.update(self.render(), refresh=True)

    def finish(self, label, status="done", activity="completed"):
        if self.models and label == 'worker':
            for model_label in self.models:
                active = model_label == self.active_model
                self.finish(model_label, status if active else 'done',
                            activity if active else 'Turn finished')
            return
        label = self.resolve_label(label)
        state = self.states.get(label)
        if not state:
            return
        if state['status'] == 'running':
            state['accumulated'] = state.get('accumulated',0) + time.monotonic() - state['started']
        state["duration"] = state.get('accumulated',0)
        state["status"] = status
        state["activity"] = activity
        if self.live:
            self.live.update(self.render(), refresh=True)


def print_usage_summary(turn_usage, estimated_cost):
    if not turn_usage:
        print("\nToken usage: no API calls were made.")
        return
    print("\nToken usage for this turn:")
    totals = {name: 0 for name in ("api_calls", "reported_calls",
        "prompt_tokens", "completion_tokens", "total_tokens",
        "reasoning_tokens", "cached_tokens")}
    total_cost = 0.0
    all_costs_known = True
    for label, entry in turn_usage.items():
        for name in totals:
            totals[name] += entry[name]
        missing = entry["api_calls"] - entry["reported_calls"]
        suffix = f" | usage missing for {missing} call(s)" if missing else ""
        cost = estimated_cost(entry)
        if cost is None:
            all_costs_known = False
            cost_text = ""
        else:
            total_cost += cost
            cost_text = f" | estimated=${cost:.5f}"
        print(f"  {label} ({entry['provider']} / {entry['model']}): "
              f"calls={entry['api_calls']} | input={entry['prompt_tokens']} "
              f"(cached={entry['cached_tokens']}) | output={entry['completion_tokens']} "
              f"(reasoning={entry['reasoning_tokens']}) | total={entry['total_tokens']}"
              f"{cost_text}{suffix}")
    missing = totals["api_calls"] - totals["reported_calls"]
    suffix = f" | usage missing for {missing} call(s)" if missing else ""
    total_cost_text = f" | estimated=${total_cost:.5f}" if all_costs_known else ""
    print(f"  TOTAL: calls={totals['api_calls']} | input={totals['prompt_tokens']} "
          f"(cached={totals['cached_tokens']}) | output={totals['completion_tokens']} "
          f"(reasoning={totals['reasoning_tokens']}) | total={totals['total_tokens']}"
          f"{total_cost_text}{suffix}")
