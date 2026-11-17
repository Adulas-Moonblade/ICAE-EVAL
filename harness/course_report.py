"""Export course experiment tables and, for one batch, its audit evidence.

The source of truth remains results/course_runs/*/summary.json and the
per-append-id logs. Missing or inconsistent data is visible in diagnostics.json.
"""
import argparse
import csv
import hashlib
import json
import tempfile
from pathlib import Path

from . import config as C, summarize
from .course_experiment import GROUPS

GROUP_NAMES = tuple(group for group, _, _ in GROUPS)
PAIRS = (
    ("A", "A′3"), ("A", "A′8"), ("A", "B3"), ("A", "B8"),
    ("A", "C3"), ("A", "C8"), ("A", "D"),
    ("A′3", "A′8"), ("A′3", "B3"), ("A′8", "B8"),
    ("B3", "B8"), ("C3", "C8"), ("B3", "C3"), ("B8", "C8"),
    ("B3", "D"), ("B8", "D"), ("C3", "D"), ("C8", "D"),
)
DELTA_FIELDS = ("overall_pass_rate", "total_codex_tokens", "total_time_s",
                "actual_questions", "constraint_coverage_pct", "fallback_rate_pct")
RUN_FIELDS = (
    "batch_id", "repo", "group", "append_id", "status", "study_ready", "issues",
    "public_passed", "public_total", "hidden_passed", "hidden_total",
    "enhanced_passed", "enhanced_total", "overall_pass_rate",
    "actual_questions", "constraint_coverage_pct", "fallback_rate_pct",
    "clarification_tokens", "coding_tokens", "total_codex_tokens",
    "clarification_time_s", "coding_time_s", "total_time_s",
)
PAIR_FIELDS = ("batch_id", "repo", "left_group", "right_group", "study_ready") + tuple(
                   f"delta_{field}" for field in DELTA_FIELDS)

REPORT_README = """# Course batch export

This folder contains one project and one eight-group experiment batch. `batch_id`
in the CSV files identifies `summary.json`, which holds the run settings, model
configuration, input fingerprints, all group metrics, and retry history.

- `runs.csv`: one row per group. `overall_pass_rate` follows the framework's
  Native+Enhanced definition: passed cases divided by total cases across hidden
  and enhanced tiers. Public cases are reported separately and excluded from
  Overall. A missing evaluation is blank. A completed generation without a
  test runner scores zero.
  A/D have zero clarification cost. `actual_questions` counts self-answered
  questions for A′ and Oracle requests for B/C; only B/C represent external
  interactions. `constraint_coverage_pct` and `fallback_rate_pct` apply to B/C
  and use the Oracle Stats values. Raw Oracle logs are retained to check counts.
- `pairs.csv`: selected within-batch comparisons. Every `delta_*` is right group
  minus left group. Blank values mean the metric does not apply or is unavailable.
  Compare only rows with `study_ready=True` for formal analysis.
- `diagnostics.json`: missing or inconsistent source data and other reasons a
  group may not be study ready. Do not replace missing values with zero.
- `manifest.json`: archived source paths, relative paths, byte sizes, SHA256
  digests, and expected files that could not be copied.
- `groups/`: original settings, Codex JSONL trajectories, requirements output,
  Oracle logs where applicable, coding `start.md`, objective score, and evaluation
  build logs. Attempts have separate subdirectories.

`total_codex_tokens` is clarification plus coding tokens, including cached input
tokens already counted by Codex. Oracle model tokens are excluded. `total_time_s`
is clarification plus coding time, including normal Oracle waits but excluding
container preparation, final evaluation, and infrastructure retry time.

Model details appear once in `summary.json`; join on `batch_id` when combining
batches. This export supports metric and log analysis. It does not include
generated code, benchmark test definitions, or per-case `outcap_*` files.
Oracle logs can contain hidden requirement details; handle this folder accordingly.
"""


def _number(value):
    return value if type(value) in (int, float) else None


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _phase_usage(results_root: Path, append_id: str, repo: str, phase: str,
                 metrics: dict, issues: list[str]) -> None:
    if metrics.get("status") == "pre_satisfied":
        return
    path = (results_root / append_id / "trajectory" /
            f"{C.docker_safe(repo)}.{phase}.jsonl")
    if not path.is_file():
        issues.append(f"missing_{phase}_jsonl")
        return
    try:
        completed = [event.get("usage") or {} for event in _read_jsonl(path)
                     if event.get("type") == "turn.completed"]
    except (OSError, json.JSONDecodeError):
        issues.append(f"invalid_{phase}_jsonl")
        return
    if metrics.get("status") == "success" and not completed:
        issues.append(f"missing_{phase}_usage")
    if completed:
        usage = completed[-1]
        for key in ("input_tokens", "output_tokens"):
            if _number(metrics.get(key)) != _number(usage.get(key)):
                issues.append(f"{phase}_{key}_mismatch")


