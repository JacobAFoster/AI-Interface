"""Batch editor: clone a folder, apply LLM tasks to files inside the clone,
validate, and write. Runs on a background thread; emits events to a callback.

Auto-progress mode:
  When validation fails, the runner does NOT pause — it cycles through a
  sequence of "approaches" (retry strategies), feeding the validation failures
  back to the LLM each time. Only after all approaches are exhausted does it
  pause for manual review. If the user rejects the exhausted attempt with
  feedback, it gets one final user-guided attempt before failing the task.

Manual mode:
  Every validation outcome is surfaced to the user; they approve, reject with
  feedback, or skip. No approach cycling.
"""
import os
import re
import ast
import json
import queue
import shutil
import fnmatch
import difflib
import threading
import traceback
import tempfile
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional


class EventType(Enum):
    BATCH_STARTED = "batch_started"
    BATCH_COMPLETED = "batch_completed"
    BATCH_ABORTED = "batch_aborted"
    TASK_STARTED = "task_started"
    TASK_STREAM = "task_stream"
    TASK_RETRYING = "task_retrying"     # auto mode: validation failed, switching approach
    TASK_READY = "task_ready"           # pause for user review (manual or post-exhaustion)
    TASK_APPLIED = "task_applied"
    TASK_REJECTED = "task_rejected"
    TASK_SKIPPED = "task_skipped"
    TASK_FAILED = "task_failed"
    LOG = "log"


@dataclass
class BatchTask:
    description: str
    target_file: str
    status: str = "pending"  # pending|running|ready|applied|skipped|failed
    attempts: int = 0
    result_notes: str = ""


@dataclass
class BatchConfig:
    source_folder: str
    clone_folder: str
    tasks: list = field(default_factory=list)
    auto_progress: bool = False
    checks: list = field(default_factory=lambda: ["syntax", "not_identical", "diff_bounded"])
    max_retries: int = 1                  # manual mode only
    max_diff_ratio: float = 0.6
    ignore_patterns: list = field(default_factory=lambda: [
        "__pycache__", "*.pyc", ".git", ".hg", ".svn",
        "node_modules", ".venv", "venv", "env",
        ".batch", ".idea", ".vscode", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", "*.log",
    ])


class BatchAborted(Exception):
    pass


