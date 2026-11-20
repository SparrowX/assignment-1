"""Read the runner's actual chat transcripts. Never execute submitted snippets."""

import ast
import json
import math
import re
import sys
from collections import Counter

from .submission import ROOT


def require(condition, message):
    if not condition:
        raise ValueError(message)


def message(response):
    msg = response["choices"][0]["message"]
    require(msg["role"] == "assistant", "missing assistant action")
    return msg


def normalized(obj):
    if isinstance(obj, dict):
        return {k: normalized(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [normalized(v) for v in obj]
    return obj


def observations(action, tail):
    """Consume tool observations belonging to this action only (IDs can recur)."""
    calls = action.get("tool_calls") or []
    ids = [c["id"] for c in calls]
    require(len(set(ids)) == len(ids), "ambiguous tool IDs within one action")
    obs = tail[: len(ids)]
    require(
        all(x["role"] == "tool" and isinstance(x["content"], str) for x in obs)
        and Counter(x["tool_call_id"] for x in obs) == Counter(ids),
        "unlinked tool observations",
    )
    return {x["tool_call_id"]: x["content"] for x in obs}


class Trace:
    def __init__(self, data):
        self.prompts = data["prompts"]
        self.responses = data["responses"]
        self.events = data["compactions"]
        count = len(self.responses)
        require(
            count and len(self.prompts) == count,
            "unpaired or empty action requests/responses",
        )
        self.actions = [message(r) for r in self.responses]
        self.before = {}
        for event in self.events:
            step = event["step"]
            require(
                type(step) is int and 0 < step < count and step not in self.before,
                "compaction step does not identify a unique next request",
            )
            self.before[step] = event["active_prompt_before"]
        for i, prompt in enumerate(self.prompts):
            require(
                len(prompt) >= 2
                and prompt[0]["role"] == "system"
                and prompt[1]["role"] == "user",
                f"step {i + 1}: invalid opening messages",
            )
            if i + 1 < count:
                after = self.before.get(i + 1, self.prompts[i + 1])
                require(
                    after[: len(prompt)] == prompt,
                    f"step {i + 1}: unexplained history rewrite",
                )
                tail = after[len(prompt) :]
                action = self.actions[i]
                require(
                    tail and normalized(tail[0]) == normalized(action),
                    f"step {i + 1}: response/history mismatch",
                )
                calls = action.get("tool_calls") or []
                observations(action, tail[1:])
                require(
                    all(m["role"] == "user" for m in tail[1 + len(calls) :]),
                    "unexpected messages after action",
                )

    def steps(self):
        for i, action in enumerate(self.actions):
            obs = {}
            if i + 1 < len(self.prompts):
                after = self.before.get(i + 1, self.prompts[i + 1])
                obs = observations(action, after[len(self.prompts[i]) + 1 :])
            for call in action.get("tool_calls") or []:
                yield i, call["function"], obs.get(call["id"])


def usage(trace):
    action = [r["usage"]["total_tokens"] for r in trace.responses]
    summaries = [
        e["compaction_response"]["usage"]["total_tokens"] for e in trace.events
    ]
    require(
        all(type(n) is int and n > 0 for n in action + summaries),
        "missing positive usage counts",
    )
    return dict(
        actions=len(action),
        compactions=len(summaries),
        action_tokens=sum(action),
        summary_tokens=sum(summaries),
        total_tokens=sum(action + summaries),
    )


def compact(trace, mode):
    require(trace.events, "no recorded compactions")
    reductions = []
    for e in trace.events:
        i = e["step"]
        before, after = e["active_prompt_before"], trace.prompts[i]
        if mode == "summary":
            summary = message(e["compaction_response"])["content"]
            require(
                e["compaction_prompt"]
                and isinstance(summary, str)
                and summary.strip()
                and any(summary.strip() in (m.get("content") or "") for m in after[2:]),
                f"step {i}: generated summary absent from next prompt",
            )
        elif mode == "retain":
            require(
                before[:2] == after[:2] == trace.prompts[0][:2],
                "original instructions changed",
            )
            actions = [
                j
                for j, m in enumerate(before)
                if m["role"] == "assistant" and m.get("tool_calls")
            ]
            require(actions, "no complete recent tool action before compaction")
            j = actions[-1]
            observations(before[j], before[j + 1 :])
            group = before[j : j + 1 + len(before[j]["tool_calls"])]
            require(
                any(after[k : k + len(group)] == group for k in range(2, len(after))),
                "lost recent tool step",
            )
            # Check all tool messages still belong to a complete action, including older retained steps.
            k = 2
            while k < len(after):
                msg = after[k]
                require(
                    msg["role"] in ("assistant", "user"), "orphaned tool observation"
                )
                observations(msg, after[k + 1 :])
                k += 1 + len(msg.get("tool_calls") or [])
        else:

            def tokens(msgs):
                return max(
                    1,
                    math.ceil(
                        len(json.dumps(msgs, ensure_ascii=False, separators=(",", ":")))
                        / 4
                    ),
                )

            a, b = tokens(before), tokens(after)
            require(
                (a, b) == (e["estimated_tokens_before"], e["estimated_tokens_after"]),
                "recorded estimates disagree with actual prompts",
            )
            reductions.append((a - b) / a)
    if mode == "reduce":
        require(
            max(reductions) >= 0.10 and sum(reductions) > 0,
            "no material reduction (local threshold 10%)",
        )
        return dict(
            usage(trace), reductions_pct=[round(x * 100, 1) for x in reductions]
        )
    return f"{len(trace.events)} compaction events checked"


def field(text, key):
    m = re.search(r"^" + re.escape(key) + r":[ \t]*(.*)$", text, re.M)
    require(m, f"missing chess state field {key}")
    return m[1].strip()


def chess_module():
    try:
        import chess
    except ImportError as exc:
        raise Pending("chess==1.11.2 required for board replay") from exc
    return chess


class Pending(Exception):
    """Unavailable grader dependency or evidence requiring human judgment."""


def position(text, board, legal):
    require(
        "<chess_state>" in text and field(text, "fen") == board.fen(),
        "logged position differs from replay",
    )
    visible = bool(re.search(r"^legal_moves:", text, re.M))
    require(visible == legal, "legal-move observation condition mismatch")
    if legal:
        expected = (
            sorted(m.uci() for m in board.legal_moves)
            if not board.outcome(claim_draw=True)
            else []
        )
        values = field(text, "legal_moves").split()
        require(
            sorted([] if values == ["(none)"] else values) == expected,
            "incorrect list of legal moves",
        )


def replay_game(trace, result, legal=True, terminal=False):
    models = {r["model"].strip().casefold() for r in trace.responses}
    require(
        len(models) == 1 and all(models), "one run must use a single identified model"
    )
    chess = chess_module()
    game_source = str(ROOT / "chess_app" / "src")
    sys.path.insert(0, game_source)
    try:
        from chess_app.game import ChessGame
    except ImportError as exc:
        raise Pending(
            "initialize the trusted chess_app submodule to replay Black's replies"
        ) from exc
    finally:
        sys.path.remove(game_source)
    opponent = ChessGame()
    board = (
        opponent._board
    )  # Includes the full move stack for repetition-dependent draws.
    position(trace.prompts[0][1]["content"], board, legal)
    history = result["history"]
    cursor = calls = illegal = errors = 0

    def push(uci):
        nonlocal cursor
        move = board.parse_uci(uci)
        expected = dict(
            ply=cursor + 1,
            color="white" if board.turn else "black",
            san=board.san(move),
            uci=move.uci(),
        )
        require(
            cursor < len(history) and history[cursor] == expected,
            f"result history mismatch at ply {cursor + 1}",
        )
        board.push(move)
        cursor += 1

    for step, fn, output in trace.steps():
        require(fn["name"] == "play_move", f"unexpected tool {fn['name']}")
        calls += 1
        try:
            uci = json.loads(fn["arguments"])["move"].strip().lower()
            # The server promotes to a queen when the suffix is omitted.
            if len(uci) == 4 and uci + "q" in (m.uci() for m in board.legal_moves):
                uci += "q"
            move = board.parse_uci(uci)
            valid = bool(move) and not board.outcome(claim_draw=True)
        except (ValueError, TypeError, KeyError, AttributeError):
            valid = False
        if output is not None and "<chess_error>" in output:
            if re.search(r"not legal|illegal move", output, re.I):
                require(not valid, "legal move recorded as illegal")
                illegal += 1
            else:
                errors += 1
            continue
        if output is None and cursor == len(history) and not valid:
            illegal += 1
            continue
        require(valid, f"illegal successful move at step {step + 1}")
        push(uci)
        if not board.outcome(claim_draw=True):
            require(cursor < len(history), "missing Black reply")
            reply = history[cursor]["uci"]
            require(
                reply == opponent._choose_engine_move().uci(),
                f"wrong deterministic Black reply at ply {cursor + 1}",
            )
            if output is not None:
                require(field(output, "engine_move") == reply, "Black reply mismatch")
            push(reply)
        if output is not None:
            require(field(output, "human_move") == uci, "White move mismatch")
            position(output, board, legal)
    outcome = board.outcome(claim_draw=True)
    require(
        calls
        and cursor == len(history)
        and result["fen"] == board.fen()
        and result["game_over"] is bool(outcome),
        "final history, FEN or game_over mismatch",
    )
    expected_squares = {
        chess.square_name(square): piece.symbol()
        for square, piece in board.piece_map().items()
    }
    require(
        result["turn"] == ("white" if board.turn else "black")
        and result["squares"] == expected_squares
        and result["in_check"] is board.is_check(),
        "result board inconsistent with replay",
    )
    if outcome:
        status = {True: "White wins", False: "Black wins", None: "Draw"}[outcome.winner]
        require(result["status"] == status, "terminal outcome mismatch")
    require(not terminal or outcome, "basic game did not reach a terminal position")
    return dict(
        model=models.pop(),
        calls=calls,
        illegal=illegal,
        other_errors=errors,
        invalid_rate=round(illegal / calls, 4),
        game_over=bool(outcome),
        plies=cursor,
        status=result["status"],
    )


def skill_replay(trace):
    loaded = False
    fen = field(trace.prompts[0][1]["content"], "fen")
    for i, fn, output in trace.steps():
        if not output:
            continue  # last response has no logged tool result
        if fn["name"] == "invoke_skill":
            loaded = (
                json.loads(fn["arguments"]).get("name") == "select-move"
                and "name: select-move" in output
                and "simulate_move" in output
            )
        elif fn["name"] == "run_python" and "<chess_state>" in output:
            envelope, _, state = output.partition("<chess_state>")
            next_fen = field(state, "fen")
            if loaded and json.loads(envelope)["error"] is None and next_fen != fen:
                tree = ast.parse(json.loads(fn["arguments"])["code"])
                calls = [
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                ]
                sims = [
                    n
                    for n in calls
                    if n.func.id == "simulate_move"
                    and len(n.args) + len(n.keywords) >= 2
                ]
                plays = [n for n in calls if n.func.id == "play_move"]
                loops = (
                    ast.For,
                    ast.While,
                    ast.ListComp,
                    ast.DictComp,
                    ast.SetComp,
                    ast.GeneratorExp,
                )
                compares_candidates = len(sims) > 1 or any(
                    isinstance(node, loops) for node in ast.walk(tree)
                )
                if sims and compares_candidates and len(plays) == 1:
                    # Replay the new plies from SAN, without executing submitted Python.
                    chess = chess_module()
                    board = chess.Board(fen)
                    start = board.ply()
                    moves = re.findall(r"(\d+):(\S+)", field(state, "recent_history"))
                    for ply, san in moves:
                        if int(ply) > start:
                            require(
                                int(ply) == board.ply() + 1, "gap in skill game history"
                            )
                            board.push_san(san)
                    require(
                        board.fen() == next_fen and board.ply() - start in (1, 2),
                        "search snippet did not produce one live turn",
                    )
                    return f"step {i + 1}: loaded skill, candidate simulation, one play_move site, verified board transition"
            fen = next_fen
        elif fn["name"] == "play_move" and "<chess_state>" in output:
            fen = field(output, "fen")
    raise ValueError(
        "no observed skill-guided Python search with a successful live move"
    )


def verify_coding_trace(trace, patch):
    """An untruncated read of patch.txt, when recorded, must agree with the submitted diff."""
    require(trace.actions, "empty coding trajectory")
    for _, fn, observation in reversed(list(trace.steps())):
        if fn["name"] != "execute" or not observation:
            continue
        try:
            cmd = json.loads(fn["arguments"]).get("command")
        except (ValueError, AttributeError):
            continue
        if not isinstance(cmd, str) or cmd.strip() not in {
            "cat patch.txt",
            "cat /testbed/patch.txt",
        }:
            continue
        try:
            out = json.loads(observation)
            stdout = (
                out.get("stdout", out.get("output", ""))
                if out["returncode"] == 0
                else ""
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            match = re.search(
                r"<(?:stdout|output)>(.*?)</(?:stdout|output)>", observation, re.S
            )
            stdout = match[1] if match else ""
        if stdout.startswith(("diff --git", "--- ")) and not re.search(
            r"elided|truncated", stdout, re.I
        ):
            require(
                stdout.strip() == patch.strip(),
                "observed patch.txt differs from submitted diff",
            )
        break
