"""Run the installed Codex CLI as one ICAE coding agent.

Codex reads its normal user config and auth. CODEX_MODEL and
CODEX_REASONING_EFFORT in the selected model registry entry override its model
settings; endpoint credentials are never copied into its process environment.
JSON events are kept verbatim in .jsonl.
"""
import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path

from .runner_common import AgentResult, RateLimited, RATELIMIT_MARKERS, _contains


INACTIVITY_TIMEOUT = 900

# Codex currently runs on the host so it can invoke the task's Docker container.
# This is a guard against accidental out-of-scope commands, not a security
# boundary: Codex's command event may arrive after the shell command has started.
CODEX_SCOPE = """

## Codex host command scope

The Codex CLI is launched on the host, which also holds evaluation-only data.
Use host commands only to run `docker exec` against the container ID above and
to call the Oracle URL supplied in the PRD. Do all file reads, edits, builds,
and tests inside that container under /workspace. Do not inspect or search the
host project, other containers, benchmark source, hidden tests, Oracle internals,
model credentials, or run metadata. Do not create, copy from, or inspect Docker
containers; the provided development container is the only one you may use.
"""

_SCOPE_RULES = (
    ("benchmark_or_credentials", re.compile(
        r"(?i)(?<![\w])(?:realcode_repos|rcb_tests_repos|detailed_prds|"
        r"repo_alias\.json|model_list\.json|\.codex/auth\.json|"
        r"user_agent/(?:user_model\.json|prd_json\w*|oracle_data\w*))(?![\w])")),
    ("docker_container_control", re.compile(
        r"(?i)\bdocker\s+(?:run|create|cp|inspect|build|volume|container)\b")),
)


def _scope_violation(command: str) -> str | None:
    for name, pattern in _SCOPE_RULES:
        if pattern.search(command):
            return name
    return None


