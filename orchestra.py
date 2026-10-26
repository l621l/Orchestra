"""Orchestra: streaming status, buffered answers and read-only delegation."""
import argparse
import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from openai import AsyncOpenAI
from rich import box
from rich.console import Console
from rich.live import Live
from rich.table import Table
from tools import FILE_TOOLS, DELEGATE_TOOL, ProjectTools, parse_arguments

BASE = Path(__file__).resolve().parent


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AgentDashboard:
    def __init__(self):
        self.console = Console()
        self.live = None
        self.states = {}
        self.title = "Orchestra"

    def render(self):
        table = Table(title=self.title, box=box.ROUNDED, expand=True,
                      header_style="bold cyan", border_style="bright_black")
        table.add_column("Agent", style="bold", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Activity")
        table.add_column("Time", justify="right", no_wrap=True)
        table.add_column("Stream", justify="right", no_wrap=True)
        table.add_column("Spent", justify="right", no_wrap=True,
                         style="bright_green")
        total_cost = 0.0
        known_costs = False
        for label, state in self.states.items():
            prefix = "  +-- " if state["depth"] else ""
            elapsed = state.get("duration")
            if elapsed is None:
                elapsed = time.monotonic() - state["started"]
            status = state["status"]
            status_style = {"running": "yellow", "done": "green",
                            "partial": "yellow", "failed": "red"}.get(status, "white")
            chunks = f"{state['chunks']} chunks" if state["chunks"] else "—"
            cost = state.get("cost")
            if cost is None:
                cost_text = "—"
            else:
                cost_text = f"${cost:.5f}"
                total_cost += cost
                known_costs = True
            table.add_row(prefix + label, f"[{status_style}]{status}[/{status_style}]",
                          state["activity"], f"{elapsed:.1f}s", chunks, cost_text)
        if self.states:
            table.add_section()
            table.add_row("TOTAL", "", "completed API calls", "", "",
                          f"${total_cost:.5f}" if known_costs else "—")
        return table

    def start(self, turn_id, topic):
        self.states = {}
        self.title = f"Orchestra · {topic}"
        self.live = Live(self.render(), console=self.console, refresh_per_second=4,
                         transient=False)
        self.live.start()

    def stop(self):
        if self.live:
            self.live.update(self.render(), refresh=False)
            self.live.stop()
            self.live = None

    def update(self, label, depth=0, activity="waiting", chunks=0,
               status="running", cost=None):
        state = self.states.setdefault(label, {
            "depth": depth, "started": time.monotonic(), "duration": None,
            "status": "running", "activity": "starting", "chunks": 0,
            "cost": None,
        })
        state.update(depth=depth, activity=activity, chunks=chunks, status=status)
        if cost is not None:
            state["cost"] = cost
        if self.live:
            self.live.update(self.render(), refresh=True)

    def finish(self, label, status="done", activity="completed"):
        state = self.states.get(label)
        if not state:
            return
        state["duration"] = time.monotonic() - state["started"]
        state["status"] = status
        state["activity"] = activity
        if self.live:
            self.live.update(self.render(), refresh=True)


class TurnTrace:
    """A readable execution tree plus detailed provider events for one user turn."""

    def __init__(self, base, user_input, workspace):
        stamp = datetime.now(timezone.utc)
        self.turn_id = f"turn_{stamp.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
        self.topic = self.make_topic(user_input)
        self.topic_slug = self.make_slug(self.topic)
        self.directory = base / stamp.date().isoformat() / f"{self.turn_id}__{self.topic_slug}"
        self.directory.mkdir(parents=True, exist_ok=False)
        self.started_at = time.monotonic()
        self.started = utc_now()
        self.user_input = self.preview(user_input)
        self.workspace = str(workspace)
        self.events = []
        self.tasks = {}
        self.root_task_id = None
        self.status = "running"
        self.error_type = None

    @staticmethod
    def preview(value, limit=500):
        value = " ".join(value.split())
        return value if len(value) <= limit else value[:limit - 1] + "…"

    @classmethod
    def make_topic(cls, value, limit=80):
        text = cls.preview(value, limit=limit * 2)
        first_sentence = re.split(r"[.!?\n]", text, maxsplit=1)[0].strip()
        words = first_sentence.split()[:10]
        topic = " ".join(words) or "untitled request"
        return topic if len(topic) <= limit else topic[:limit - 1].rstrip() + "…"

    @staticmethod
    def make_slug(topic, limit=60):
        slug = re.sub(r"[^\w-]+", "_", topic.lower(), flags=re.UNICODE)
        slug = re.sub(r"_+", "_", slug).strip("_-")
        return (slug[:limit].rstrip("_-") or "untitled")

    @staticmethod
    def new_id(prefix):
        return f"{prefix}_{uuid.uuid4().hex[:10]}"

    def write_json(self, name, value):
        path = self.directory / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(path)

    def event(self, event_type, **details):
        self.events.append({"time": utc_now(), "event": event_type, **details})
        self.write_json("events.json", self.events)

    def start_task(self, agent, description, parent_task_id=None):
        task_id = self.new_id("task")
        depth = self.tasks[parent_task_id]["depth"] + 1 if parent_task_id else 0
        self.tasks[task_id] = {
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "agent": agent,
            "description": self.preview(description),
            "status": "running",
            "started_at": utc_now(),
            "duration_seconds": None,
            "usage": {"api_calls": 0, "prompt_tokens": 0,
                      "completion_tokens": 0, "total_tokens": 0},
            "metrics": {"tool_calls": 0, "tool_failures": 0,
                        "read_file_calls": 0, "lines_read": 0,
                        "characters_read": 0, "list_files_calls": 0,
                        "entries_listed": 0, "search_text_calls": 0,
                        "files_searched": 0, "matches_returned": 0,
                        "delegations": 0},
            "children": [],
            "depth": depth,
            "_started": time.monotonic(),
        }
        if parent_task_id:
            self.tasks[parent_task_id]["children"].append(task_id)
        else:
            self.root_task_id = task_id
        self.event("task_started", task_id=task_id,
                   parent_task_id=parent_task_id, agent=agent,
                   description=self.tasks[task_id]["description"])
        self.flush_summary()
        return task_id

    def finish_task(self, task_id, status, error_type=None):
        task = self.tasks[task_id]
        task["status"] = status
        task["duration_seconds"] = round(time.monotonic() - task["_started"], 1)
        if error_type:
            task["error_type"] = error_type
        self.event("task_finished", task_id=task_id, status=status,
                   duration_seconds=task["duration_seconds"], error_type=error_type)
        self.flush_summary()

    def record_request(self, task_id, request_id, provider_response_id, provider,
                       requested_model, actual_model, duration, finish_reason,
                       usage, error_type, request_metrics, response_metrics):
        task_usage = self.tasks[task_id]["usage"]
        task_usage["api_calls"] += 1
        if usage:
            for source, target in (("prompt_tokens", "prompt_tokens"),
                                   ("completion_tokens", "completion_tokens"),
                                   ("total_tokens", "total_tokens")):
                task_usage[target] += usage.get(source, 0) or 0
        self.event("model_request_finished", task_id=task_id,
                   request_id=request_id,
                   provider_response_id=provider_response_id,
                   provider=provider, requested_model=requested_model,
                   actual_model=actual_model,
                   duration_seconds=round(duration, 1),
                   finish_reason=finish_reason, usage=usage,
                   request=request_metrics, response=response_metrics,
                   error_type=error_type)
        self.flush_summary()

    def record_tool(self, task_id, agent, tool_event_id, provider_tool_call_id,
                    name, arguments, duration, ok, result_summary=None,
                    error_type=None):
        metrics = self.tasks[task_id]["metrics"]
        metrics["tool_calls"] += 1
        if not ok:
            metrics["tool_failures"] += 1
        elif name == "read_file":
            metrics["read_file_calls"] += 1
            metrics["lines_read"] += result_summary.get("returned_lines", 0)
            metrics["characters_read"] += result_summary.get("returned_characters", 0)
        elif name == "list_files":
            metrics["list_files_calls"] += 1
            metrics["entries_listed"] += result_summary.get("returned_entries", 0)
        elif name == "search_text":
            metrics["search_text_calls"] += 1
            metrics["files_searched"] += result_summary.get("files_scanned", 0)
            metrics["matches_returned"] += result_summary.get("matches_returned", 0)
        elif name == "delegate_task":
            metrics["delegations"] += 1
        self.event("tool_finished", task_id=task_id, agent=agent,
                   tool_event_id=tool_event_id,
                   provider_tool_call_id=provider_tool_call_id,
                   tool=name, arguments=arguments, ok=ok,
                   duration_seconds=round(duration, 3),
                   result=result_summary, error_type=error_type)
        self.flush_summary()

    def save_final_answer(self, answer):
        (self.directory / "answer.md").write_text(answer.rstrip() + "\n",
                                                   encoding="utf-8")
        self.event("answer_saved", characters=len(answer), file="answer.md")
        self.flush_summary()

    def task_tree(self, task_id):
        source = self.tasks[task_id]
        task = {key: value for key, value in source.items()
                if key not in {"children", "depth", "_started"}}
        task["children"] = [self.task_tree(child) for child in source["children"]]
        return task

    def flush_summary(self):
        totals = {"api_calls": 0, "prompt_tokens": 0,
                  "completion_tokens": 0, "total_tokens": 0}
        for task in self.tasks.values():
            for name in totals:
                totals[name] += task["usage"][name]
        summary = {
            "turn_id": self.turn_id,
            "status": self.status,
            "started_at": self.started,
            "duration_seconds": round(time.monotonic() - self.started_at, 1),
            "topic": self.topic,
            "request": self.user_input,
            "workspace": self.workspace,
            "usage": totals,
            "answer_file": "answer.md" if (self.directory / "answer.md").exists() else None,
            "root_task": self.task_tree(self.root_task_id) if self.root_task_id else None,
        }
        if self.error_type:
            summary["error_type"] = self.error_type
        self.write_json("summary.json", summary)

    def finish(self, status, error_type=None):
        self.status = status
        self.error_type = error_type
        self.event("turn_finished", turn_id=self.turn_id, status=status,
                   duration_seconds=round(time.monotonic() - self.started_at, 1),
                   error_type=error_type)
        self.flush_summary()


class Orchestra:
    def __init__(self, config, workspace, settings, prompts, pricing):
        self.config = config
        self.settings = settings
        self.prompts = prompts
        self.pricing = pricing
        validate_settings(settings)
        validate_prompts(prompts)
        for role in ("director", "worker"):
            self.role_settings(role)
        self.files = ProjectTools(workspace)
        self.clients = {}
        self.dashboard = AgentDashboard()
        self.logs_path = BASE / "logs"
        self.logs_path.mkdir(exist_ok=True)

    def role_settings(self, role):
        settings = self.config.get("roles", {}).get(role)
        if not isinstance(settings, dict):
            raise ValueError(f"Configure roles.{role} with provider and model.")
        provider = settings.get("provider")
        model = settings.get("model")
        if not isinstance(provider, str) or provider not in self.config.get("providers", {}):
            raise ValueError(f"roles.{role}.provider must reference a configured provider.")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"roles.{role}.model must be a non-empty string.")
        return settings

    def begin_usage(self, turn_usage, label, role, provider, model):
        entry = turn_usage.setdefault(label, {
            "role": role, "provider": provider, "model": model,
            "api_calls": 0, "reported_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0,
        })
        entry["api_calls"] += 1
        return entry

    @staticmethod
    def add_usage(entry, usage, actual_model):
        if usage is None:
            return
        entry["reported_calls"] += 1
        entry["model"] = actual_model or entry["model"]
        entry["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
        entry["completion_tokens"] += usage.get("completion_tokens", 0) or 0
        entry["total_tokens"] += usage.get("total_tokens", 0) or 0
        entry["reasoning_tokens"] += (usage.get("completion_tokens_details", {})
            .get("reasoning_tokens", 0) or 0)
        entry["cached_tokens"] += (usage.get("prompt_tokens_details", {})
            .get("cached_tokens", 0) or 0)

    def estimated_cost(self, entry):
        rates = (self.pricing.get("providers", {}).get(entry["provider"], {})
                 .get(entry["model"]))
        if not rates:
            return None
        uncached = max(0, entry["prompt_tokens"] - entry["cached_tokens"])
        return ((uncached * rates["input_cache_miss"]
                 + entry["cached_tokens"] * rates["input_cache_hit"]
                 + entry["completion_tokens"] * rates["output"]) / 1_000_000)

    @staticmethod
    def message_characters(messages):
        total = 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                total += len(content)
            elif content is not None:
                total += len(json.dumps(content, ensure_ascii=False))
        return total

    @staticmethod
    def tool_result_summary(name, result):
        if name == "read_file":
            content = result.get("content", "")
            return {"path": result.get("path"),
                    "total_file_lines": result.get("total_lines"),
                    "returned_lines": content.count("\n") + bool(content),
                    "returned_characters": len(content),
                    "truncated": result.get("truncated", False)}
        if name == "list_files":
            return {"path": result.get("path"),
                    "returned_entries": result.get("entries", 0),
                    "files": result.get("files", 0),
                    "directories": result.get("directories", 0),
                    "tree_characters": len(result.get("tree", "")),
                    "max_depth": result.get("max_depth"),
                    "truncated": result.get("truncated", False),
                    "continuation_paths": result.get("continuation_paths", [])}
        if name == "search_text":
            return {"query": result.get("query"), "path": result.get("path"),
                    "file_pattern": result.get("file_pattern"),
                    "context_lines": result.get("context_lines", 0),
                    "matches_returned": result.get("matches_returned", 0),
                    "total_matches": result.get("total_matches", 0),
                    "files_with_matches": result.get("files_with_matches", 0),
                    "files_scanned": result.get("files_scanned", 0),
                    "files_skipped": result.get("files_skipped", 0),
                    "output_characters": len(result.get("matches", "")),
                    "truncated": result.get("truncated", False),
                    "has_next_cursor": result.get("next_cursor") is not None}
        if name == "delegate_task":
            return {"child_task_id": result.get("task_id"),
                    "answer_characters": len(result.get("answer", ""))}
        return {"result_type": type(result).__name__}

    def print_usage_summary(self, turn_usage):
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
            cost = self.estimated_cost(entry)
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

    async def request(self, role, messages, tools, resource_status, turn_usage,
                      trace, task_id, label=None, force_final=False):
        settings = self.role_settings(role)
        label = label or role
        provider_name = settings["provider"]
        usage_entry = self.begin_usage(turn_usage, label, role,
                                       provider_name, settings["model"])
        if provider_name not in self.clients:
            provider = self.config["providers"][provider_name]
            self.clients[provider_name] = AsyncOpenAI(api_key=provider["api_key"],
                base_url=provider["base_url"],
                timeout=self.settings["api"]["timeout_seconds"],
                max_retries=self.settings["api"]["max_retries"])
        start = time.monotonic()
        request_id = trace.new_id("req")
        content, reasoning, calls = [], [], {}
        usage = response_id = actual_model = finish = None
        error_type = None
        received = 0
        request_messages = [*messages, {"role": "system",
            "content": resource_status}]
        if force_final:
            request_messages.append({"role": "system",
                "content": self.prompts["final_response"]})
        request_metrics = {"messages": len(request_messages),
                           "characters": self.message_characters(request_messages),
                           "tools_available": len(tools) if not force_final else 0,
                           "forced_final": force_final}
        depth = trace.tasks[task_id]["depth"]
        self.dashboard.update(label, depth, "waiting for model", 0)
        try:
            request_args = dict(
                model=settings["model"], messages=request_messages,
                **self.settings["generation"],
                stream=True, stream_options={"include_usage": True},
            )
            if tools and not force_final:
                request_args["tools"] = tools
            stream = await self.clients[provider_name].chat.completions.create(**request_args)
            async with stream:
                async for chunk in stream:
                    response_id, actual_model = chunk.id, chunk.model
                    if chunk.usage is not None:
                        usage = chunk.usage.model_dump(mode="json", exclude_none=True)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta.content:
                        content.append(delta.content)
                    thought = getattr(delta, "reasoning_content", None)
                    if thought:
                        reasoning.append(thought)
                    for part in delta.tool_calls or []:
                        call = calls.setdefault(part.index, {"id": "", "type": "function",
                            "function": {"name": "", "arguments": ""}})
                        if part.id:
                            call["id"] = part.id
                        if part.function:
                            call["function"]["name"] += part.function.name or ""
                            call["function"]["arguments"] += part.function.arguments or ""
                    received += 1
                    if content:
                        phase = "writing answer"
                    elif reasoning:
                        phase = "reasoning"
                    else:
                        phase = "receiving stream"
                    self.dashboard.update(label, depth, phase, received)
                    if choice.finish_reason is not None:
                        finish = choice.finish_reason
            partial = finish == "length" and bool(content) and not calls
            if finish not in {"stop", "tool_calls"} and not partial:
                raise RuntimeError(f"Incomplete response ({finish}); no tools executed.")
            message = {"role": "assistant", "content": "".join(content) or None}
            if partial:
                message["_partial"] = True
            if reasoning:
                message["reasoning_content"] = "".join(reasoning)
            if calls:
                if finish != "tool_calls" or any(not c["id"] or not c["function"]["name"] for c in calls.values()):
                    raise RuntimeError("Incomplete tool call; no tools executed.")
                message["tool_calls"] = [calls[i] for i in sorted(calls)]
            return message
        except Exception as error:
            error_type = type(error).__name__
            raise
        finally:
            duration = time.monotonic() - start
            activity = "request failed" if error_type else "response received"
            self.add_usage(usage_entry, usage, actual_model)
            current_cost = self.estimated_cost(usage_entry) if usage is not None else None
            self.dashboard.update(label, depth, activity, received,
                                  "failed" if error_type else "running",
                                  cost=current_cost)
            response_metrics = {"chunks": received,
                                "answer_characters": sum(map(len, content)),
                                "reasoning_characters": sum(map(len, reasoning)),
                                "tool_calls": len(calls),
                                "tool_names": [call["function"]["name"]
                                               for call in calls.values()]}
            trace.record_request(task_id, request_id, response_id, provider_name,
                settings["model"], actual_model, duration, finish, usage, error_type,
                request_metrics, response_metrics)

    async def run(self, role, messages, budget, turn_usage, trace, task_id,
                  max_delegations=0, max_calls=None, label=None):
        self.role_settings(role)
        label = label or role
        if role != "director" and max_delegations != 0:
            raise ValueError("Workers cannot delegate tasks.")
        tools = FILE_TOOLS + ([DELEGATE_TOOL] if role == "director" and max_delegations > 0 else [])
        maximum_steps = self.settings["execution"]["max_steps_per_agent"]
        call_limit = min(max_calls or maximum_steps, maximum_steps)
        reserved = self.settings["execution"]["reserved_director_calls"]
        for step in range(call_limit):
            if budget["calls"] <= 0:
                raise RuntimeError("Shared API call limit reached.")
            force_final = (step == call_limit - 1
                or (role == "director" and budget["calls"] <= reserved))
            prior_usage = turn_usage.get(label, {})
            recommended_output = (self.settings["execution"]["final_answer_target_tokens"]
                                  if force_final else
                                  self.settings["generation"]["max_completion_tokens"])
            resource_status = self.prompts["resource_status"].format(
                shared_calls_after=budget["calls"] - 1,
                agent_calls_after=call_limit - step - 1,
                delegations_remaining=max_delegations,
                tools_available="yes" if tools and not force_final else "no",
                max_completion_tokens=self.settings["generation"]["max_completion_tokens"],
                recommended_output_tokens=recommended_output,
                agent_input_tokens_used=prior_usage.get("prompt_tokens", 0),
                agent_output_tokens_used=prior_usage.get("completion_tokens", 0),
                agent_api_calls_used=prior_usage.get("api_calls", 0),
            )
            budget["calls"] -= 1
            message = await self.request(role, messages, tools, resource_status,
                                         turn_usage, trace, task_id, label, force_final)
            partial = message.pop("_partial", False)
            messages.append(message)
            calls = message.get("tool_calls", [])
            if partial:
                return {"answer": message.get("content"), "status": "partial"}
            if not calls:
                return {"answer": message.get("content") or "No answer text received.",
                        "status": "completed"}
            delegate_calls = [call for call in calls
                              if call["function"]["name"] == "delegate_task"]
            allowed_delegates = min(len(delegate_calls), max_delegations)
            available_worker_calls = max(0, budget["calls"] - reserved)
            calls_per_worker = (available_worker_calls // allowed_delegates
                                if allowed_delegates else 0)
            calls_per_worker = min(calls_per_worker, maximum_steps)
            delegate_labels = {}
            for call in delegate_calls[:allowed_delegates]:
                number = budget.setdefault("next_worker_id", 1)
                delegate_labels[id(call)] = f"worker-{number}"
                budget["next_worker_id"] = number + 1
            max_delegations -= allowed_delegates

            async def execute(call):
                name = call["function"]["name"]
                tool_started = time.monotonic()
                tool_event_id = trace.new_id("tool")
                args = {"raw": TurnTrace.preview(call["function"]["arguments"], 500)}
                result_summary = None
                tool_error = None
                self.dashboard.update(label, trace.tasks[task_id]["depth"],
                                      f"tool · {name}")
                trace.event("tool_started", task_id=task_id, agent=label,
                            tool_event_id=tool_event_id,
                            provider_tool_call_id=call["id"], tool=name)
                try:
                    args = parse_arguments(call["function"]["arguments"])
                    if name == "delegate_task" and role == "director":
                        limit = self.settings["execution"]["max_delegated_task_characters"]
                        if set(args) != {"task"} or not isinstance(args["task"], str) or not 1 <= len(args["task"]) <= limit:
                            raise ValueError("Task is empty or exceeds the configured character limit.")
                        worker_label = delegate_labels.get(id(call))
                        if worker_label is None:
                            raise ValueError("Delegation limit reached for this turn.")
                        if calls_per_worker < 1:
                            raise RuntimeError("Not enough shared API budget to start a worker while reserving the director's final response.")
                        worker_messages = [{"role": "system", "content": self.prompts["worker"]},
                                           {"role": "user", "content": args["task"]}]
                        worker_task_id = trace.start_task(worker_label, args["task"], task_id)
                        try:
                            worker_result = await self.run("worker", worker_messages,
                                budget, turn_usage, trace, worker_task_id,
                                max_calls=calls_per_worker, label=worker_label)
                        except BaseException as worker_error:
                            self.dashboard.finish(worker_label, "failed",
                                                  type(worker_error).__name__)
                            trace.finish_task(worker_task_id, "failed",
                                              type(worker_error).__name__)
                            raise
                        else:
                            worker_status = worker_result["status"]
                            activity = ("output limit reached" if worker_status == "partial"
                                        else "completed")
                            dashboard_status = "partial" if worker_status == "partial" else "done"
                            self.dashboard.finish(worker_label, dashboard_status, activity)
                            trace.finish_task(worker_task_id, worker_status)
                        result = {**worker_result, "task_id": worker_task_id}
                    else:
                        result = self.files.execute(name, args)
                    result_summary = self.tool_result_summary(name, result)
                    output = {"ok": True, "result": result}
                except Exception as error:
                    tool_error = type(error).__name__
                    self.dashboard.update(label, trace.tasks[task_id]["depth"],
                                          f"tool failed · {name}")
                    # Do not expose provider error bodies, credentials or local absolute paths.
                    output = {"ok": False, "error": type(error).__name__,
                              "message": "Tool failed. Check arguments, path, permissions or execution limits."}
                trace.record_tool(task_id, label, tool_event_id, call["id"], name,
                                  args, time.monotonic() - tool_started,
                                  output["ok"], result_summary, tool_error)
                return {"role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps(output, ensure_ascii=False)}

            results = await asyncio.gather(*(execute(call) for call in calls))
            messages.extend(results)
        raise RuntimeError("Agent call limit reached without a final response.")


def validate_settings(settings):
    ranges = {
        ("api", "timeout_seconds"): (0, None, False, False),
        ("api", "max_retries"): (0, None, True, True),
        ("generation", "max_completion_tokens"): (1, None, True, True),
        ("generation", "temperature"): (0, 2, False, True),
        ("generation", "top_p"): (0, 1, False, False),
        ("execution", "max_api_calls_per_turn"): (1, None, True, True),
        ("execution", "max_steps_per_agent"): (1, None, True, True),
        ("execution", "reserved_director_calls"): (1, None, True, True),
        ("execution", "final_answer_target_tokens"): (1, None, True, True),
        ("execution", "max_delegated_task_characters"): (1, None, True, True),
    }
    import math
    for (section, name), (low, high, integer, inclusive) in ranges.items():
        value = settings.get(section, {}).get(name)
        if (type(value) not in ((int,) if integer else (int, float))
                or not math.isfinite(value)
                or (value < low if inclusive else value <= low)
                or (high is not None and value > high)):
            raise ValueError(f"Invalid settings: {section}.{name}")
    if set(settings["generation"]) != {"max_completion_tokens", "temperature", "top_p"}:
        raise ValueError("Unexpected generation setting.")
    for role in ("director", "worker"):
        value = settings.get("roles", {}).get(role, {}).get("max_delegations", 0)
        if type(value) is not int or value < 0 or (role == "worker" and value != 0):
            raise ValueError(f"Invalid settings: roles.{role}.max_delegations")
    if settings["execution"]["reserved_director_calls"] >= settings["execution"]["max_api_calls_per_turn"]:
        raise ValueError("reserved_director_calls must be smaller than max_api_calls_per_turn.")
    if settings["execution"]["final_answer_target_tokens"] >= settings["generation"]["max_completion_tokens"]:
        raise ValueError("final_answer_target_tokens must be smaller than max_completion_tokens.")


def validate_prompts(prompts):
    required = {"director", "worker", "final_response", "resource_status"}
    if not isinstance(prompts, dict) or set(prompts) != required:
        raise ValueError(f"prompts.json must contain exactly: {', '.join(sorted(required))}.")
    for name, prompt in prompts.items():
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"Prompt '{name}' must be a non-empty string.")
    try:
        prompts["resource_status"].format(
            shared_calls_after=0, agent_calls_after=0,
            delegations_remaining=0, tools_available="no",
            max_completion_tokens=2, recommended_output_tokens=1,
            agent_input_tokens_used=0, agent_output_tokens_used=0,
            agent_api_calls_used=0)
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError(f"Invalid resource_status template: {error}") from error


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=BASE / "workspace")
    args = parser.parse_args()
    with (BASE / "config.local.json").open(encoding="utf-8") as file:
        config = json.load(file)
    with (BASE / "settings.json").open(encoding="utf-8") as file:
        runtime_settings = json.load(file)
    with (BASE / "prompts.json").open(encoding="utf-8") as file:
        prompts = json.load(file)
    with (BASE / "pricing.json").open(encoding="utf-8") as file:
        pricing = json.load(file)
    if args.workspace == BASE / "workspace":
        args.workspace.mkdir(exist_ok=True)
    app = Orchestra(config, args.workspace, runtime_settings, prompts, pricing)
    print(f"Read-only workspace: {app.files.root}")
    for role in ("director", "worker"):
        settings = app.role_settings(role)
        print(f"{role}: {settings['provider']} / {settings['model']}")
    messages = [{"role": "system", "content": prompts["director"]}]
    while True:
        try:
            text = input("\nYou (exit to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() == "exit":
            break
        if not text:
            continue
        checkpoint = len(messages)
        messages.append({"role": "user", "content": text})
        turn_usage = {}
        trace = TurnTrace(app.logs_path, text, app.files.root)
        root_task_id = trace.start_task("director", text)
        app.dashboard.start(trace.turn_id, trace.topic)
        turn_status = "failed"
        turn_error = None
        try:
            director_result = await app.run("director", messages,
                {"calls": runtime_settings["execution"]["max_api_calls_per_turn"]},
                turn_usage, trace, root_task_id,
                max_delegations=runtime_settings.get("roles", {}).get("director", {}).get("max_delegations", 0))
            answer = director_result["answer"]
            result_status = director_result["status"]
            trace.save_final_answer(answer)
            trace.finish_task(root_task_id, result_status)
            activity = "output limit reached" if result_status == "partial" else "completed"
            dashboard_status = "partial" if result_status == "partial" else "done"
            app.dashboard.finish("director", dashboard_status, activity)
            app.dashboard.stop()
            turn_status = result_status
            print(f"\nAssistant:\n{answer}")
            if result_status == "partial":
                print("\n[Output limit reached; partial answer preserved.]")
        except Exception as error:
            turn_error = type(error).__name__
            app.dashboard.finish("director", "failed", turn_error)
            app.dashboard.stop()
            trace.finish_task(root_task_id, "failed", turn_error)
            del messages[checkpoint:]
            print(f"Turn failed ({turn_error}). Conversation restored.")
        except KeyboardInterrupt:
            turn_status = "cancelled"
            turn_error = "KeyboardInterrupt"
            app.dashboard.finish("director", "failed", "cancelled")
            app.dashboard.stop()
            trace.finish_task(root_task_id, "cancelled", turn_error)
            del messages[checkpoint:]
            print("\nTurn cancelled. Conversation restored.")
        finally:
            app.dashboard.stop()
            trace.finish(turn_status, turn_error)
            app.print_usage_summary(turn_usage)
            print(f"  Log: {trace.directory}")


if __name__ == "__main__":
    asyncio.run(main())
