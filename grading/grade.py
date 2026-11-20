"""Grade Assignment 1 from a working tree or submission ZIP using recorded evidence."""

import argparse
import hashlib
import json
import sys
import zipfile
from contextlib import redirect_stdout
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

from grader.evidence import (
    Pending,
    Trace,
    compact,
    replay_game,
    require,
    skill_replay,
    usage,
    verify_coding_trace,
)
from grader.execution import patch_replay, run_probes, tests_for
from grader.reports import judge_report, observation_table, token_table
from grader.submission import (
    INSTANCE,
    ROOT,
    STARTER,
    ab_name,
    model_tags,
    restore_starter,
    stage_submission,
)


class Scorecard:
    def __init__(self):
        self.rows = []

    def row(self, part, key, title, checks):
        earned = pending = maximum = 0
        details = []
        for points, check in checks:
            maximum += points
            try:
                evidence = check()
                # Manual report review can award partial credit.
                score = (
                    evidence.get("awarded", points)
                    if isinstance(evidence, dict)
                    else points
                )
                earned += score
                status = "pass" if score == points else "partial" if score else "fail"
            except Pending as exc:
                score, status, evidence = 0, "pending", str(exc)
                pending += points
            except (
                ValueError,
                KeyError,
                IndexError,
                TypeError,
                OSError,
                AttributeError,
                RecursionError,
                SyntaxError,
            ) as exc:
                score, status, evidence = 0, "fail", str(exc)
            details.append(
                dict(status=status, earned=score, maximum=points, evidence=evidence)
            )
        self.rows.append(
            dict(
                part=part,
                id=key,
                criterion=title,
                earned=earned,
                maximum=maximum,
                pending=pending,
                checks=details,
            )
        )

    def result(self):
        assert len(self.rows) == 19 and sum(r["maximum"] for r in self.rows) == 100
        return dict(
            earned=sum(r["earned"] for r in self.rows),
            maximum=100,
            pending=sum(r["pending"] for r in self.rows),
            rows=self.rows,
        )


