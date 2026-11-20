"""Regression tests for the grader. No LLM client, Modal account or archive execution."""

import json
from copy import deepcopy
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from grade import grade

from grader.evidence import Trace, replay_game
from grader.reports import token_table
from grader.submission import ROOT, ab_name, stage_submission


def test_repeated_ids_bind_to_their_own_action():
    base = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
    ]
    actions = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call_0", "function": {"name": "execute", "arguments": str(i)}}
            ],
        }
        for i in range(3)
    ]
    prompts = [base]
    for i in range(2):
        prompts.append(
            [
                *prompts[-1],
                actions[i],
                {"role": "tool", "tool_call_id": "call_0", "content": f"answer {i}"},
            ]
        )
    data = {
        "prompts": prompts,
        "responses": [{"choices": [{"message": a}]} for a in actions],
        "compactions": [],
    }
    assert [out for _, _, out in Trace(data).steps()] == ["answer 0", "answer 1", None]
    broken = deepcopy(data)
    broken["prompts"][-1][-2] = actions[0]
    with pytest.raises(ValueError, match="mismatch"):
        Trace(broken)


def test_zip_skips_forbidden_members_but_preserves_other_evidence(tmp_path):
    archive = tmp_path / "sub.zip"
    with ZipFile(archive, "w") as z:
        for name in ["../escape", ".env", "src/assignment/agent/tests/helper.py"]:
            z.writestr(name, "forbidden")
        z.writestr("AI_USAGE.md", "Reviewed with ChatGPT.")
    dest = tmp_path / "stage"
    issues = stage_submission(archive, dest)
    assert all(
        any(name in issue for issue in issues)
        for name in ["../escape", ".env", "helper.py"]
    )
    assert not (tmp_path / "escape").exists()
    assert (dest / "AI_USAGE.md").is_file()


def test_token_report_needs_matching_totals_in_correct_columns():
    facts = {
        "baseline": {"compactions": 0, "total_tokens": 6543},
        "compacted": {"compactions": 1, "total_tokens": 4321},
    }
    report = "| 指标 | 无压缩 | 压缩 |\n| 总 tokens | 6,543 | 4,321 |"
    assert token_table(report, facts)
    assert not token_table(
        report.replace("| 无压缩 | 压缩 |", "| 压缩 | 无压缩 |"), facts
    )


@pytest.fixture
def played_game():
    chess = pytest.importorskip("chess")
    board = chess.Board()
    prompt = [
        {"role": "system", "content": "play"},
        {
            "role": "user",
            "content": "<chess_state>\nfen: "
            + board.fen()
            + "\nlegal_moves: "
            + " ".join(m.uci() for m in board.legal_moves),
        },
    ]
    action = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call_0",
                "function": {"name": "play_move", "arguments": '{"move":"e2e4"}'},
            }
        ],
    }
    data = {
        "prompts": [prompt],
        "responses": [{"model": "family-model", "choices": [{"message": action}]}],
        "compactions": [],
    }
    h = []
    for i, uci in enumerate(["e2e4", "e7e5"]):
        move = board.parse_uci(uci)
        h.append(
            dict(
                ply=i + 1,
                color="white" if board.turn else "black",
                san=board.san(move),
                uci=uci,
            )
        )
        board.push(move)
    result = dict(
        history=h,
        fen=board.fen(),
        game_over=False,
        status="Your turn",
        turn="white",
        in_check=False,
        squares={
            chess.square_name(s): p.symbol() for s, p in board.piece_map().items()
        },
    )
    return data, result


def test_terminal_requires_real_outcome(played_game):
    data, result = played_game
    assert replay_game(Trace(data), result)["plies"] == 2
    with pytest.raises(ValueError, match="terminal"):
        replay_game(Trace(data), result, terminal=True)
    result["game_over"] = True
    with pytest.raises(ValueError, match="mismatch"):
        replay_game(Trace(data), result)


@pytest.mark.parametrize("duplicate,expected", [(False, 4), (True, 2)])
def test_any_two_actual_models(tmp_path, played_game, duplicate, expected):
    data, result = played_game
    archive = tmp_path / "two.zip"
    with ZipFile(archive, "w") as z:
        for tag in ("a", "b"):
            for legal in (True, False):
                run = deepcopy(data)
                if not legal:
                    run["prompts"][0][1]["content"] = run["prompts"][0][1][
                        "content"
                    ].split("\nlegal_moves:")[0]
                run["responses"][0]["model"] = (
                    "vendor/model-a" if duplicate else "vendor/model-" + tag
                )
                stem = "artifacts/" + ab_name(tag, legal)
                z.writestr(stem + ".json", json.dumps(run))
                z.writestr(stem + "-result.json", json.dumps(result))
    args = SimpleNamespace(
        trust_code=False,
        offline=True,
        django_replay_log=None,
        baseline=None,
        modal_replay=False,
        django_source=None,
        chess_evaluation=ROOT / "tasks/chess-terminal-move/public_tests",
    )
    report = grade(archive, tmp_path / "stage", args, {})
    rows = {r["id"]: r for r in report["rows"]}
    assert len(rows) == 19 and report["maximum"] == 100
    assert rows["ab"]["earned"] == expected and rows["archive"]["earned"] == 0