async def _stop_process(process) -> None:
    """Stop the CLI and any docker/shell commands it started before retrying."""
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def run_agent(prompt: str, cwd: Path, log_path: Path, model_entry: dict,
                    *, max_turns: int = 200, timeout: float | None = 7200,
                    container_id: str | None = None) -> AgentResult:
    """Run one noninteractive Codex session in the bind-mounted task directory.

    Codex reports one top-level turn per `exec`; its JSON stream does not expose
    an equivalent of Claude's model-iteration limit, so max_turns cannot be
    enforced by this CLI. The overall and inactivity watchdogs still apply.
    """
    del max_turns
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = (["docker", "exec", "-i", "-w", "/workspace", container_id, "codex"]
               if container_id else [os.environ.get("ICAE_CODEX_CLI", "codex")])
    command += ["exec", "--json",
        "--ephemeral", "--skip-git-repo-check", "--color", "never",
        "--dangerously-bypass-approvals-and-sandbox", "-C", "/workspace" if container_id else str(cwd),
    ]
    if model_entry.get("CODEX_MODEL"):
        command += ["--model", str(model_entry["CODEX_MODEL"])]
    if model_entry.get("CODEX_REASONING_EFFORT"):
        command += ["--config", f"model_reasoning_effort={model_entry['CODEX_REASONING_EFFORT']}"]
    command.append("-")  # pass the task prompt via stdin, never the process list

    transcript_path = log_path.with_suffix(".jsonl")
    with open(log_path, "a", encoding="utf-8") as log, \
            open(transcript_path, "a", encoding="utf-8") as transcript:
        def w(line: str) -> None:
            log.write(line.rstrip("\n") + "\n")
            log.flush()

        def emit(record: dict) -> None:
            transcript.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            transcript.flush()

        w(f"\n===== codex run_agent @ {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"cwd={cwd} model={model_entry.get('CODEX_MODEL') or '(CLI config)'} "
          f"effort={model_entry.get('CODEX_REASONING_EFFORT') or '(CLI config)'} "
          f"(overall={timeout}s inactivity={INACTIVITY_TIMEOUT}s) =====")
        scoped_prompt = prompt if container_id else prompt + CODEX_SCOPE
        emit({"type": "prompt", "prompt": scoped_prompt, "cwd": str(cwd),
              "container_id": container_id,
              "model": model_entry.get("CODEX_MODEL"),
              "reasoning_effort": model_entry.get("CODEX_REASONING_EFFORT"),
              "ts": time.time()})
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=str(cwd), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except OSError as exc:
            detail = f"codex launch failed: {type(exc).__name__}: {exc}"
            w(f"[error] {detail}")
            return AgentResult(status="error", is_error=True, detail=detail)

        queue = asyncio.Queue()

        async def read_stream(stream, source: str) -> None:
            try:
                pending = b""
                while chunk := await stream.read(65536):
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        await queue.put((source, line.decode("utf-8", errors="replace")))
                if pending:
                    await queue.put((source, pending.decode("utf-8", errors="replace")))
            finally:
                await queue.put((source, None))

        readers = [asyncio.create_task(read_stream(process.stdout, "stdout")),
                   asyncio.create_task(read_stream(process.stderr, "stderr"))]
        started = time.monotonic()
        completed = False
        failed = False
        turns = 0
        usage = {}
        last_text = ""
        last_error = ""
        stderr_tail = ""
        fatal_tool_error = ""
        closed = 0
        timeout_detail = ""
        scope_violation = None
        try:
            try:
                process.stdin.write(scoped_prompt.encode("utf-8"))
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError) as exc:
                w(f"[stdin] {type(exc).__name__}: {exc}")
            while closed < 2:
                remaining = None if timeout is None else timeout - (time.monotonic() - started)
                if remaining is not None and remaining <= 0:
                    timeout_detail = "timeout_overall"
                    break
                budget = min(INACTIVITY_TIMEOUT, remaining) if remaining is not None else INACTIVITY_TIMEOUT
                try:
                    source, line = await asyncio.wait_for(queue.get(), timeout=budget)
                except asyncio.TimeoutError:
                    timeout_detail = "timeout_overall" if remaining is not None and remaining <= INACTIVITY_TIMEOUT else "timeout_inactivity"
                    break
                if line is None:
                    closed += 1
                    continue
                if source == "stderr":
                    w(f"[stderr] {line}")
                    stderr_tail = (stderr_tail + "\n" + line)[-3000:]
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    w(f"[stdout] {line}")
                    continue
                emit(event)
                kind = event.get("type", "")
                if kind == "turn.started":
                    turns += 1
                elif kind in ("turn.completed", "turn.failed"):
                    completed = kind == "turn.completed"
                    failed = kind == "turn.failed"
                    usage = event.get("usage") or usage
                    if failed:
                        error = event.get("error") or {}
                        last_error = error.get("message", "") if isinstance(error, dict) else str(error)
                elif kind == "error":
                    error = event.get("message") or event.get("error") or ""
                    last_error = error.get("message", "") if isinstance(error, dict) else str(error)
                elif kind in ("item.started", "item.updated", "item.completed"):
                    item = event.get("item") or {}
                    item_type = item.get("type", "")
                    if kind == "item.completed" and item_type == "error":
                        message = str(item.get("message") or "")
                        if ("code mode is unavailable" in message.lower() or
                                "failed to spawn code-mode host" in message.lower()):
                            fatal_tool_error = message
                    if kind == "item.started" and item_type == "command_execution":
                        scope_violation = _scope_violation(str(item.get("command") or ""))
                        if scope_violation:
                            w(f"[scope] stopped after suspicious command: {scope_violation}")
                            emit({"type": "scope.violation", "rule": scope_violation})
                            await _stop_process(process)
                            break
                    if kind == "item.completed" and item_type == "agent_message":
                        last_text = item.get("text") or last_text
                    if kind != "item.updated":
                        summary = item.get("text") or item.get("command") or item.get("output") or item.get("name") or ""
                        w(f"[{kind} {item_type}] {str(summary)[:2000]}")
                else:
                    w(f"[{kind}] {str(event.get('message') or '')[:2000]}")
            if timeout_detail:
                w(f"[watchdog] {timeout_detail} after {time.monotonic() - started:.0f}s")
                await _stop_process(process)
            else:
                remaining = None if timeout is None else max(0.01, timeout - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(process.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    timeout_detail = "timeout_overall"
                    w(f"[watchdog] {timeout_detail} after {time.monotonic() - started:.0f}s")
                    await _stop_process(process)
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        finally:
            await asyncio.gather(*readers, return_exceptions=True)

        tokens = {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(usage.get("cached_input_tokens") or 0),
        }
        base = dict(num_turns=turns, tail=last_text[-2000:], **tokens)
        if scope_violation:
            return AgentResult(status="error", is_error=True,
                               detail=f"scope_violation:{scope_violation}", **base)
        if timeout_detail:
            return AgentResult(status="error", is_error=True, detail=timeout_detail, **base)
        if fatal_tool_error:
            w(f"[result] error: {fatal_tool_error[:2000]}")
            return AgentResult(status="error", is_error=True,
                               detail=fatal_tool_error[:2000], **base)
        detail = last_error or (stderr_tail.strip() if process.returncode else "")
        if (failed or process.returncode != 0 or not completed) and _contains(detail, RATELIMIT_MARKERS):
            raise RateLimited(detail[:300])
        if failed or process.returncode != 0 or not completed:
            if not detail:
                detail = f"codex exited {process.returncode} without turn.completed"
            w(f"[result] error: {detail[:2000]}")
            return AgentResult(status="error", is_error=True,
                               raw_subtype="turn.failed" if failed else "process_error",
                               detail=detail[:2000], **base)
        refused = any(marker.lower() in last_text.lower() for marker in (
            "I can't help with that", "I cannot help with", "I'm not able to help"))
        w(f"[result] status={'refused' if refused else 'success'} "
          f"turns={turns} tokens={tokens['input_tokens']}/{tokens['output_tokens']} "
          f"cached={tokens['cache_read_tokens']}")
        return AgentResult(status="refused" if refused else "success",
                           refused=refused, raw_subtype="turn.completed", **base)