def grade(source, stage, args, reviews):
    issues = stage_submission(source, stage)
    art = stage / "artifacts"
    trusted = source == ROOT or args.trust_code

    @lru_cache
    def read(name):
        return json.loads((art / name).read_text())

    @lru_cache
    def trace(name):
        return Trace(read(name + ".json"))

    @lru_cache
    def unit_results(key):
        if not trusted:
            raise Pending(
                "use --trust-code for a trusted ZIP/directory or run inside your isolated grader"
            )
        return run_probes(stage, key)

    def unit(key, *names):
        return lambda: tests_for(unit_results(key), key, names or None)

    def comp(mode):
        return lambda: compact(trace(INSTANCE + "-trajectory"), mode)

    def report_row(key, filename, evidence, match):
        @lru_cache
        def verdict():
            report = (art / filename).read_text().strip()
            require(report, "empty report")
            facts = evidence()
            # Cache a provider failure too, so the second criterion never sends a duplicate request.
            try:
                return judge_report(key, report, facts, match(report, facts)), None
            except Pending as exc:
                return None, str(exc)

        def inspect(quantitative):
            report = (art / filename).read_text().strip()
            require(report, "empty report")
            facts = evidence()
            if quantitative and match(report, facts):
                return facts
            if args.offline:
                raise Pending(f"offline report review: {filename}")
            grades, error = verdict()
            if error:
                raise Pending(error)
            return grades["numbers" if quantitative else "analysis"]

        entry = reviews.get(key)
        report_path = art / filename
        if entry and report_path.is_file() and (
            "report_sha256" not in entry
            or hashlib.sha256(report_path.read_bytes()).hexdigest() == entry["report_sha256"]
        ):

            def reviewed():
                require((art / filename).read_text().strip(), "empty report")
                return dict(reviews[key])

            return [(4, reviewed)]
        return [(2, lambda: inspect(True)), (2, lambda: inspect(False))]

    @lru_cache
    def token_evidence():
        metrics = {}
        for label, path in [
            ("compacted", art / (INSTANCE + "-trajectory.json")),
            (
                "baseline",
                args.baseline
                or source / "artifacts" / (INSTANCE + "-baseline-trajectory.json"),
            ),
        ]:
            try:
                metrics[label] = usage(Trace(json.loads(path.read_text())))
            except (
                OSError,
                KeyError,
                ValueError,
                TypeError,
                AttributeError,
                IndexError,
            ) as exc:
                metrics[label] = f"unavailable ({type(exc).__name__})"
        return metrics

    @lru_cache
    def ab_runs():
        """One point per verified condition, at most two distinct models."""
        runs = {}
        failures = []
        for tag in sorted(model_tags(stage)):
            by_model = {}
            for legal in (False, True):
                stem = ab_name(tag, legal)
                try:
                    metrics = replay_game(
                        trace(stem), read(stem + "-result.json"), legal
                    )
                    by_model.setdefault(metrics["model"], {})[legal] = dict(
                        file=stem, **metrics
                    )
                except Pending as exc:
                    failures.append(("pending", stem, str(exc)))
                except (
                    KeyError,
                    ValueError,
                    TypeError,
                    OSError,
                    AttributeError,
                    IndexError,
                ) as exc:
                    failures.append(("fail", stem, str(exc)))
            if len(by_model) > 1:
                failures.append(
                    ("fail", tag, "model changed between paired conditions")
                )
                continue
            for model, conditions in by_model.items():
                runs.setdefault(model, {}).update(conditions)
        best = sorted(runs, key=lambda m: (-len(runs[m]), m))[:2]
        return [runs[m] for m in best], failures

    def ab(slot, legal):
        def check():
            groups, failures = ab_runs()
            if len(groups) > slot and legal in groups[slot]:
                return groups[slot][legal]
            if any(f[0] == "pending" for f in failures):
                raise Pending(str(failures))
            raise ValueError("missing consistent model/condition: " + str(failures))

        return check

    def archive_check():
        if not issues:
            verify_coding_trace(
                trace("part1-trajectory"), (art / "fix.patch").read_text()
            )
            verify_coding_trace(
                trace(INSTANCE + "-trajectory"),
                (art / (INSTANCE + ".patch")).read_text(),
            )
        if issues:
            raise ValueError(" | ".join(issues))
        return "validated ZIP layout, allowed paths, required source and 17 parseable artifacts"

    card = Scorecard()

    def row(part, key, title, points, check):
        card.row(part, key, title, [(points, check)])

    row(1, "prompt", "Prompt and linked history", 6, unit("prompt"))
    row(1, "lifecycle", "ReAct lifecycle, cleanup and trajectory", 6, unit("lifecycle"))
    row(
        1,
        "dispatch",
        "Coding tools, recoverable errors and submission",
        6,
        unit("dispatch"),
    )
    row(
        1,
        "chess_patch",
        "Chess patch replay",
        8,
        lambda: patch_replay(art / "fix.patch", True, args, trusted),
    )
    row(1, "skills", "Skill discovery and invocation", 4, unit("skills"))
    card.row(
        2,
        "trigger",
        "Compaction trigger and generated summary",
        [(3, unit("trigger")), (3, comp("summary"))],
    )
    card.row(
        2,
        "retain",
        "Original instructions and complete recent step",
        [(3, unit("retain")), (3, comp("retain"))],
    )
    row(2, "reduce", "Auditable material reduction", 4, comp("reduce"))
    card.row(
        2,
        "tokens",
        "Full-context versus compacted usage report",
        report_row("tokens", "token-usage-analysis.md", token_evidence, token_table),
    )
    row(
        2,
        "django_patch",
        "SWE-bench patch replay",
        8,
        lambda: patch_replay(art / (INSTANCE + ".patch"), False, args, trusted),
    )
    row(3, "schema", "play_move schema and registration", 4, unit("schema"))
    row(3, "play", "play_move updates and recoverable errors", 6, unit("play"))
    row(
        3,
        "terminal",
        "Basic game reaches a terminal position",
        4,
        lambda: replay_game(
            trace("part3-trajectory"), read("game-result.json"), terminal=True
        ),
    )
    card.row(
        3,
        "ab",
        "Observation experiment, two models x two conditions",
        [(1, ab(slot, legal)) for slot in range(2) for legal in (False, True)],
    )
    card.row(
        3,
        "observation",
        "Observation experiment report",
        report_row(
            "observation",
            "observation-experiment.md",
            lambda: ab_runs()[0],
            observation_table,
        ),
    )
    card.row(3, "simulate", "Stateless simulation and errors", [
        (3, unit("simulate", "test_simulate_state")),
        (3, unit("simulate", "test_simulate_errors")),
    ])
    card.row(3, "python", "Sandbox Python execution, refresh and errors", [
        (3, unit("python", "test_python_success")),
        (3, unit("python", "test_python_errors")),
    ])
    row(
        3,
        "skill_game",
        "Skill, programmatic search and live move",
        6,
        lambda: skill_replay(trace("part3-python-skill-trajectory")),
    )
    row("submission", "archive", "Complete parseable submission ZIP", 2, archive_check)
    return card.result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submission", nargs="?", type=Path, default=ROOT)
    parser.add_argument(
        "--trust-code",
        action="store_true",
        help="execute source/patches from a trusted submission locally",
    )
    parser.add_argument(
        "--starter-ref", default=STARTER, help="trusted course starter git commit"
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="optional full-context trajectory for report review",
    )
    parser.add_argument(
        "--review", type=Path, help="grader-supplied scores for the two written reports"
    )
    parser.add_argument(
        "--django-source",
        type=Path,
        help="Django git checkout containing the vendored base commit",
    )
    parser.add_argument(
        "--django-replay-log",
        type=Path,
        help="reuse an explicitly trusted course replay log for the identical patch and tests",
    )
    parser.add_argument(
        "--chess-evaluation",
        type=Path,
        default=ROOT / "tasks/chess-terminal-move/public_tests",
    )
    parser.add_argument(
        "--modal-replay",
        action="store_true",
        help="use the course's billable Modal patch testbeds",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable results"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="disable report model calls and automatic Django Modal fallback",
    )
    args = parser.parse_args()
    if args.offline and args.modal_replay:
        parser.error("--offline and --modal-replay cannot be combined")
    if args.django_replay_log and (args.django_source or args.modal_replay):
        parser.error("choose a fresh Django replay or a previous replay log")
    source = args.submission.resolve()
    if not source.exists():
        parser.error("submission does not exist")
    reviews = {}
    if args.review:
        try:
            reviews = json.loads(args.review.read_text())
            require(
                isinstance(reviews, dict)
                and reviews.keys() <= {"tokens", "observation"},
                "unknown review key",
            )
            for entry in reviews.values():
                require(
                    type(entry["awarded"]) is int
                    and 0 <= entry["awarded"] <= 4
                    and isinstance(entry["reason"], str)
                    and entry["reason"].strip(),
                    "need awarded 0..4 and reason",
                )
        except (ValueError, OSError, TypeError, KeyError) as exc:
            parser.error(str(exc))
    with TemporaryDirectory() as temp:
        stage = Path(temp)
        restore_starter(stage, args.starter_ref)
        try:
            # Some testbed dependencies print startup messages to stdout.
            with redirect_stdout(sys.stderr):
                result = grade(source, stage, args, reviews)
        except zipfile.BadZipFile:
            parser.error("unreadable submission ZIP")
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for r in result["rows"]:
            print(
                f"{str(r['part']):>10}  {r['earned']:>2}/{r['maximum']:<2}  {r['criterion']}"
            )
            for c in r["checks"]:
                print(f"                   {c['status']}: {c['evidence']}")
        print(
            f"Earned {result['earned']}/100 | Pending {result['pending']} | Lost {100 - result['pending'] - result['earned']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
