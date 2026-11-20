"""Run trusted submissions against independent tests and clean patch testbeds."""

import ast
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from .evidence import Pending, require
from .submission import INSTANCE, ROOT

SUITES = {
    "prompt": [
        "test_prompt",
        "test_prompt_carries_task_action_and_result",
        "test_long_observation_is_truncated",
    ],
    "lifecycle": ["test_cleanup_text", "test_step_limit_raises"],
    "dispatch": [
        "test_dispatch",
        "test_dispatch_continues_after_bad_calls",
        "test_malformed_arguments_are_not_executed",
        "test_unknown_tool_is_not_executed",
    ],
    "skills": ["test_skills", "test_skill_catalog_lists_names_without_skill_bodies"],
    "trigger": ["test_summary_trigger"],
    "retain": ["test_retention"],
    "schema": ["test_play_move_schema_is_strict_and_registered", "test_schema_details"],
    "play": [
        "test_play_batch",
        "test_play_move_posts_request_and_returns_new_state",
        "test_rejected_and_malformed_moves_are_recoverable",
    ],
    "simulate": ["test_simulate_state", "test_simulate_errors"],
    "python": ["test_python_success", "test_python_errors"],
}


def environment(cwd, paths):
    return dict(
        PATH=os.environ.get("PATH", os.defpath),
        HOME=str(cwd),
        TMPDIR=str(cwd),
        PYTHONPATH=os.pathsep.join(map(str, paths)),
        PYTHONNOUSERSITE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONDONTWRITEBYTECODE="1",
        OPENAI_BASE_URL="http://127.0.0.1:0/v1",
        OPENAI_API_KEY="offline-test",
        OPENAI_MAX_RETRIES="0",
        PYTHON_DOTENV_DISABLED="1",
    )


def run_probes(stage, key):
    with TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        for name in ("test_agent.py", "test_chess_agent.py"):
            shutil.copy(ROOT / "tests" / name, cwd / name)
        shutil.copy(Path(__file__).with_name("probes.py"), cwd / "test_probes.py")
        shutil.copytree(ROOT / "tasks/code-skills", cwd / "tasks/code-skills")
        # Keep imports on the staged tree even when the active venv has an editable install.
        env = environment(cwd, [stage / "src", cwd])
        files = {
            node.name: path.name
            for path in cwd.glob("test_*.py")
            for node in ast.parse(path.read_text()).body
            if isinstance(node, ast.FunctionDef)
        }
        nodes = [f"{files[name]}::{name}" for name in SUITES[key]]
        cmd = [sys.executable, "-m", "pytest", "-q", "--junitxml=results.xml", *nodes]
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=60
            )
        except subprocess.TimeoutExpired as exc:
            raise Pending("offline test process timed out") from exc
        xml = cwd / "results.xml"
        require(xml.is_file(), f"pytest exited {proc.returncode}: {proc.stdout[-300:]}")
        results = {}
        for case in ET.parse(xml).iter("testcase"):
            problem = next(
                (c for c in case if c.tag in {"failure", "error", "skipped"}), None
            )
            results[case.get("name")] = (
                None
                if problem is None
                else (problem.get("message") or problem.tag)[:360]
            )
        return results


def tests_for(results, key, names=None):
    tests = SUITES[key] if names is None else names
    failures = {
        t: results.get(t, "test not collected")
        for t in tests
        if results.get(t, "missing") is not None
    }
    require(not failures, str(failures))
    return ", ".join(tests)