def _oracle_log(results_root: Path, append_id: str, row: dict,
                issues: list[str]) -> tuple[int | None, float | None]:
    path = results_root / append_id / "oracle_service.jsonl"
    if not path.is_file():
        issues.append("missing_oracle_service_log")
        return None, None
    try:
        entries = [e for e in _read_jsonl(path) if "answer" in e]
    except (OSError, json.JSONDecodeError):
        issues.append("invalid_oracle_service_log")
        return None, None
    relay_path = results_root / append_id / "oracle_raw.jsonl"
    if not relay_path.is_file():
        issues.append("missing_oracle_relay_log")
    else:
        try:
            relayed = [e for e in _read_jsonl(relay_path)
                       if ((e.get("response") or {}).get("status") or {}).get("ok")]
            if len(relayed) != len(entries):
                issues.append("oracle_relay_service_count_mismatch")
        except (OSError, json.JSONDecodeError):
            issues.append("invalid_oracle_relay_log")
    fallback = lambda e: bool(e.get("fallback_triggered") or e.get("parse_error"))
    confirmed = [e for e in entries if not fallback(e)]
    observed = (len(entries), len(confirmed), len(entries) - len(confirmed))
    recorded = (row.get("actual_questions"), row.get("confirmed_questions"),
                row.get("fallback_count"))
    if observed != recorded:
        issues.append("oracle_question_counts_mismatch")
    response_chars = sum(len(str(e.get("answer") or "")) for e in confirmed)
    if response_chars != row.get("oracle_response_length"):
        issues.append("oracle_response_length_mismatch")
    triggered = {constraint for e in entries if not fallback(e)
                 for constraint in e.get("triggers_hit", [])}
    per_query = round(len(triggered) / len(entries), 4) if entries else None
    return response_chars, per_query