def test_black_reply_must_match_course_bot(played_game):
    data, result = played_game
    result["history"][1]["uci"] = "c7c5"  # Legal, but the deterministic bot plays e7e5.
    with pytest.raises(ValueError, match="deterministic"):
        replay_game(Trace(data), result)


def test_absent_source_uses_course_starter(tmp_path):
    from grader.execution import run_probes, tests_for
    from grader.submission import STARTER, restore_starter

    restore_starter(tmp_path, STARTER)
    results = run_probes(tmp_path, "prompt")
    with pytest.raises(ValueError, match="test_prompt"):
        tests_for(results, "prompt")


def test_replay_log_binds_exact_diff_and_all_tests(tmp_path):
    from assignment.eval.instances import load
    from grader.evidence import Pending
    from grader.execution import prior_django_replay

    spec = load("django__django-15368")
    log = ROOT / "artifacts" / f"{spec.task_id}-check.log"
    patch = ROOT / "artifacts" / f"{spec.task_id}.patch"
    assert prior_django_replay(log, patch, spec)["pass_to_pass"] == len(
        spec.pass_to_pass
    )
    altered = tmp_path / "other.patch"
    altered.write_bytes(patch.read_bytes() + b"\n")
    with pytest.raises(Pending):
        prior_django_replay(log, altered, spec)
    incomplete = tmp_path / "missing-test.log"
    incomplete.write_bytes(
        log.read_bytes().replace(spec.pass_to_pass[-1].encode(), b"unrelated_test")
    )
    with pytest.raises(Pending):
        prior_django_replay(incomplete, patch, spec)


@pytest.mark.parametrize(("awarded", "trim", "accepted"), [
    (1, 0, True), (True, 0, False), (5, 0, False),
    (2, 1, True), (2, 2, False), (5, 1, False),
])
def test_report_model_uses_grader_env_and_checks_response(
    tmp_path, monkeypatch, awarded, trim, accepted
):
    import httpx
    import openai

    from grader import submission
    from grader.evidence import Pending
    from grader.reports import judge_report

    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=fake-key\nOPENAI_BASE_URL=https://judge.invalid/v1\nOPENAI_MODEL=test-model\n"
    )
    monkeypatch.setattr(submission, "ROOT", tmp_path)

    def handler(request):
        assert request.url.host == "judge.invalid"
        data = json.loads(request.content)
        assert data["model"] == "test-model" and data["response_format"] == {
            "type": "json_object"
        }
        payload = json.loads(data["messages"][1]["content"])
        assert payload["report"] == "A report" and payload["replay_metrics"] == {
            "compactions": 7
        }
        assert "fake-key" not in str(data)
        verdict = {
            k: {"score": awarded, "reason": "Partial interpretation."}
            for k in ("numbers", "analysis")
        }
        return httpx.Response(
            200,
            json={
                "id": "judge-1",
                "model": "test-model",
                "created": 1,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(verdict)[:-trim or None],
                        },
                    }
                ],
            },
        )

    cls = openai.OpenAI
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kw: cls(
            **kw, http_client=httpx.Client(transport=httpx.MockTransport(handler))
        ),
    )
    if accepted:
        assert (
            judge_report("tokens", "A report", {"compactions": 7}, False)["analysis"][
                "awarded"
            ]
            == awarded
        )
    else:
        with pytest.raises(Pending, match="invalid JSON verdict"):
            judge_report("tokens", "A report", {"compactions": 7}, False)


def test_saved_review_applies_only_to_reviewed_report(tmp_path):
    import hashlib

    source = tmp_path / "submission"
    artifacts = source / "artifacts"
    artifacts.mkdir(parents=True)
    report = artifacts / "token-usage-analysis.md"
    report.write_text("Reviewed version")
    review = {"tokens": {"awarded": 3, "reason": "Recorded assessment",
                         "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()}}
    args = SimpleNamespace(trust_code=False, baseline=None, offline=True, modal_replay=False,
                           django_source=None, django_replay_log=None,
                           chess_evaluation=ROOT / "tasks/chess-terminal-move/public_tests")
    for i, (content, earned, pending) in enumerate([("Reviewed version", 3, 0), ("Replacement", 0, 4)]):
        report.write_text(content)
        score = grade(source, tmp_path / f"stage{i}", args, review)
        row = next(row for row in score["rows"] if row["id"] == "tokens")
        assert (row["earned"], row["pending"]) == (earned, pending)
