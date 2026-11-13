"""The Part 1 coding agent: fix a software issue and submit a git patch."""

from __future__ import annotations

import json
from typing import Any

from assignment.agent.base import (
    DEFAULT_COMPACTION_KEEP_RECENT_STEPS,
    DEFAULT_COMPACTION_MAX_TOKENS,
    Agent,
    format_tool_output,
)
from assignment.agent.tools import EXECUTE_TOOL, SEND_MESSAGE_TOOL
from assignment.env import Environment

class CodeAgent(Agent):
    """An agent that fixes a software issue and submits a git patch."""

    def __init__(
        self,
        task: str,
        environment: Environment,
        model: str | None = None,
        logs_save_path: str | None = None,
        step_limit: int = 100,
        skills_path: str | None = None,
        auto_stop_environment: bool = True,
        compact_threshold_tokens: int | None = None,
        compaction_keep_recent_steps: int = DEFAULT_COMPACTION_KEEP_RECENT_STEPS,
        compaction_max_tokens: int = DEFAULT_COMPACTION_MAX_TOKENS,
    ):
        super().__init__(
            environment=environment,
            model=model,
            logs_save_path=logs_save_path,
            step_limit=step_limit,
            skills_path=skills_path,
            auto_stop_environment=auto_stop_environment,
            compact_threshold_tokens=compact_threshold_tokens,
            compaction_keep_recent_steps=compaction_keep_recent_steps,
            compaction_max_tokens=compaction_max_tokens,
        )
        self.task = task
        self.submitted_patch = ""

        # TODO(Part 1.3): Make the `execute` and `send_message` tools available
        # to the agent.
        self.tools.extend([EXECUTE_TOOL, SEND_MESSAGE_TOOL])

        system_info = {
            "machine": self.env.machine,
            "release": self.env.release,
            "system": self.env.system,
            "version": self.env.version,
        }
        self.system_prompt = (
            "You are a smart, helpful agent.\n\n"
            "<system_information>\n"
            + json.dumps(system_info, indent=2)
            + "\n</system_information>"
        )

        self.task_prompt = task
        # TODO(1.4): If any skills are available to the agent, make their
        # descriptions/metadata available to the agent in the prompt.

    def execute_tool_calls(
        self, tool_calls: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """Execute ``execute`` and ``send_message`` calls in the code sandbox."""

        observations = []
        for call in tool_calls:
            try:
                name = call["function"]["name"]
                arguments = json.loads(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be a JSON object.")

                if name == "execute":
                    allowed = {"command", "cwd", "timeout", "env", "shell"}
                    if arguments.keys() - allowed:
                        raise ValueError("Unsupported execute arguments.")
                    command = arguments["command"]
                    if not isinstance(command, str) and not (
                        isinstance(command, list)
                        and all(isinstance(part, str) for part in command)
                    ):
                        raise ValueError("command must be a string or a list of strings.")
                    for key, types in (
                        ("cwd", (str,)),
                        ("timeout", (int, float)),
                        ("env", (dict,)),
                        ("shell", (bool,)),
                    ):
                        value = arguments.get(key)
                        if value is not None and (
                            not isinstance(value, types)
                            or (key == "timeout" and isinstance(value, bool))
                        ):
                            raise ValueError(f"Invalid type for {key}.")
                    env = arguments.get("env")
                    if env is not None and not all(
                        isinstance(value, str) for value in env.values()
                    ):
                        raise ValueError("env values must be strings.")
                elif name == "send_message":
                    if set(arguments) != {"summary"} or not isinstance(
                        arguments["summary"], str
                    ):
                        raise ValueError("send_message requires a string summary.")
                else:
                    raise ValueError(f"Unknown tool: {name}")
            except (KeyError, TypeError, ValueError) as exc:
                content = f"Tool error: {exc}"
            else:
                # Infrastructure failures propagate; only invalid calls are recoverable here.
                if name == "execute":
                    content = format_tool_output(self.env.execute(**arguments))
                else:
                    self.finished = True
                    content = f"Submission received: {arguments['summary']}"

            observations.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": content,
            })
        return observations
