"""The Part 1 coding agent: fix a software issue and submit a git patch."""

from __future__ import annotations

import json
from typing import Any

from assignment.agent.base import (
    DEFAULT_COMPACTION_KEEP_RECENT_STEPS,
    DEFAULT_COMPACTION_MAX_TOKENS,
    Agent,
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

        schemas = {
            tool["function"]["name"]: tool["function"]["parameters"]
            for tool in (EXECUTE_TOOL, SEND_MESSAGE_TOOL)
        }
        observations = []
        for call in tool_calls:
            try:
                name = call["function"]["name"]
                arguments = json.loads(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be a JSON object.")

                if name not in schemas:
                    raise ValueError(f"Unknown tool: {name}")
                schema = schemas[name]
                unknown = arguments.keys() - schema.get("properties", {}).keys()
                missing = set(schema.get("required", [])) - arguments.keys()
                if schema.get("additionalProperties") is False and unknown:
                    raise ValueError(f"Unknown arguments: {sorted(unknown)}")
                if missing:
                    raise ValueError(f"Missing arguments: {sorted(missing)}")

                if name == "execute":
                    # Environment expands env before Command validates parameter types.
                    env = arguments.get("env")
                    if env is not None and not isinstance(env, dict):
                        raise ValueError("env must be an object or null.")
                elif name == "send_message":
                    if not isinstance(arguments["summary"], str):
                        raise ValueError("send_message requires a string summary.")
            except (KeyError, TypeError, ValueError) as exc:
                content = f"Tool error: {exc}"
            else:
                # Infrastructure failures propagate; only invalid calls are recoverable here.
                if name == "execute":
                    content = json.dumps(self.env.execute(**arguments), ensure_ascii=False)
                else:
                    self.finished = True
                    content = f"Submission received: {arguments['summary']}"

            observations.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": content,
            })
        return observations