class BatchRunner:
    """Runs tasks sequentially against a cloned folder. Threaded."""

    REVIEW_TIMEOUT_S = 90  # hard cap for the optional LLM review check

    def __init__(self, config: BatchConfig,
                 llm_fn: Callable[[str, Callable[[str], None]], str],
                 on_event: Callable[[EventType, dict], None]):
        self._config = config
        self._llm = llm_fn
        self._on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self._abort = threading.Event()
        self._pause = threading.Event()
        self._action_queue: "queue.Queue[tuple]" = queue.Queue()
        self._state_lock = threading.Lock()
        self._awaiting_action = False

    # ---- Public API --------------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._abort.clear()
        self._pause.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._abort.set()
        self._action_queue.put(("abort", None))

    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def is_awaiting_action(self) -> bool:
        with self._state_lock:
            return self._awaiting_action

    def approve(self, edited_content: Optional[str] = None):
        self._action_queue.put(("approve", edited_content))

    def reject(self, feedback: str):
        self._action_queue.put(("reject", feedback))

    def skip(self):
        self._action_queue.put(("skip", None))

    # ---- Internals ---------------------------------------------------------

    def _emit(self, event_type: EventType, payload: dict):
        try:
            self._on_event(event_type, payload)
        except Exception:
            traceback.print_exc()

    def _log(self, msg: str):
        self._emit(EventType.LOG, {"msg": msg,
                                   "ts": datetime.now().strftime("%H:%M:%S")})

    def _wait_for_action(self):
        with self._state_lock:
            self._awaiting_action = True
        try:
            while True:
                if self._abort.is_set():
                    return ("abort", None)
                try:
                    return self._action_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
        finally:
            with self._state_lock:
                self._awaiting_action = False

    def _run(self):
        try:
            self._clone_folder()
            self._emit(EventType.BATCH_STARTED, {
                "source": self._config.source_folder,
                "clone": self._config.clone_folder,
                "task_count": len(self._config.tasks),
                "auto_progress": self._config.auto_progress,
                "approaches": [name for name, _ in self._approaches()]
                             if self._config.auto_progress else [],
            })
            if self._config.auto_progress:
                self._log(
                    f"Auto-progress ON. Each task will try up to "
                    f"{len(self._approaches())} approach(es) before pausing "
                    f"for review, plus one user-guided retry if you reject.")
            for idx, task in enumerate(self._config.tasks):
                while self._pause.is_set() and not self._abort.is_set():
                    threading.Event().wait(0.2)
                if self._abort.is_set():
                    raise BatchAborted()
                self._run_task(idx, task)
            if self._abort.is_set():
                raise BatchAborted()
            self._emit(EventType.BATCH_COMPLETED, {
                "applied": sum(1 for t in self._config.tasks if t.status == "applied"),
                "skipped": sum(1 for t in self._config.tasks if t.status == "skipped"),
                "failed":  sum(1 for t in self._config.tasks if t.status == "failed"),
                "total":   len(self._config.tasks),
            })
        except BatchAborted:
            self._emit(EventType.BATCH_ABORTED, {"reason": "User aborted"})
        except Exception as e:
            traceback.print_exc()
            self._emit(EventType.BATCH_ABORTED, {"reason": f"Fatal error: {e}"})

    def _clone_folder(self):
        src = self._config.source_folder
        dst = self._config.clone_folder
        if not os.path.isdir(src):
            raise ValueError(f"Source folder does not exist: {src}")
        if os.path.abspath(src) == os.path.abspath(dst):
            raise ValueError("Source and clone folders must differ")
        if os.path.exists(dst) and os.listdir(dst):
            raise ValueError(f"Clone folder exists and is not empty: {dst}")

        patterns = self._config.ignore_patterns

        def _ignore(dirpath, names):
            skipped = []
            for n in names:
                full = os.path.join(dirpath, n)
                rel = os.path.relpath(full, src)
                for pat in patterns:
                    if fnmatch.fnmatch(n, pat) or fnmatch.fnmatch(rel, pat):
                        skipped.append(n)
                        break
            return skipped

        os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
        shutil.copytree(src, dst, ignore=_ignore, dirs_exist_ok=True)
        self._log(f"Cloned {src} → {dst}")

    # ---- Task dispatch -----------------------------------------------------

    def _run_task(self, idx: int, task: BatchTask):
        task.status = "running"
        self._emit(EventType.TASK_STARTED, {"idx": idx, "task": task.description})

        clone = self._config.clone_folder
        file_path = os.path.join(clone, task.target_file)
        if not os.path.isfile(file_path):
            task.status = "failed"
            task.result_notes = "target file not found in clone"
            self._emit(EventType.TASK_FAILED, {
                "idx": idx, "reason": task.result_notes, "attempts": 0,
            })
            return

        try:
            old_content = open(file_path, "r", encoding="utf-8").read()
        except Exception as e:
            task.status = "failed"
            task.result_notes = f"read error: {e}"
            self._emit(EventType.TASK_FAILED, {
                "idx": idx, "reason": task.result_notes, "attempts": 0,
            })
            return

        if self._config.auto_progress:
            self._run_task_auto(idx, task, old_content, file_path)
        else:
            self._run_task_manual(idx, task, old_content, file_path)

    # ---- Approach list -----------------------------------------------------

    def _approaches(self) -> list:
        """Ordered retry strategies for auto mode.

        Each tuple is (name, hint). The hint is added to the apply prompt.
        Order matters: most likely to succeed first, permission-to-refuse last.
        """
        return [
            ("direct_fix",
             "Your previous attempt did not pass validation. Fix exactly the "
             "issues listed below and return the full file. Do not change "
             "unrelated code."),
            ("minimal_edit",
             "Your previous attempt did not pass validation. Try a smaller, "
             "more surgical change: modify only the minimal lines needed for "
             "the task. Preserve everything else byte-for-byte."),
            ("alternative",
             "Your previous attempt did not pass validation. Re-read the "
             "original file carefully and try a DIFFERENT implementation "
             "strategy for the same task. If the task is genuinely impossible "
             "or unsafe to apply, refuse in prose and do NOT emit a code "
             "block."),
            ("strict",
             "Your previous attempt did not pass validation. Return the ENTIRE "
             "file from line 1 to the end with no omissions, no placeholders, "
             "no `...` markers, and no truncation. The output must be "
             "syntactically valid on its own."),
        ]

    def _build_validation_feedback(self, validation: Optional[list]) -> Optional[str]:
        if not validation:
            return None
        failed = [v for v in validation if not v.get("passed")]
        if not failed:
            return None
        lines = ["Your previous attempt failed the following checks:"]
        for v in failed:
            lines.append(f"- {v['check']}: {v['detail']}")
        return "\n".join(lines)

    # ---- Auto mode: cycle approaches until validation passes ---------------

    def _run_task_auto(self, idx: int, task: BatchTask,
                       old_content: str, file_path: str):
        approaches = self._approaches()
        feedback: Optional[str] = None
        last_validation: Optional[list] = None
        last_content: Optional[str] = None

        for approach_idx, (approach_name, approach_hint) in enumerate(approaches):
            if self._abort.is_set():
                raise BatchAborted()
            task.attempts += 1
            self._log(f"Task {idx+1}: attempt {task.attempts} "
                      f"(approach={approach_name})")

            try:
                prompt = self._build_apply_prompt(
                    task, old_content, feedback, approach_hint)
                raw = self._llm(prompt, on_chunk=lambda c: self._emit(
                    EventType.TASK_STREAM, {"idx": idx, "chunk": c}))
            except Exception as e:
                self._log(f"LLM error: {e}")
                last_validation = [{
                    "check": "llm", "passed": False, "required": True,
                    "detail": f"error: {e}",
                }]
                feedback = f"Your previous attempt errored: {e}"
                continue

            new_content = self._extract_code(raw, target_file=task.target_file)
            if not new_content:
                # "alternative" is the only approach allowed to legitimately
                # refuse. Treat a refusal there as a skip, not a failure.
                if approach_name == "alternative":
                    task.status = "skipped"
                    task.result_notes = "model declined (alternative approach)"
                    self._emit(EventType.TASK_SKIPPED, {
                        "idx": idx, "reason": "model declined the task",
                    })
                    self._log(f"Task {idx+1}: model declined — skipping")
                    return
                self._log("No code block in model output")
                last_validation = [{
                    "check": "extract", "passed": False, "required": True,
                    "detail": "no code block found in model output",
                }]
                feedback = ("Your previous response did not contain a code "
                            "block. Return the full updated file inside a "
                            "single fenced code block.")
                continue

            validation = self._validate(task, old_content, new_content)
            last_validation = validation
            last_content = new_content

            all_required = all(v["passed"] for v in validation
                               if v.get("required", True))

            if all_required:
                if self._write_and_snapshot(idx, task, new_content):
                    return
                return  # write failed; _write_and_snapshot already emitted

            # ---- Validation failed: cycle to next approach -----------------
            failed_checks = [v["check"] for v in validation if not v.get("passed")]
            next_approach = (approaches[approach_idx + 1][0]
                             if approach_idx + 1 < len(approaches) else None)
            self._emit(EventType.TASK_RETRYING, {
                "idx": idx,
                "attempt": task.attempts,
                "max_attempts": len(approaches),
                "approach": approach_name,
                "next_approach": next_approach,
                "failed_checks": failed_checks,
                "validation": validation,
            })
            self._log(f"Task {idx+1}: failed ({', '.join(failed_checks)}); "
                      f"{'trying approach ' + next_approach if next_approach else 'exhausted'}")
            feedback = self._build_validation_feedback(validation)

        if self._abort.is_set():
            raise BatchAborted()

        # ---- All auto approaches exhausted: pause for manual review --------
        self._log(f"Task {idx+1}: all {len(approaches)} auto approaches "
                  f"exhausted — pausing for manual review")
        diff_text = self._compute_diff(task.target_file, old_content,
                                       last_content or "")
        self._emit(EventType.TASK_READY, {
            "idx": idx,
            "task": task.description,
            "target_file": task.target_file,
            "attempts": task.attempts,
            "max_attempts": len(approaches),
            "diff": diff_text,
            "validation": last_validation or [],
            "all_pass": False,
            "auto_progress_blocked": True,
            "auto_exhausted": True,
            "new_content": last_content or "",
        })

        action, payload = self._wait_for_action()
        if action == "abort":
            raise BatchAborted()
        if action == "skip":
            task.status = "skipped"
            task.result_notes = "user skipped after auto exhaustion"
            self._emit(EventType.TASK_SKIPPED, {"idx": idx})
            return
        if action == "approve":
            content = payload if isinstance(payload, str) else (last_content or "")
            self._write_and_snapshot(idx, task, content)
            return
        if action == "reject":
            self._log(f"Task {idx+1}: user rejected — running one final "
                      f"user-guided attempt")
            self._run_one_user_guided_attempt(
                idx, task, old_content, file_path, payload, last_validation)
            return

    def _run_one_user_guided_attempt(self, idx: int, task: BatchTask,
                                     old_content: str, file_path: str,
                                     user_feedback: str,
                                     prev_validation: Optional[list]):
        """Single final attempt driven by user feedback after auto exhaustion."""
        task.attempts += 1
        hint = ("The user reviewed the previous attempt and rejected it. "
                "Apply their feedback precisely and return the full file.")
        feedback = user_feedback or "User rejected the previous attempt."
        try:
            prompt = self._build_apply_prompt(
                task, old_content, feedback, hint)
            raw = self._llm(prompt, on_chunk=lambda c: self._emit(
                EventType.TASK_STREAM, {"idx": idx, "chunk": c}))
        except Exception as e:
            task.status = "failed"
            task.result_notes = f"user-guided attempt errored: {e}"
            self._emit(EventType.TASK_FAILED, {
                "idx": idx, "reason": task.result_notes,
                "attempts": task.attempts,
                "validation": prev_validation,
            })
            return

        new_content = self._extract_code(raw, target_file=task.target_file)
        if not new_content:
            task.status = "failed"
            task.result_notes = "no code block in user-guided attempt"
            self._emit(EventType.TASK_FAILED, {
                "idx": idx, "reason": task.result_notes,
                "attempts": task.attempts,
            })
            return

        validation = self._validate(task, old_content, new_content)
        all_required = all(v["passed"] for v in validation
                           if v.get("required", True))
        if not all_required:
            failed_checks = [v["check"] for v in validation if not v.get("passed")]
            task.status = "failed"
            task.result_notes = f"user-guided attempt failed: {', '.join(failed_checks)}"
            self._emit(EventType.TASK_FAILED, {
                "idx": idx, "reason": task.result_notes,
                "attempts": task.attempts,
                "validation": validation,
                "last_content": new_content,
            })
            return

        self._write_and_snapshot(idx, task, new_content)

    # ---- Manual mode: user decides every step -----------------------------

    def _run_task_manual(self, idx: int, task: BatchTask,
                         old_content: str, file_path: str):
        feedback: Optional[str] = None
        last_validation: Optional[list] = None
        last_content: Optional[str] = None
        max_attempts = max(1, int(self._config.max_retries) + 1)

        while task.attempts < max_attempts and not self._abort.is_set():
            task.attempts += 1
            self._log(f"Task {idx+1}: manual attempt "
                      f"{task.attempts}/{max_attempts}")

            try:
                prompt = self._build_apply_prompt(
                    task, old_content, feedback, None)
                raw = self._llm(prompt, on_chunk=lambda c: self._emit(
                    EventType.TASK_STREAM, {"idx": idx, "chunk": c}))
            except Exception as e:
                self._log(f"LLM error: {e}")
                feedback = f"Your previous attempt errored: {e}"
                last_validation = [{
                    "check": "llm", "passed": False, "required": True,
                    "detail": str(e),
                }]
                # don't emit TASK_READY for transient LLM errors — retry silently
                continue

            new_content = self._extract_code(raw, target_file=task.target_file)
            if not new_content:
                last_validation = [{
                    "check": "extract", "passed": False, "required": True,
                    "detail": "no code block found in model output",
                }]
                last_content = None
            else:
                last_validation = self._validate(task, old_content, new_content)
                last_content = new_content

            all_required = last_content is not None and all(
                v["passed"] for v in last_validation if v.get("required", True))

            if all_required:
                if self._write_and_snapshot(idx, task, last_content):
                    return
                return

            # Show user the failed attempt and let them decide
            diff_text = (self._compute_diff(task.target_file, old_content,
                                            last_content or "")
                         if last_content else "(no content produced)")
            self._emit(EventType.TASK_READY, {
                "idx": idx,
                "task": task.description,
                "target_file": task.target_file,
                "attempts": task.attempts,
                "max_attempts": max_attempts,
                "diff": diff_text,
                "validation": last_validation or [],
                "all_pass": False,
                "auto_progress_blocked": False,
                "auto_exhausted": False,
                "new_content": last_content or "",
            })

            action, payload = self._wait_for_action()
            if action == "abort":
                raise BatchAborted()
            if action == "skip":
                task.status = "skipped"
                task.result_notes = "user skipped"
                self._emit(EventType.TASK_SKIPPED, {"idx": idx})
                return
            if action == "approve":
                content = (payload if isinstance(payload, str)
                           else (last_content or ""))
                self._write_and_snapshot(idx, task, content)
                return
            if action == "reject":
                feedback = payload or "User rejected the change."
                task.result_notes = "rejected by user"
                self._emit(EventType.TASK_REJECTED, {
                    "idx": idx, "feedback": feedback,
                })
                continue

        if self._abort.is_set():
            raise BatchAborted()

        task.status = "failed"
        task.result_notes = f"exhausted {task.attempts} attempt(s)"
        self._emit(EventType.TASK_FAILED, {
            "idx": idx,
            "reason": task.result_notes,
            "attempts": task.attempts,
            "validation": last_validation,
            "last_content": last_content,
        })

    # ---- Write / snapshot --------------------------------------------------

    def _write_and_snapshot(self, idx: int, task: BatchTask, content: str) -> bool:
        clone = self._config.clone_folder
        file_path = os.path.join(clone, task.target_file)

        snap_dir = os.path.join(clone, ".batch", "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = task.target_file.replace(os.sep, "__").replace("/", "__")
        snap_path = os.path.join(snap_dir, f"{idx:03d}_{ts}_{safe}")
        try:
            if os.path.exists(file_path):
                shutil.copy2(file_path, snap_path)
        except Exception as e:
            self._log(f"Snapshot failed: {e}")

        try:
            with open(file_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except Exception as e:
            task.status = "failed"
            task.result_notes = f"write error: {e}"
            self._emit(EventType.TASK_FAILED, {
                "idx": idx, "reason": task.result_notes,
                "attempts": task.attempts,
            })
            return False

        task.status = "applied"
        task.result_notes = f"written ({len(content.splitlines())} lines)"
        self._emit(EventType.TASK_APPLIED, {
            "idx": idx,
            "target_file": task.target_file,
            "attempts": task.attempts,
            "lines": len(content.splitlines()),
        })
        return True

    # ---- Prompt building ---------------------------------------------------

    def _build_apply_prompt(self, task: BatchTask, old_content: str,
                            feedback: Optional[str],
                            approach_hint: Optional[str]) -> str:
        lang = self._language_for_file(task.target_file) or "text"
        parts = [
            f"You are editing a single file: {task.target_file}",
            "",
            "Current content:",
            f"```{lang}",
            old_content,
            "```",
            "",
            f"Task: {task.description}",
            "",
            "Apply this task. Return the FULL updated file in a single "
            f"```{lang} block.",
            "- Do not paraphrase or use `… unchanged`.",
            "- Preserve unrelated code, comments, formatting exactly.",
            "- If the task is ambiguous or impossible, reply in prose and do "
            "NOT emit a code block.",
            "- If applying the task would break the file, refuse and explain.",
        ]
        if approach_hint:
            parts += ["", approach_hint]
        if feedback:
            parts += ["", "Feedback:", feedback, "", "Correct the issue in this attempt."]
        return "\n".join(parts)

    def _language_for_file(self, path: str) -> Optional[str]:
        ext = os.path.splitext(path)[1].lower()
        return {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".json": "json", ".yml": "yaml", ".yaml": "yaml",
            ".html": "html", ".css": "css", ".md": "markdown",
            ".sh": "bash", ".bash": "bash", ".sql": "sql",
            ".rs": "rust", ".go": "go", ".java": "java",
            ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
        }.get(ext)

    # ---- Code extraction ---------------------------------------------------

    def _extract_code(self, response: str, target_file: str) -> Optional[str]:
        lang = self._language_for_file(target_file)
        blocks = []
        for m in re.finditer(r"```([a-zA-Z0-9_+-]*)\s*\n(.*?)```",
                             response, re.DOTALL):
            blocks.append((m.group(1).strip().lower(), m.group(2)))
        if not blocks:
            return None
        if lang:
            for tag, body in blocks:
                if tag == lang:
                    return body.rstrip("\n") + "\n"
        return blocks[-1][1].rstrip("\n") + "\n"

    # ---- Validation --------------------------------------------------------

    def _validate(self, task: BatchTask, old_content: str,
                  new_content: str) -> list:
        results = []
        for check in self._config.checks:
            try:
                if check == "syntax":
                    results.append(self._check_syntax(task.target_file, new_content))
                elif check == "not_identical":
                    results.append(self._check_not_identical(old_content, new_content))
                elif check == "diff_bounded":
                    results.append(self._check_diff_bounded(old_content, new_content))
                elif check == "ruff":
                    results.append(self._check_ruff(task.target_file, new_content))
                elif check == "llm_review":
                    results.append(self._check_llm_review(task, old_content, new_content))
                else:
                    results.append({"check": check, "passed": True,
                                    "required": False, "detail": "unknown check"})
            except Exception as e:
                results.append({"check": check, "passed": False,
                                "required": True, "detail": f"error: {e}"})
        return results

    def _check_syntax(self, path: str, content: str) -> dict:
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".py":
                ast.parse(content)
            elif ext == ".json":
                json.loads(content)
            else:
                return {"check": "syntax", "passed": True, "required": False,
                        "detail": f"skipped ({ext or 'no ext'})"}
            return {"check": "syntax", "passed": True, "required": True,
                    "detail": "parses"}
        except SyntaxError as e:
            return {"check": "syntax", "passed": False, "required": True,
                    "detail": f"line {e.lineno}: {e.msg}"}
        except json.JSONDecodeError as e:
            return {"check": "syntax", "passed": False, "required": True,
                    "detail": f"line {e.lineno}: {e.msg}"}
        except Exception as e:
            return {"check": "syntax", "passed": False, "required": True,
                    "detail": f"error: {e}"}

    def _check_not_identical(self, old: str, new: str) -> dict:
        if old.strip() == new.strip():
            return {"check": "not_identical", "passed": False, "required": True,
                    "detail": "no change vs input"}
        return {"check": "not_identical", "passed": True, "required": True,
                "detail": "content changed"}

    def _check_diff_bounded(self, old: str, new: str) -> dict:
        a, b = old.splitlines(), new.splitlines()
        total = max(len(a), len(b), 1)
        changed = sum(1 for line in difflib.unified_diff(a, b, lineterm="")
                      if line.startswith(("+", "-"))
                      and not line.startswith(("+++", "---")))
        ratio = changed / total
        return {"check": "diff_bounded",
                "passed": ratio <= self._config.max_diff_ratio,
                "required": True,
                "detail": f"{changed}/{total} lines ({ratio:.0%}); "
                          f"limit {self._config.max_diff_ratio:.0%}"}

    def _check_ruff(self, path: str, content: str) -> dict:
        if not path.endswith(".py"):
            return {"check": "ruff", "passed": True, "required": False,
                    "detail": "skipped (not python)"}
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
                f.write(content)
                tmp = f.name
            proc = subprocess.run(
                [sys.executable, "-m", "ruff", "check",
                 "--select", "E,F", "--output-format", "json", tmp],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                return {"check": "ruff", "passed": True, "required": False,
                        "detail": "no issues"}
            if "No module named ruff" in proc.stderr:
                return {"check": "ruff", "passed": True, "required": False,
                        "detail": "skipped (ruff not installed)"}
            try:
                issues = json.loads(proc.stdout or "[]")
            except Exception:
                issues = []
            if not issues:
                return {"check": "ruff", "passed": True, "required": False,
                        "detail": "no issues"}
            msg = "; ".join(f"{i.get('code','?')} L{i.get('location',{}).get('row','?')}"
                            for i in issues[:3])
            return {"check": "ruff", "passed": False, "required": False,
                    "detail": f"{len(issues)} issue(s): {msg}"}
        except subprocess.TimeoutExpired:
            return {"check": "ruff", "passed": True, "required": False,
                    "detail": "timeout"}
        except Exception as e:
            return {"check": "ruff", "passed": True, "required": False,
                    "detail": f"skipped ({e})"}
        finally:
            if tmp and os.path.exists(tmp):
                try: os.unlink(tmp)
                except Exception: pass

    def _check_llm_review(self, task: BatchTask, old: str, new: str) -> dict:
        lang = self._language_for_file(task.target_file) or "text"
        prompt = (
            f"Task: {task.description}\n\n"
            f"Original file:\n```{lang}\n{old}\n```\n\n"
            f"After edit:\n```{lang}\n{new}\n```\n\n"
            "Did the change implement the task correctly without introducing "
            "bugs? Reply with ONLY a JSON object: "
            '{"ok": true, "issues": []}'
        )
        try:
            # Optional check: bound it so a stuck review can't freeze the runner
            # for the full generation timeout.
            try:
                raw = self._llm(prompt, on_chunk=lambda _c: None,
                                timeout=self.REVIEW_TIMEOUT_S)
            except TypeError:
                # llm_fn without a timeout parameter
                raw = self._llm(prompt, on_chunk=lambda _c: None)
        except Exception as e:
            return {"check": "llm_review", "passed": True, "required": False,
                    "detail": f"skipped ({e})"}
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {"check": "llm_review", "passed": True, "required": False,
                    "detail": "no JSON in reply"}
        try:
            data = json.loads(m.group(0))
        except Exception:
            return {"check": "llm_review", "passed": True, "required": False,
                    "detail": "unparseable JSON"}
        ok = bool(data.get("ok"))
        issues = data.get("issues") or []
        detail = "looks correct" if ok else f"issues: {'; '.join(map(str, issues[:3]))}"
        return {"check": "llm_review", "passed": ok, "required": False,
                "detail": detail}

    # ---- Diff --------------------------------------------------------------

    def _compute_diff(self, path: str, old: str, new: str) -> str:
        diff = list(difflib.unified_diff(
            old.splitlines(), new.splitlines(),
            fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
        ))
        return "\n".join(diff) if diff else "(no changes)"