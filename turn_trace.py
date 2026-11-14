"""On-disk execution trace for a user turn."""
import json
import logging
import re
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone


logger = logging.getLogger(__name__)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TurnTrace:
    """A readable execution tree plus detailed provider events for one user turn."""

    def __init__(self, base, user_input, workspace, *, on_event=None, on_progress=None):
        self.on_event = on_event
        self.on_progress = on_progress
        self.cost = None
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
        self.error_message = None

    @staticmethod
    def preview(value, limit=500):
        value = " ".join(value.split())
        return value if len(value) <= limit else value[:limit - 1] + "â€¦"

    @classmethod
    def make_topic(cls, value, limit=80):
        text = cls.preview(value, limit=limit * 2)
        first_sentence = re.split(r"[.!?\n]", text, maxsplit=1)[0].strip()
        words = first_sentence.split()[:10]
        topic = " ".join(words) or "untitled request"
        return topic if len(topic) <= limit else topic[:limit - 1].rstrip() + "â€¦"

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
        event = {"time": utc_now(), "event": event_type, **details}
        self.events.append(event)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.notify(self.on_event, event)

    def progress(self, kind, **details):
        self.notify(self.on_progress, {"kind": kind, **details})

    def notify(self, callback, event):
        if callback is not None:
            try:
                callback(deepcopy({"turn_id": self.turn_id, **event}))
            except Exception:
                logger.exception("Turn event callback failed")

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
        if self.cost is not None:
            summary['cost'] = self.cost
        if self.error_message:
            summary["error_message"] = self.error_message
        self.write_json("summary.json", summary)

    def finish(self, status, error_type=None):
        self.status = status
        self.error_type = error_type
        self.event("turn_finished", turn_id=self.turn_id, status=status,
                   duration_seconds=round(time.monotonic() - self.started_at, 1),
                   error_type=error_type)
        self.flush_summary()
        self.write_json("events.json", self.events)