def _run_row(batch: dict, group: str, row: dict, source: Path,
             results_root: Path, batch_complete: bool) -> dict:
    issues: list[str] = []
    repo, append_id = batch.get("repo"), row.get("append_id")
    model = batch.get("model") or {}
    fingerprints = batch.get("input_fingerprints") or {}
    if not fingerprints or any(not value for value in fingerprints.values()):
        issues.append("missing_input_fingerprints")
    config_hash = batch.get("codex_cli_config_sha256")
    if not config_hash:
        issues.append("codex_cli_config_unrecorded")
    fingerprint_id = (hashlib.sha256(json.dumps(
                                    {"inputs": fingerprints, "codex_cli_config": config_hash},
                                    sort_keys=True)
                                    .encode("utf-8")).hexdigest()[:16]
                      if fingerprints else None)
    if not model.get("CODEX_MODEL"):
        issues.append("codex_model_unspecified")
    if not batch_complete:
        issues.append("incomplete_batch")
    objective = row.get("objective") or {}
    values = {"batch_id": batch.get("batch_id"), "repo": repo, "group": group,
              "strategy": row.get("strategy"), "status": row.get("status"),
              "append_id": append_id, "model_name": batch.get("model_name"),
              "codex_model": model.get("CODEX_MODEL"),
              "reasoning_effort": model.get("CODEX_REASONING_EFFORT"),
              "oracle_model": batch.get("oracle_model"),
              "codex_cli_config_sha256": config_hash,
              "question_budget": row.get("question_budget"),
              "actual_questions": row.get("actual_questions"),
              "confirmed_questions": row.get("confirmed_questions"),
              "fallback_count": row.get("fallback_count"),
              "self_reported_questions": row.get("self_reported_questions"),
              "oracle_log_mismatch": row.get("oracle_log_mismatch"),
              "constraint_coverage_pct": None, "constraints_per_query": None,
              "oracle_response_chars": None, "source_summary": str(source),
              "retry_count": max(0, len(row.get("attempts") or []) - 1),
              "input_fingerprint_id": fingerprint_id}
    actual = _number(row.get("actual_questions"))
    fallbacks = _number(row.get("fallback_count"))
    is_oracle = row.get("strategy") in ("free", "risk")
    values["budget_usage_pct"] = None
    values["fallback_rate_pct"] = None
    if is_oracle:
        interaction = row.get("interaction") or {}
        if interaction.get("ok"):
            values["constraint_coverage_pct"] = interaction.get("constraint_coverage")
            values["fallback_rate_pct"] = _number(interaction.get("fallback_rate"))
            values["budget_usage_pct"] = _number(interaction.get("budget_usage_rate"))
            logged_fallback = (100 * fallbacks / actual if actual and fallbacks is not None
                               else 0.0 if actual == 0 else None)
            if (logged_fallback is not None and values["fallback_rate_pct"] is not None
                    and abs(logged_fallback - values["fallback_rate_pct"]) > 0.2):
                issues.append("fallback_rate_mismatch")
            budget = _number(row.get("question_budget"))
            logged_budget = (100 * actual / budget if budget and actual is not None else None)
            if (logged_budget is not None and values["budget_usage_pct"] is not None
                    and abs(logged_budget - values["budget_usage_pct"]) > 0.2):
                issues.append("budget_usage_rate_mismatch")
        else:
            issues.append("missing_interaction_stats")
        if append_id:
            chars, per_query = _oracle_log(results_root, append_id, row, issues)
            values["oracle_response_chars"] = chars
            values["constraints_per_query"] = per_query
    passed = total = 0
    complete_tiers = True
    for source_key, prefix in (("public_visible", "public"), ("hidden", "hidden"),
                               ("enhanced", "enhanced")):
        tier = objective.get(source_key) or {}
        p, t = _number(tier.get("passed")), _number(tier.get("total"))
        values[f"{prefix}_passed"] = p
        values[f"{prefix}_total"] = t
        values[f"{prefix}_rate"] = summarize._rate(tier)
        if source_key != "public_visible":
            if p is None or t is None or t <= 0 or p > t:
                complete_tiers = False
            else:
                passed += p
                total += t
    generated_without_runner = (row.get("status") == "generation_failed" and
                                bool(objective.get("error")))
    quality_ready = complete_tiers or generated_without_runner
    values["overall_passed"] = passed if complete_tiers else (0 if generated_without_runner else None)
    values["overall_total"] = total if complete_tiers else None
    values["overall_pass_rate"] = (summarize._overall_rate(
                                   objective.get("hidden"), objective.get("enhanced"))
                                   if complete_tiers else
                                   0.0 if generated_without_runner else None)
    if not quality_ready:
        issues.append("missing_objective_score")
    reported_rate = _number(row.get("overall_pass_rate"))
    if (reported_rate is not None and values["overall_pass_rate"] is not None and
            abs(reported_rate - values["overall_pass_rate"]) > 0.00011):
        issues.append("overall_pass_rate_mismatch")
    requirements = row.get("requirements") or {}
    coding = row.get("coding") or {}
    for phase, metrics, prefix in (("requirements", requirements, "clarification"),
                                   ("coding", coding, "coding")):
        values[f"{prefix}_tokens"] = _number(metrics.get("total_codex_tokens"))
        values[f"{prefix}_cache_read_tokens"] = _number(metrics.get("cache_read_tokens"))
        values[f"{prefix}_time_s"] = _number(metrics.get("seconds"))
        if append_id and metrics:
            _phase_usage(results_root, append_id, repo, phase, metrics, issues)
    values["total_codex_tokens"] = row.get("total_codex_tokens")
    values["total_time_s"] = row.get("total_time")
    for field, parts in (("total_codex_tokens", ("clarification_tokens", "coding_tokens")),
                         ("total_time_s", ("clarification_time_s", "coding_time_s"))):
        items = [_number(values.get(key)) for key in parts]
        if all(item is not None for item in items):
            if _number(values.get(field)) is None or abs(values[field] - sum(items)) > 0.002:
                issues.append(f"{field}_mismatch")
    values["quality_ready"] = quality_ready
    values["study_ready"] = (quality_ready and batch_complete and
                             row.get("status") in ("success", "generation_failed") and
                             not issues)
    values["issues"] = ";".join(issues)
    return values


