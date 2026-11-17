"""Run all eight course configurations for exactly one ICAE task.

Usage: python -m harness.course_experiment --repo realcode@005
Each group and infrastructure retry has a fresh Codex workspace and Oracle
append_id. The JSON summary is host-side; the PRD and public cases enter the workspace.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

from . import config as C
from . import docker_env, evaluate, summarize, user_agent_client as ua
from .codex_runner import run_agent
from .course_oracle import OracleRelay, confirmed_questions, service_questions
from .runner_common import RateLimited

GROUPS = (("A", "direct", 0), ("A′3", "self", 3), ("A′8", "self", 8),
          ("B3", "free", 3), ("B8", "free", 8),
          ("C3", "risk", 3), ("C8", "risk", 8), ("D", "detailed", 0))
RETRY_DELAYS = (30, 60, 120, 240)


class InfrastructureError(RuntimeError):
    pass


def _json_message(log_path: Path) -> dict:
    message = ""
    for line in log_path.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                message = str(item.get("text") or "")
    message = message.strip()
    if message.startswith("```"):
        message = re.sub(r"^```(?:json)?\s*|\s*```$", "", message, flags=re.I).strip()
    result = json.loads(message)
    if not isinstance(result, dict):
        raise ValueError("requirements phase final message must be a JSON object")
    return result


def _requirements_prompt(strategy: str, budget: int, relay_url: str | None,
                         oracle_timeout: float = 180) -> str:
    common = (
        "You are in the requirements processing phase. Read /workspace/start.md and "
        "the provided examples in /workspace/rcb_tests/public_test_cases/ to "
        "identify the exact dispatcher input/output contract. Do not edit, "
        "create, or delete files, and do not implement code. Finish with exactly one "
        "JSON object, no prose: {\"questions\":[{\"question\":\"...\","
        "\"answer\":\"...\",\"source\":\"...\"}]}. "
        "Each question must cover one independent requirement topic; do not bundle "
        "unrelated questions to evade the budget. "
        "Never inspect hidden tests, reference source, Oracle internals, or host files.\n\n"
    )
    if strategy == "self":
        return common + (
            f"Independently identify and answer at most {budget} important ambiguous "
            "requirements. Do not contact any Oracle. Use source=agent_assumption "
            "for every item. If no useful questions remain, return an empty list."
        )
    question = (
        f"You may ask at most {budget} questions of the Oracle. POST JSON "
        f"{{\"question\":\"...\"}} to {relay_url}. The response has data and status "
        "with remaining and fallback. A fallback consumes one question and says "
        "the requested detail is unspecified. Set the HTTP client timeout to at least "
        f"{math.ceil(oracle_timeout) + 30} seconds for every request, longer than "
        "the Oracle relay timeout. Ask exactly one question at a time. Wait for "
        "its full answer, then analyze what it resolves and choose the next "
        "question. If the command tool returns a running session handle, poll "
        "that same session until the HTTP response arrives. Never start another "
        "request while one is pending or repeat a question merely because its "
        "response has not arrived yet. "
        "You may rephrase while budget remains. "
        "Keep every successful Oracle reply in your final questions, including "
        "partial answers and pure fallbacks. Use source=oracle. Your "
        "final JSON is a non-authoritative summary; the experiment script "
        "deterministically builds the coding Q&A from the trusted Oracle log.\n"
    )
    if strategy == "risk":
        question += (
            "Before EVERY Oracle request, list candidate questions and at least two "
            "plausible interpretations for each. Give each impact (integer 1-5), "
            "uncertainty (integer 1-3), and score=impact*uncertainty. Choose the "
            "highest score; break ties by higher impact. After each answer, update "
            "the candidates and scores. Every POST must include both the selected "
            "question and candidates array: {\"question\":\"selected text\","
            "\"candidates\":[{\"question\":\"selected text\","
            "\"interpretations\":[\"meaning 1\",\"meaning 2\"],"
            "\"impact\":5,\"uncertainty\":3,\"score\":15}]}. "
            "The relay records each risk decision before sending the question."
        )
    return common + question


def _workspace_manifest(workdir: Path) -> dict[str, str | None]:
    """Record the requirements workspace so its PRD and public cases stay read-only."""
    return {p.relative_to(workdir).as_posix():
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in workdir.rglob("*")}


def _coding_prd(original: str, questions: list[dict], strategy: str) -> str:
    if strategy in ("direct", "detailed"):
        return original
    title = "Agent assumptions" if strategy == "self" else "Oracle replies"
    return (original.rstrip() + f"\n\n## {title}\n\n" +
            json.dumps({"questions": questions}, ensure_ascii=False, indent=2) + "\n")


async def _save_oracle_service_log(append_id: str, alias: str, args) -> list[dict]:
    """Preserve the trusted service log, including parse errors."""
    entries = await asyncio.to_thread(
        ua.fetch_interaction_log, append_id, alias,
        user_host=args.user_host, user_eval_port=args.user_eval_port)
    log_path = C.RESULTS / append_id / "oracle_service.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8")
    return entries


def _metrics(result, seconds: float) -> dict:
    return {"status": result.status, "detail": result.detail,
            "seconds": round(seconds, 3), "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "total_codex_tokens": result.input_tokens + result.output_tokens}


def _overall(objective: dict) -> float:
    rate = summarize._overall_rate(objective.get("hidden"), objective.get("enhanced"))
    return round(rate, 4) if rate is not None else 0.0


def _fingerprint(path: Path) -> str | None:
    """Hash the bytes used by a run, including sorted JSON files in a case dir."""
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        return None
    digest = hashlib.sha256()
    for file in sorted(path.glob("*.json")):
        digest.update(file.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


async def _phase(prompt: str, *, alias: str, append_id: str, phase: str,
                 prd: str, tar_path: Path, args, model: dict):
    workdir = C.code_path(append_id, alias)
    scaffold_src = (C.REPOS_DIR / C.key_for_alias(alias) / "rcb_tests" /
                    "public_test_cases")
    if not scaffold_src.is_dir():
        raise FileNotFoundError(f"public test scaffold missing: {scaffold_src}")
    container = await asyncio.to_thread(
        docker_env.provision, alias, workdir, prd, tar_path,
        clean_workdir=True, proxy=args.proxy,
        no_proxy="localhost,127.0.0.1",
        hide_real_tag=False, name_suffix=f"{append_id[:8]}_{phase}",
        scaffold_src=scaffold_src, codex_in_container=True,
        proxy_fallback_file=args.proxy_fallback_file)
    log_path = C.RESULTS / append_id / "trajectory" / f"{C.docker_safe(alias)}.{phase}.log"
    started = time.monotonic()
    try:
        original_manifest = _workspace_manifest(workdir) if phase == "requirements" else None
        result = await run_agent(prompt, workdir, log_path, model,
                                 timeout=args.requirements_timeout if phase == "requirements"
                                 else args.coding_timeout,
                                 container_id=container.container_id)
        seconds = time.monotonic() - started
        if phase == "requirements" and result.status == "success":
            if _workspace_manifest(workdir) != original_manifest:
                raise ValueError("requirements phase modified the task workspace")
        return result, seconds, log_path, container.evaluation_image
    finally:
        container.teardown()


async def _attempt(alias: str, group: str, strategy: str, budget: int,
                   fuzzy: str, detailed: str, tar_path: Path, args, model: dict,
                   batch_id: str) -> dict:
    append_id = await asyncio.to_thread(
        ua.mint_append_id, args.user_host, args.user_init_port,
        args.user_model_name, budget, "normal")
    ua.init_settings(append_id, {
        "course_batch_id": batch_id, "course_group": group, "model_name": args.model_name,
        "endpoint_model": model.get("CODEX_MODEL"),
        "endpoint_reasoning_effort": model.get("CODEX_REASONING_EFFORT"),
        "user_model_name": args.user_model_name, "query_count": budget,
        "env_mode": "base", "prd_type": "detailed" if strategy == "detailed" else "fuzzy",
        "selected_repos": [alias], "agent_framework": "codex", "scaffold": True,
    })
    row = {"group": group, "strategy": strategy, "question_budget": budget,
           "scaffold": True,
           "repo": alias, "append_id": append_id,
           "requirements": {"status": "pre_satisfied", "seconds": 0,
                            "input_tokens": 0, "output_tokens": 0,
                            "total_codex_tokens": 0},
           "actual_questions": 0, "fallback_count": 0,
           "confirmed_questions": 0, "forwarded_questions": 0}
    questions: list[dict] = []
    original = detailed if strategy == "detailed" else fuzzy
    try:
        if strategy in ("self", "free", "risk"):
            relay = (OracleRelay(append_id=append_id, alias=alias, host=args.user_host,
                                 port=args.user_query_port,
                                 log_path=C.RESULTS / append_id / "oracle_raw.jsonl",
                                 timeout=args.oracle_timeout,
                                 risk_mode=(strategy == "risk"))
                     if strategy in ("free", "risk") else None)
            if relay:
                with relay:
                    prompt = _requirements_prompt(
                        strategy, budget, f"http://127.0.0.1:{relay.port}/",
                        args.oracle_timeout)
                    phase = await _phase(prompt, alias=alias, append_id=append_id,
                                         phase="requirements", prd=original,
                                         tar_path=tar_path, args=args, model=model)
                if relay.fatal:
                    raise RuntimeError(relay.fatal)
                entries = await _save_oracle_service_log(append_id, alias, args)
                questions, count, fallbacks = service_questions(entries)
                _, relay_count, _ = confirmed_questions(relay.events)
                if count != relay_count:
                    raise RuntimeError(f"Oracle log count mismatch: service={count}, relay={relay_count}")
                row.update(actual_questions=count, fallback_count=fallbacks,
                           confirmed_questions=count - fallbacks,
                           forwarded_questions=len(questions),
                           oracle_response_length=sum(len(q["answer"]) for q in questions))
                if strategy == "risk":
                    row["risk_decisions"] = [
                        {"question": e["request"]["question"],
                         "candidates": e["risk_candidates"]}
                        for e in relay.events if "risk_candidates" in e]
            else:
                phase = await _phase(_requirements_prompt(strategy, budget, None),
                                     alias=alias, append_id=append_id,
                                     phase="requirements", prd=original,
                                     tar_path=tar_path, args=args, model=model)
            result, seconds, log_path, _ = phase
            row["requirements"] = _metrics(result, seconds)
            if result.status != "success":
                error = f"requirements phase: {result.detail or result.status}"
                raise InfrastructureError(error) if result.is_error else RuntimeError(error)
            row["requirements_output_authority"] = (
                "codex_assumptions" if strategy == "self" else "audit_summary_only")
            try:
                output = _json_message(log_path)
                items = output.get("questions")
                if not isinstance(items, list):
                    raise ValueError("requirements result must contain a questions list")
            except (OSError, ValueError) as exc:
                if strategy == "self":
                    raise
                row["requirements_summary_error"] = f"{type(exc).__name__}: {exc}"
                row["oracle_log_mismatch"] = True
            else:
                (C.RESULTS / append_id / "requirements_output.json").write_text(
                    json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
                if strategy == "self":
                    if len(items) > budget:
                        raise ValueError("invalid self clarification question count")
                    for q in items:
                        if (not isinstance(q, dict) or q.get("source") != "agent_assumption"
                                or not isinstance(q.get("question"), str)
                                or not isinstance(q.get("answer"), str)):
                            raise ValueError("invalid self clarification item")
                    questions = items
                    row.update(actual_questions=len(items), confirmed_questions=len(items),
                               forwarded_questions=len(items))
                else:
                    row["self_reported_questions"] = len(items)
                    reported = [(q.get("question"), q.get("answer")) for q in items
                                if isinstance(q, dict) and q.get("source") == "oracle"]
                    confirmed = [(q["question"], q["answer"]) for q in questions]
                    row["oracle_log_mismatch"] = reported != confirmed
            if strategy in ("free", "risk") and row["actual_questions"] > budget:
                raise RuntimeError("Oracle request count exceeded budget")
        coding_prd = _coding_prd(original, questions, strategy)
        from .task_template import render_task_prompt
        prompt = render_task_prompt(alias, docker_id="", lang=C.prompt_language("base", alias),
                                    in_container=True)
        result, seconds, _log, eval_image = await _phase(
            prompt, alias=alias, append_id=append_id, phase="coding", prd=coding_prd,
            tar_path=tar_path, args=args, model=model)
        row["coding"] = _metrics(result, seconds)
        if result.status != "success":
            error = f"coding phase: {result.detail or result.status}"
            raise InfrastructureError(error) if result.is_error else RuntimeError(error)
        row["clarification_time"] = row["requirements"]["seconds"]
        row["coding_time"] = row["coding"]["seconds"]
        row["total_time"] = round(row["clarification_time"] + row["coding_time"], 3)
        row["clarification_tokens"] = row["requirements"]["total_codex_tokens"]
        row["coding_tokens"] = row["coding"]["total_codex_tokens"]
        row["total_codex_tokens"] = row["clarification_tokens"] + row["coding_tokens"]
        ua.update_repo_status(append_id, alias, {
            "generation": "success", "code_path": str(C.code_path(append_id, alias)),
            "image": eval_image, "course_group": group,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens})
        if args.eval:
            objective = await asyncio.to_thread(
                evaluate.evaluate_repo, append_id, alias, eval_image,
                proxy=args.proxy, timeout=args.eval_timeout)
            row["objective"] = objective
            row["overall_pass_rate"] = _overall(objective)
            ua.update_repo_status(append_id, alias, {"objective": objective})
        if strategy in ("free", "risk"):
            row["interaction"] = await asyncio.to_thread(
                ua.fetch_interaction_stats, append_id, user_host=args.user_host,
                user_eval_port=args.user_eval_port, task_id=alias)
        row["status"] = "generation_failed" if row.get("objective", {}).get("error") else "success"
        if row["status"] == "generation_failed":
            ua.update_repo_status(append_id, alias, {"generation": "generation_failed",
                                                     "reason": row["objective"]["error"]})
    except Exception as exc:  # preserve logs and prevent scoring partial code
        if strategy in ("free", "risk") and not (
                C.RESULTS / append_id / "oracle_service.jsonl").exists():
            try:
                await _save_oracle_service_log(append_id, alias, args)
            except Exception as log_exc:
                row["oracle_log_export_error"] = f"{type(log_exc).__name__}: {log_exc}"
        row["status"] = "infrastructure_error" if (
            isinstance(exc, (InfrastructureError, docker_env.DockerError, OSError, RateLimited)) or
            any(x in str(exc).lower() for x in
                ("timeout", "transport", "oracle", "connection", "ratelimit"))) else "generation_error"
        row["error"] = f"{type(exc).__name__}: {exc}"
        ua.update_repo_status(append_id, alias, {"generation": "error",
                                                 "reason": row["error"],
                                                 "course_group": group})
    return row


async def run(args) -> dict:
    if "," in args.repo or " " in args.repo:
        raise SystemExit("--repo must identify exactly one project")
    alias = C.resolve_alias(args.repo)
    fuzzy_path = C.FUZZY_PRDS / alias / "start.md"
    detailed_path = C.ROOT / "detailed_prds_oracle" / alias / "start.md"
    for path in (fuzzy_path, detailed_path):
        if not path.is_file():
            raise SystemExit(f"required PRD missing: {path}")
    scaffold_path = C.REPOS_DIR / C.key_for_alias(alias) / "rcb_tests" / "public_test_cases"
    if not scaffold_path.is_dir() or not any(scaffold_path.glob("*.json")):
        raise SystemExit(f"public test scaffold missing: {scaffold_path}")
    fuzzy, detailed = (p.read_text(encoding="utf-8") for p in
                       (fuzzy_path, detailed_path))
    tar_path = C.resolve_docker_tar("base", alias)
    if not tar_path.is_file():
        raise SystemExit(f"base Docker image tar missing: {tar_path}")
    if not docker_env.codex_binary_path().is_file():
        raise SystemExit("static Codex binary missing; set ICAE_CODEX_BINARY")
    if not docker_env.codex_code_mode_host_path().is_file():
        raise SystemExit("Codex code-mode host missing next to the static Codex binary")
    try:
        configured = C.resolve_model(args.model_name)
    except KeyError:
        configured = [{}]  # a run label may use the Codex CLI's own model config
    model = {k: configured[0][k] for k in ("CODEX_MODEL", "CODEX_REASONING_EFFORT")
             if configured and configured[0].get(k)}
    if getattr(args, "codex_model", None):
        model["CODEX_MODEL"] = args.codex_model
    if getattr(args, "reasoning_effort", None):
        model["CODEX_REASONING_EFFORT"] = args.reasoning_effort
    batch_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    outdir = C.RESULTS / "course_runs" / batch_id
    outdir.mkdir(parents=True)
    auth_root = C.REPOS_DIR / C.key_for_alias(alias) / "rcb_tests"
    inputs = {
        "fuzzy_prd": fuzzy_path,
        "detailed_prd": detailed_path,
        "oracle_prd_json": C.USER_AGENT_DIR / "prd_json" / f"{alias}.json",
        "public_test_cases": auth_root / "public_test_cases",
        "hidden_test_cases": auth_root / "test_cases",
        "enhanced_test_cases": auth_root / "enhanced_test_cases",
        "course_runner": C.HARNESS / "course_experiment.py",
        "codex_runner": C.HARNESS / "codex_runner.py",
        "oracle_relay": C.HARNESS / "course_oracle.py",
        "evaluator": C.HARNESS / "evaluate.py",
        "task_renderer": C.HARNESS / "task_template.py",
        "task_prompt": C.HARNESS / "prompt_templates" / "task_docker.md",
        "container_task_prompt": C.HARNESS / "prompt_templates" / "task_host_docker.md",
        "docker_environment": C.HARNESS / "docker_env.py",
        "oracle_service": C.USER_AGENT_DIR / "main.py",
        "oracle_runtime": C.USER_AGENT_DIR / "user_agent.py",
        "oracle_model_config": C.USER_MODEL_FILE,
        "shell_runner": C.ROOT / "run.sh",
    }
    summary = {"batch_id": batch_id, "repo": alias, "model_name": args.model_name,
               "scaffold": True,
               "model": model, "oracle_model": args.user_model_name,
               "codex_cli_config_sha256": _fingerprint(
                   Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) /
                   "config.toml"),
               "input_fingerprints": {name: _fingerprint(path) for name, path in inputs.items()},
               "runner_settings": {
                   "requirements_timeout": getattr(args, "requirements_timeout", None),
                   "coding_timeout": getattr(args, "coding_timeout", None),
                   "oracle_timeout": getattr(args, "oracle_timeout", None),
                   "eval_timeout": getattr(args, "eval_timeout", None),
                   "max_retries": args.max_retries,
                   "evaluation_enabled": getattr(args, "eval", True),
               },
               "groups": {}}
    for group, strategy, budget in GROUPS:
        print(f"[course] {alias} {group}: starting", flush=True)
        attempts = []
        for retry in range(args.max_retries + 1):
            try:
                row = await _attempt(alias, group, strategy, budget, fuzzy, detailed,
                                     tar_path, args, model, batch_id)
            except Exception as exc:
                row = {"group": group, "repo": alias, "append_id": None,
                       "status": "infrastructure_error",
                       "error": f"{type(exc).__name__}: {exc}"}
            attempts.append(row)
            if row["status"] != "infrastructure_error" or retry == args.max_retries:
                break
            delay = RETRY_DELAYS[min(retry, len(RETRY_DELAYS) - 1)]
            print(f"[course] {group} infrastructure error; new append_id after {delay}s",
                  flush=True)
            await asyncio.sleep(delay)
        final = attempts[-1]
        final["attempts"] = [{"append_id": a["append_id"], "status": a["status"],
                              "error": a.get("error")} for a in attempts]
        summary["groups"][group] = final
        (outdir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[course] {group}: {final['status']} append_id={final['append_id']}",
              flush=True)
    return summary


def build_parser() -> argparse.ArgumentParser:
    class SingleRepo(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            if getattr(namespace, self.dest, None) is not None:
                parser.error("--repo may be specified only once")
            setattr(namespace, self.dest, values)

    parser = argparse.ArgumentParser(description="One project, eight course groups")
    parser.add_argument("--repo", required=True, action=SingleRepo,
                        help="one realcode@NNN alias or repo key")
    parser.add_argument("--model-name", default="Codex-config")
    parser.add_argument("--codex-model", default=None,
                        help="override the Codex CLI model for this batch")
    parser.add_argument("--reasoning-effort", default=None,
                        help="override the Codex CLI reasoning effort for this batch")
    parser.add_argument("--user-model-name", default="Qwen3.8-Flash")
    parser.add_argument("--user-host", default="127.0.0.1")
    parser.add_argument("--user-init-port", type=int, default=50001)
    parser.add_argument("--user-query-port", type=int, default=50002)
    parser.add_argument("--user-eval-port", type=int, default=50003)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--proxy-fallback-file", default=None)
    parser.add_argument("--requirements-timeout", type=float, default=3600)
    parser.add_argument("--coding-timeout", type=float, default=7200)
    parser.add_argument("--oracle-timeout", type=float, default=180)
    parser.add_argument("--eval-timeout", type=float, default=2400)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--no-eval", dest="eval", action="store_false")
    parser.set_defaults(eval=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.max_retries < 0:
        raise SystemExit("--max-retries must be nonnegative")
    summary = asyncio.run(run(args))
    print(f"[course] summary: {C.RESULTS / 'course_runs' / summary['batch_id'] / 'summary.json'}")
    if any(row["status"] not in ("success", "generation_failed")
           for row in summary["groups"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
