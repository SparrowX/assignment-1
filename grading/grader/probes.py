"""Offline behavioral checks supplementing the public tests. Uses their deterministic fixtures."""

import base64
import json
import shlex
from pathlib import Path

import httpx
import pytest
from test_agent import (
    TASK,
    FakeEnvironment,
    make_action,
    make_completion,
    make_raw_action,
    make_text,
)
from test_chess_agent import (
    INITIAL_STATE,
    UPDATED_STATE,
    FakeChessEnvironment,
    tool_call,
)

from assignment.agent import ChessAgent, CodeAgent, StepLimitError

pytest_plugins = ("test_agent",)


def call(name, args, id="call_0"):
    return {
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_prompt(make_agent):
    a, requests = make_agent(
        [
            make_action("call_0", "ls -la"),
            make_action("call_0", "python hello.py"),
            make_text("next"),
        ]
    )
    with pytest.raises(StepLimitError):
        a.run()
    msgs = requests[-1]["messages"]
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    info = {k: getattr(a.env, k) for k in ("machine", "release", "system", "version")}
    assert (
        "<system_information>\n"
        + json.dumps(info, indent=2)
        + "\n</system_information>"
        in msgs[0]["content"]
    )
    assert TASK in msgs[1]["content"] and "patch.txt" not in msgs[0]["content"]
    assert [m["tool_call_id"] for m in msgs if m["role"] == "tool"] == [
        "call_0",
        "call_0",
    ]


def test_cleanup_text(make_agent, tmp_path):
    a, requests = make_agent(
        [
            make_text("need another step"),
            make_raw_action("done", "send_message", '{"summary":"fixed"}'),
        ]
    )
    stopped = []
    a.env.stop = lambda: stopped.append(True)
    a.run()
    assert a.finished and len(requests) == 2 and stopped == [True]
    assert any(m.get("content") == "need another step" for m in requests[1]["messages"])
    log = json.loads(Path(a.logs_save_path).read_text())
    assert len(log["responses"]) == len(log["prompts"]) == 2
    a, _ = make_agent([make_text("step before failure")])
    a.env.stop = lambda: stopped.append(True)
    a.logs_save_path = str(tmp_path / "interrupted.json")
    a.step_limit = 2
    with pytest.raises(IndexError):
        a.run()
    assert stopped == [True, True]
    interrupted = json.loads(Path(a.logs_save_path).read_text())
    assert "step before failure" in json.dumps(interrupted["responses"])


def test_dispatch(make_agent):
    a, _ = make_agent([])
    assert {t["function"]["name"] for t in a.tools} == {"execute", "send_message"}
    seen = []

    def execute(**kwargs):
        seen.append(kwargs)
        return {"output": "test-patch", "returncode": 0}

    a.env.execute = execute
    args = {"command": "cat patch.txt", "cwd": "/testbed", "timeout": 3}
    obs = a.execute_tool_calls(
        [call("execute", args, "r"), call("send_message", {"summary": "fixed"}, "s")]
    )
    assert seen == [args] and a.finished
    assert [x["tool_call_id"] for x in obs] == ["r", "s"] and "test-patch" in obs[0][
        "content"
    ]


def test_dispatch_continues_after_bad_calls(make_agent):
    agent, _ = make_agent([])
    bad = call("execute", {}, "malformed")
    bad["function"]["arguments"] = "{"
    calls = [bad, call("missing_tool", {}, "unknown"), call("execute", {"command": "ls -la"}, "ok")]
    results = agent.execute_tool_calls(calls)
    assert agent.env.commands == ["ls -la"]
    assert [r["tool_call_id"] for r in results] == ["malformed", "unknown", "ok"]
    assert all(r["role"] == "tool" and r["content"].strip() for r in results)
    assert "total 4" in results[-1]["content"] and not agent.finished


def test_skills(make_agent, tmp_path):
    skill = tmp_path / "skills" / "different-directory" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    body = "---\nname: example\ndescription: find example\n---\nsecret instructions\n"
    skill.write_text(body)
    a = CodeAgent(
        TASK, FakeEnvironment(), model="test", skills_path=str(skill.parent.parent)
    )
    assert (
        "find example" in a.system_prompt
        and "secret instructions" not in a.system_prompt
    )
    obs = a.execute_tool_calls([call("invoke_skill", {"name": "example"})])
    assert obs[0]["tool_call_id"] == "call_0" and body.strip() in obs[0]["content"]
    other = skill.parent.parent / "duplicate" / "SKILL.md"
    other.parent.mkdir()
    for bad in (body, "not frontmatter"):
        other.write_text(bad)
        with pytest.raises(ValueError):
            CodeAgent(
                TASK,
                FakeEnvironment(),
                model="test",
                skills_path=str(skill.parent.parent),
            )


def test_retention(make_agent):
    recent = make_action("call_0", "ls -la")
    recent["tool_calls"].append(
        call("execute", {"command": "python hello.py"}, "other")
    )
    a, _ = make_agent([{}, {}, {}])
    actions = iter([make_action("call_0", "cat f.txt"), recent, make_text("next")])
    requests = []

    def create(**kw):
        if "tools" in kw:
            requests.append(kw["messages"])
            return make_completion(next(actions))
        return make_completion(make_text("old context summarized"))

    a.client.chat.completions.create = create
    a.compact_threshold_tokens = 1
    with pytest.raises(StepLimitError):
        a.run()
    p = requests[-1]
    assert p[:2] == requests[0][:2] and a.compaction_events
    assert [m["tool_call_id"] for m in p if m["role"] == "tool"][-2:] == [
        "call_0",
        "other",
    ]
    assert any(m.get("tool_calls") == recent["tool_calls"] for m in p)
    assert "hello, world" in p[-1]["content"]


@pytest.fixture
def program(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:0/v1")
    requests = []
    end = {**UPDATED_STATE, "game_over": True}

    def handler(req):
        body = json.loads(req.content or b"{}")
        requests.append((req.method, req.url.path, body))
        if req.url.path == "/api/reset":
            return httpx.Response(200, json=INITIAL_STATE)
        if req.url.path in ("/api/move", "/api/state"):
            return httpx.Response(200, json=end)
        if req.url.path == "/api/simulate" and body.get("fen") == "bad":
            raise httpx.ConnectError("transport failure")
        if req.url.path == "/api/simulate" and body.get("move") == "e2e5":
            return httpx.Response(400, json={"error": "illegal move"})
        if req.url.path == "/api/simulate":
            return httpx.Response(200, json={**UPDATED_STATE, "simulation": True})
        return httpx.Response(404, json={})

    with httpx.Client(
        base_url="http://chess.test", transport=httpx.MockTransport(handler)
    ) as client:
        a = ChessAgent(
            FakeChessEnvironment(),
            model="test",
            http_client=client,
            programmatic_tools=True,
        )
        yield a, requests


def test_play_batch(program):
    a, requests = program
    obs = a.execute_tool_calls([tool_call("e2e4", "a"), tool_call("d2d4", "b")])
    assert [m["tool_call_id"] for m in obs] == ["a", "b"] and "<chess_error>" in obs[1][
        "content"
    ]
    assert (
        sum(p == "/api/move" for _, p, _ in requests) == 1
        and a.finished
        and a.last_state["game_over"]
    )


def test_simulate_state(program):
    a, req = program
    assert "simulate_move" in {t["function"]["name"] for t in a.tools}
    for params in ({"fen": "position"}, {"fen": "position", "move": "e2e4"}):
        obs = a.execute_tool_calls([call("simulate_move", params)])[0]
        assert (
            obs["tool_call_id"] == "call_0" and json.loads(obs["content"])["simulation"]
        )
        method, path, payload = req[-1]
        assert (method, path, payload["fen"], payload.get("move")) == (
            "POST",
            "/api/simulate",
            "position",
            params.get("move"),
        )
        assert a.last_state == INITIAL_STATE and not a.finished


def test_simulate_errors(program):
    a, _ = program
    malformed = call("simulate_move", {})
    malformed["function"]["arguments"] = "{"
    params = ([1], {}, {"fen": 0}, {"fen": "bad"}, {"fen": "position", "move": 3},
              {"fen": "position", "move": "e2e5"})
    for invocation in [malformed, *(call("simulate_move", p) for p in params)]:
        observation = a.execute_tool_calls([invocation])[0]
        assert "<chess_error>" in observation["content"] and "</chess_error>" in observation["content"]
        assert observation["tool_call_id"] == invocation["id"]
        assert a.last_state == INITIAL_STATE and not a.finished


def test_python_success(program):
    a, requests = program
    code = "print('quotes, 雪, $ and backticks')"
    stdout = json.dumps({"stdout": "ok", "stderr": "", "error": None})

    def execute(command, **kwargs):
        parts = shlex.split(command)
        assert parts[-3:-1] == ["/opt/assignment/sandbox_python.py", "8000"]
        assert base64.b64decode(parts[-1]).decode() == code
        return dict(stdout=stdout, output=stdout, returncode=0)

    a.env.execute = execute
    obs = a.execute_tool_calls([call("run_python", {"code": code})])[0]
    assert (
        obs["tool_call_id"] == "call_0"
        and stdout in obs["content"]
        and UPDATED_STATE["fen"] in obs["content"]
    )
    assert requests[-1][:2] == ("GET", "/api/state") and a.finished


def test_python_errors(program):
    a, requests = program
    code = "play_move('e2e4'); raise RuntimeError('after move')"
    # The sandbox command can succeed even when the submitted snippet raises after a legal move.
    envelope = json.dumps({"stdout": "", "stderr": "traceback", "error": "RuntimeError: after move"})
    a.env.execute = lambda *args, **kw: {"returncode": 0, "stdout": envelope, "output": envelope}
    observed = a.execute_tool_calls([call("run_python", {"code": code})])[0]["content"]
    assert envelope in observed and a.finished and a.last_state["fen"] == UPDATED_STATE["fen"]
    assert requests[-1][:2] == ("GET", "/api/state")

    a.env.execute = lambda *args, **kw: {"returncode": 1, "stderr": "sandbox failed"}
    assert (
        "sandbox failed"
        in a.execute_tool_calls([call("run_python", {"code": code})])[0]["content"]
    )
    assert (
        "<chess_error>"
        in a.execute_tool_calls([call("run_python", {"code": False})])[0]["content"]
    )


def test_summary_trigger(make_agent):
    responses = [
        make_action("old", "cat f.txt"),
        make_action("new", "ls -la"),
        make_text("continue"),
    ]
    agent, _ = make_agent(responses)
    agent.compact_threshold_tokens = 1
    scripted = iter(responses)
    summaries = []

    def create(**kwargs):
        if "tools" in kwargs:
            return make_completion(next(scripted))
        summaries.append(kwargs["messages"])
        return make_completion(make_text("A distinctive summary of the older work."))

    agent.client.chat.completions.create = create
    with pytest.raises(StepLimitError):
        agent.run()
    assert len(summaries) == 1 and "cat f.txt" in json.dumps(summaries[0])
    assert "A distinctive summary of the older work." in json.dumps(
        agent.api_prompts[-1]
    )


def test_schema_details(program):
    agent, _ = program
    spec = next(
        t["function"] for t in agent.tools if t["function"]["name"] == "play_move"
    )
    assert set(spec["parameters"]["properties"]) == {"move"}
    assert "uci" in json.dumps(spec).lower()