def collect(course_runs: Path, results_root: Path,
            *, batch_ids: set[str] | None = None, repo: str | None = None,
            sources: list[Path] | None = None) -> tuple[list[dict], list[dict]]:
    runs: list[dict] = []
    diagnostics: list[dict] = []
    for source in sorted(sources if sources is not None else course_runs.glob("*/summary.json")):
        if batch_ids and source.parent.name not in batch_ids:
            continue
        try:
            batch = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            diagnostics.append({"source": str(source), "issue": f"invalid_summary: {exc}"})
            continue
        if repo and batch.get("repo") != repo:
            continue
        groups = batch.get("groups") or {}
        complete = (set(groups) == set(GROUP_NAMES) and
                    all((groups[g] or {}).get("status") in ("success", "generation_failed")
                        for g in GROUP_NAMES))
        for group in GROUP_NAMES:
            if group not in groups:
                continue
            run = _run_row(batch, group, groups[group], source, results_root, complete)
            runs.append(run)
            if run["issues"]:
                diagnostics.append({"batch_id": run["batch_id"], "group": group,
                                    "issues": run["issues"].split(";")})
    versions: dict[tuple, set[str]] = {}
    for run in runs:
        if run["input_fingerprint_id"]:
            key = (run["repo"], run["codex_model"], run["reasoning_effort"],
                   run["oracle_model"])
            versions.setdefault(key, set()).add(run["input_fingerprint_id"])
    for run in runs:
        key = (run["repo"], run["codex_model"], run["reasoning_effort"],
               run["oracle_model"])
        if len(versions.get(key, ())) > 1:
            run["study_ready"] = False
            run["issues"] += (";" if run["issues"] else "") + "input_version_mismatch"
            diagnostics.append({"batch_id": run["batch_id"], "group": run["group"],
                                "issues": ["input_version_mismatch"]})
    return runs, diagnostics


def paired(runs: list[dict]) -> list[dict]:
    by_batch: dict[str, dict[str, dict]] = {}
    for run in runs:
        by_batch.setdefault(run["batch_id"], {})[run["group"]] = run
    output = []
    for batch_id, groups in by_batch.items():
        for left_name, right_name in PAIRS:
            if left_name not in groups or right_name not in groups:
                continue
            left, right = groups[left_name], groups[right_name]
            item = {"batch_id": batch_id, "repo": left["repo"],
                    "left_group": left_name, "right_group": right_name,
                    "study_ready": left["study_ready"] and right["study_ready"]}
            for field in DELTA_FIELDS:
                a, b = _number(left.get(field)), _number(right.get(field))
                item[f"delta_{field}"] = (round(b - a, 6) if a is not None and b is not None
                                           else None)
            output.append(item)
    return output


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in rows)