def local_patch(patch, repo, spec, private=None):
    from assignment.eval.harness import TestStatus, parse_report

    require(patch.is_file(), f"missing {patch.name}")
    if not repo or not repo.is_dir():
        raise Pending(
            "pinned checkout unavailable; use --django-source or --modal-replay"
        )
    chess_task = private is not None
    if chess_task:
        require(
            private.task_id == "chess-terminal-move", "incorrect chess evaluation spec"
        )
    meta = private or spec
    commit = spec.base_commit
    with TemporaryDirectory() as temp:
        cwd = Path(temp)
        archive = cwd / "base.zip"
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "archive",
                "--format=zip",
                "-o",
                str(archive),
                commit,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise Pending(f"base commit unavailable: {commit}")
        with zipfile.ZipFile(archive) as z:
            z.extractall(cwd)
        archive.unlink()
        # git apply must run outside this project's checkout.
        for diff in (patch.read_text(), meta.test_patch):
            applied = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                input=diff,
                cwd=cwd,
                capture_output=True,
                text=True,
            )
            require(
                not applied.returncode, "patch did not apply: " + applied.stderr[:300]
            )
        paths = [cwd / "src", cwd]
        if chess_task:
            chess_spec = importlib.util.find_spec("chess")
            if not chess_spec:
                raise Pending("install chess==1.11.2 to run local regression tests")
            paths.append(Path(chess_spec.origin).parents[1])
        env = environment(cwd, paths)
        # Command is from the trusted course manifest, never from the submission.
        import shlex

        argv = shlex.split(meta.test_cmd.replace("{python}", "python"))
        if argv[0] == "python":
            argv[0] = sys.executable
        try:
            proc = subprocess.run(
                argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=600
            )
        except subprocess.TimeoutExpired as exc:
            raise Pending("patch testbed timeout") from exc
        output = proc.stdout + "\n" + proc.stderr
        results = parse_report(output, meta.framework)
        if not results and ("ModuleNotFoundError" in output or "ImportError" in output):
            raise Pending("testbed dependency missing: " + output[-320:])
        expected = (*meta.fail_to_pass, *meta.pass_to_pass)
        missing = [t for t in expected if results.get(t) != TestStatus.PASSED]
        require(
            not missing and proc.returncode == 0,
            f"unpassed tests: {missing}; exit {proc.returncode}",
        )
        return f"fresh {commit[:10]}: FAIL_TO_PASS {len(meta.fail_to_pass)}, PASS_TO_PASS {len(meta.pass_to_pass)}"


def prior_django_replay(log_path, patch_path, spec):
    """Bind a grader-selected historical success log to these exact patch bytes and test IDs."""
    from assignment.eval.harness import Report, TestStatus

    try:
        raw = log_path.read_bytes()
    except OSError as exc:
        raise Pending("cannot read the grader-selected Django replay log") from exc
    patch = patch_path.read_bytes()
    expected = Report(
        task_id=spec.task_id,
        patch_applied=True,
        fail_to_pass=dict.fromkeys(spec.fail_to_pass, TestStatus.PASSED),
        pass_to_pass=dict.fromkeys(spec.pass_to_pass, TestStatus.PASSED),
    )
    start = raw.find(b"\ndiff --git ")
    prefix = raw[:start].splitlines()
    image_line = f"INFO Using published image for {spec.task_id}: {spec.image}".encode()
    if (
        start < 0
        or image_line not in prefix
        or raw[start + 1 :] != patch + b"\n" + expected.summary().encode() + b"\n"
    ):
        raise Pending(
            "prior log does not attest this exact patch, image and complete passing suite; rerun without --django-replay-log"
        )
    return dict(
        source="verified previous replay",
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        fail_to_pass=len(spec.fail_to_pass),
        pass_to_pass=len(spec.pass_to_pass),
    )


def patch_replay(path, chess_task, args, trusted):
    from assignment.eval import EvaluationSpec, Task, evaluate
    from assignment.eval.instances import load

    require(path.is_file(), f"missing {path.name}")
    task = Task.load(ROOT / "tasks/chess-terminal-move") if chess_task else None
    spec = EvaluationSpec.load(args.chess_evaluation) if chess_task else load(INSTANCE)
    if not chess_task and args.django_replay_log:
        return prior_django_replay(args.django_replay_log, path, spec)
    # A trusted submission with no local Django checkout uses the course's pinned test image.
    auto_django = (
        not chess_task and trusted and args.django_source is None and not args.offline
    )
    if args.modal_replay or auto_django:
        try:
            report = evaluate(spec, patch=path.read_text(), task=task)
        except Exception as exc:
            raise Pending(f"Modal replay unavailable ({type(exc).__name__})") from exc
        require(report.resolved, report.summary())
        return report.summary()
    if not trusted:
        raise Pending(
            "local patch execution needs --trust-code or an isolated grading environment"
        )
    if chess_task:
        return local_patch(path, task.source, task, spec)
    return local_patch(path, args.django_source, spec)
