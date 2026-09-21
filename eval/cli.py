"""CLI entrypoint for the eval harness."""

import argparse
import sys
from pathlib import Path
from typing import Optional

from .cases import CaseError, load_case, load_suite
from .compare import CompareError, compare_runs, render_compare_markdown
from .runner import REPO_ROOT, Runner, RunnerError

EVAL_DIR = Path(__file__).resolve().parent


def _tri_state(value: str) -> Optional[bool]:
    """Parse --enable-* tri-state (auto -> None = agent default)."""
    if value == "auto":
        return None
    return value == "on"


def _resolve_suite(name: str) -> Path:
    p = Path(name)
    if p.is_file():
        return p
    return EVAL_DIR / "suites" / f"{name}.yaml"


def _load_case_ids(suite_path: Path, restrict: list[str]) -> list[str]:
    suite_raw = load_suite(suite_path)
    case_ids = suite_raw.get("cases") or []
    if restrict:
        chosen: list[str] = []
        unmatched: list[str] = []
        for requested in restrict:
            if requested in case_ids:
                chosen.append(requested)
                continue
            # A bare case id is accepted when the suite has exactly one version
            # of it (`--case pod-crashloop-001` -> `pod-crashloop-001@1`).
            versions = [c for c in case_ids if c.split("@")[0] == requested]
            if len(versions) == 1:
                chosen.append(versions[0])
            elif versions:
                raise CaseError(
                    f"case {requested!r} has several versions in the suite: {sorted(versions)}; "
                    "pass an explicit case-id@version")
            else:
                unmatched.append(requested)
        if unmatched:
            raise CaseError(f"case(s) not in suite: {sorted(unmatched)}")
        case_ids = [c for c in case_ids if c in set(chosen)]
    if not case_ids:
        raise CaseError(f"suite {suite_path} declares no cases")
    return case_ids


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="eval", description="k8sPilot Phase 2 Eval Harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run a suite and freeze a run")
    run_p.add_argument("--suite", required=True)
    run_p.add_argument("--runs", type=int, default=5)
    run_p.add_argument("--profile", default="baseline")
    run_p.add_argument("--agent-url", default="http://localhost:8000")
    run_p.add_argument("--trace-dir", default=None,
                       help="dir the agent writes TRACE_DIR to (must match the running agent)")
    run_p.add_argument("--kubeconfig", default=None)
    run_p.add_argument("--reports-dir", default="reports")
    run_p.add_argument("--model", default="")
    run_p.add_argument("--case", action="append", default=None)
    run_p.add_argument("--enable-knowledge", choices=("auto", "on", "off"), default="auto",
                       help="Phase 4 knowledge retrieval gate: off = not exposed (group A/C)")
    run_p.add_argument("--enable-incidents", choices=("auto", "on", "off"), default="auto",
                       help="Phase 4 incident retrieval gate: off = not exposed (group A/B)")
    run_p.add_argument("--model-profile", default=None,
                       help="server-side model profile for this run (selection must be enabled)")

    bench_p = sub.add_parser("benchmark", help="multi-model benchmark (handoff §6)")
    bench_p.add_argument("--suite", required=True)
    bench_p.add_argument("--models", required=True,
                         help="comma-separated model profile names, e.g. profile-a,profile-b")
    bench_p.add_argument("--runs", type=int, default=1)
    bench_p.add_argument("--seed", type=int, default=42)
    bench_p.add_argument("--agent-url", default="http://localhost:8000")
    bench_p.add_argument("--trace-dir", default=None)
    bench_p.add_argument("--kubeconfig", default=None)
    bench_p.add_argument("--reports-dir", default="reports")
    bench_p.add_argument("--max-diagnoses", type=int, default=None)
    bench_p.add_argument("--time-limit-seconds", type=float, default=None)
    bench_p.add_argument("--model", default="", help="declared label only; not proof of model")
    bench_p.add_argument("--case", action="append", default=None)
    bench_p.add_argument("--enable-knowledge", choices=("auto", "on", "off"), default="auto")
    bench_p.add_argument("--enable-incidents", choices=("auto", "on", "off"), default="auto")

    cmp_p = sub.add_parser("compare", help="paired baseline/candidate report")
    cmp_p.add_argument("--baseline", required=True, help="run id or path")
    cmp_p.add_argument("--candidate", required=True, help="run id or path")
    cmp_p.add_argument("--reports-dir", default="reports")
    cmp_p.add_argument("--out", default=None, help="write compare markdown to this path")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "run":
            suite_path = _resolve_suite(args.suite)
            case_ids = _load_case_ids(suite_path, args.case)
            runner = Runner(agent_url=args.agent_url, trace_dir=args.trace_dir,
                            kubeconfig=args.kubeconfig, reports_dir=args.reports_dir,
                            model=args.model)
            run_id, run_dir = runner.run(
                suite_path, case_ids, args.runs, args.profile,
                enable_knowledge=_tri_state(args.enable_knowledge),
                enable_incidents=_tri_state(args.enable_incidents),
                model_profile=args.model_profile,
            )
            print(f"run {run_id} finished: {run_dir}")
            print(f"report: {(run_dir / 'report.md')}")
            return 0

        if args.cmd == "benchmark":
            from .benchmark import run_benchmark
            suite_path = _resolve_suite(args.suite)
            case_ids = _load_case_ids(suite_path, args.case)
            models = [m.strip() for m in args.models.split(",") if m.strip()]
            if not models:
                raise CaseError("--models must list at least one model profile")
            benchmark_id, run_dir = run_benchmark(
                suite_path=suite_path, case_ids=case_ids, models=models,
                runs_per_case=args.runs, seed=args.seed, agent_url=args.agent_url,
                trace_dir=args.trace_dir, reports_dir=args.reports_dir,
                kubeconfig=args.kubeconfig, max_diagnoses=args.max_diagnoses,
                time_limit_seconds=args.time_limit_seconds, model_label=args.model,
                enable_knowledge=_tri_state(args.enable_knowledge),
                enable_incidents=_tri_state(args.enable_incidents),
            )
            print(f"benchmark {benchmark_id} finished: {run_dir}")
            print(f"report: {(run_dir / 'model-benchmark.md')}")
            return 0

        if args.cmd == "compare":
            base = _resolve_run_dir(args.baseline, args.reports_dir)
            cand = _resolve_run_dir(args.candidate, args.reports_dir)
            result = compare_runs(base, cand)
            md = render_compare_markdown(result)
            print(md)
            out = Path(args.out) if args.out else cand / "report-compare.md"
            out.write_text(md, encoding="utf-8")
            print(f"\ncompare written to {out}")
            return 0

    except (CaseError, RunnerError, CompareError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _resolve_run_dir(ref: str, reports_dir: str) -> Path:
    p = Path(ref)
    if p.is_dir() and (p / "report.json").is_file():
        return p
    candidate = Path(reports_dir) / ref
    if (candidate / "report.json").is_file():
        return candidate
    raise CompareError(f"run not found: {ref} (looked under {reports_dir})")


if __name__ == "__main__":
    sys.exit(main())