def _write_tables(output_dir: Path, runs: list[dict], diagnostics: list[dict]) -> int:
    comparisons = paired(runs)
    _write_csv(output_dir / "runs.csv", RUN_FIELDS, runs)
    _write_csv(output_dir / "pairs.csv", PAIR_FIELDS, comparisons)
    (output_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(comparisons)


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value in (".", "..") or Path(value).name != value:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def archive_batch(batch: dict, source: Path, results_root: Path, output_dir: Path) -> dict:
    """Copy a narrow evidence set and hash every copied file."""
    repo = _safe_component(batch.get("repo"), "repo")
    files: list[tuple[Path, Path, bool]] = [
        (source, Path("summary.json"), True),
    ]
    for group, row in (batch.get("groups") or {}).items():
        _safe_component(group, "group")
        attempts = row.get("attempts") or [{"append_id": row.get("append_id")}]
        if row.get("append_id") and not any(a.get("append_id") == row["append_id"] for a in attempts):
            attempts = [*attempts, {"append_id": row["append_id"]}]
        for index, attempt in enumerate(attempts, 1):
            append_id = attempt.get("append_id")
            if not append_id:
                continue  # failure before a run directory was created
            _safe_component(append_id, "append_id")
            base = results_root / append_id
            target = Path("groups") / group / f"attempt-{index:02d}-{append_id}"
            final = append_id == row.get("append_id")
            files.append((base / "settings.json", target / "settings.json", True))
            required_status = (row.get("requirements") or {}).get("status") if final else None
            coding_status = (row.get("coding") or {}).get("status") if final else None
            for phase, status in (("requirements", required_status), ("coding", coding_status)):
                log = base / "trajectory" / f"{C.docker_safe(repo)}.{phase}.jsonl"
                if log.is_file() or status not in (None, "pre_satisfied"):
                    files.append((log, target / "trajectory" / log.name, final and status != "pre_satisfied"))
            requirements_output = base / "requirements_output.json"
            if requirements_output.is_file() or (final and required_status == "success"):
                files.append((requirements_output, target / requirements_output.name,
                              final and required_status == "success"))
            if row.get("strategy") in ("free", "risk"):
                for name in ("oracle_service.jsonl", "oracle_raw.jsonl"):
                    path = base / name
                    if path.is_file() or final:
                        files.append((path, target / name, final))
            if final:
                coding_prd = base / repo / "start.md"
                if coding_prd.is_file() or coding_status:
                    files.append((coding_prd, target / "start.md", bool(coding_status)))
                objective = base / "_eval" / repo / "objective.json"
                if objective.is_file() or row.get("objective"):
                    files.append((objective, target / "evaluation" / "objective.json",
                                  bool(row.get("objective"))))
                if row.get("objective") and not (row["objective"] or {}).get("error"):
                    for tier in ("public_test_cases", "test_cases", "enhanced_test_cases"):
                        log = base / "_eval" / repo / f".build_log_{tier}.txt"
                        files.append((log, target / "evaluation" / log.name, True))
    manifest = {"batch_id": batch.get("batch_id"), "repo": repo,
                "source_summary": str(source.resolve()), "files": [], "missing": []}
    for original, relative, required in files:
        if (not original.is_file() or original.is_symlink() or
                (original != source and not original.resolve().is_relative_to(results_root.resolve()))):
            if required:
                manifest["missing"].append({"source": str(original), "archive_path": str(relative)})
            continue
        destination = output_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with original.open("rb") as reader, destination.open("wb") as writer:
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk)
                writer.write(chunk)
        manifest["files"].append({"source": str(original.resolve()),
                                  "archive_path": str(relative), "bytes": destination.stat().st_size,
                                  "sha256": digest.hexdigest()})
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Export course analysis tables and batch evidence")
    parser.add_argument("--course-runs", type=Path, default=C.RESULTS / "course_runs")
    parser.add_argument("--results-root", type=Path,
                        help="root containing per-append-id run directories")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--run-dir", type=Path,
                        help="one results/course_runs/<batch_id> directory; also archive key evidence")
    parser.add_argument("--batch-id", action="append", default=[])
    parser.add_argument("--repo")
    args = parser.parse_args()
    if args.run_dir and (args.batch_id or args.repo):
        parser.error("--run-dir cannot be combined with --batch-id or --repo")
    results_root = args.results_root or (args.run_dir.parent.parent
                                         if args.run_dir and args.run_dir.parent.name == "course_runs"
                                         else C.RESULTS)
    source = args.run_dir / "summary.json" if args.run_dir else None
    batch = None
    if source:
        try:
            batch = json.loads(source.read_text(encoding="utf-8"))
            if batch.get("batch_id") != args.run_dir.name:
                parser.error("summary batch_id does not match the selected directory")
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read batch summary: {exc}")
    runs, diagnostics = collect(args.course_runs, results_root,
                                batch_ids=set(args.batch_id) or None, repo=args.repo,
                                sources=[source] if source else None)
    if not runs:
        parser.error("no matching course groups found")
    output_dir = args.output_dir or (results_root / "course_exports" / args.run_dir.name
                                     if args.run_dir else C.RESULTS / "course_report")
    if args.run_dir:
        if output_dir.exists():
            parser.error(f"output directory already exists: {output_dir}; choose a new --output-dir")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{output_dir.name}.tmp-",
                                         dir=output_dir.parent) as temporary:
            staging = Path(temporary)
            comparisons = _write_tables(staging, runs, diagnostics)
            (staging / "README.md").write_text(REPORT_README, encoding="utf-8")
            manifest = archive_batch(batch, source, results_root, staging)
            staging.rename(output_dir)
        print(f"Archived {len(manifest['files'])} source files; missing expected files: "
              f"{len(manifest['missing'])}")
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        comparisons = _write_tables(output_dir, runs, diagnostics)
        (output_dir / "pair_summary.csv").unlink(missing_ok=True)
    print(f"Exported {len(runs)} group rows, {comparisons} paired comparisons, "
          f"study-ready groups: {sum(bool(r['study_ready']) for r in runs)}; "
          f"diagnostics: {len(diagnostics)}")
    print(output_dir)


if __name__ == "__main__":
    main()
