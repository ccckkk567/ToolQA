from __future__ import annotations

from dataclasses import dataclass
import re


THOUGHT_PREFIX_RE = re.compile(r"^\s*Thought(?:\s+\d+)?:\s*", re.IGNORECASE)
ACTION_PREFIX_RE = re.compile(r"^\s*Action(?:\s+\d+)?:\s*", re.IGNORECASE)
THOUGHT_LINE_RE = re.compile(r"^\s*Thought(?:\s+\d+)?:", re.IGNORECASE)
ACTION_LINE_RE = re.compile(r"^\s*Action(?:\s+\d+)?:", re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*(.*?)\s*```$", re.DOTALL)
ACTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(slots=True)
class ParseErrorInfo:
    error_type: str
    error_message: str


class ParserError(ValueError):
    def __init__(self, error_type: str, error_message: str) -> None:
        super().__init__(error_message)
        self.error_type = error_type
        self.error_message = error_message


@dataclass(slots=True)
class ParsedStep:
    thought: str = ""
    action: str = ""
    action_input: str = ""
    raw_text: str = ""
    is_valid: bool = False
    error_type: str | None = None
    error_message: str | None = None

    @classmethod
    def invalid(
        cls,
        raw_text: str,
        error_type: str,
        error_message: str,
    ) -> "ParsedStep":
        return cls(
            raw_text=raw_text,
            is_valid=False,
            error_type=error_type,
            error_message=error_message,
        )


def normalize_multiline_text(text: str) -> str:
    """Normalize line endings and strip outer whitespace/code fences."""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    fenced = CODE_FENCE_RE.match(normalized)
    if fenced:
        normalized = fenced.group(1).strip()

    return normalized


def parse_action_line(action_line: str) -> tuple[str, str]:
    """
    Parse an action line in the form:
    Action k: tool_name[input]
    """
    normalized = normalize_multiline_text(action_line)
    prefix_match = ACTION_PREFIX_RE.match(normalized)
    if not prefix_match:
        raise ParserError(
            "missing_action",
            "Could not find a valid 'Action:' line.",
        )

    payload = normalized[prefix_match.end() :].strip()
    first_left_bracket = payload.find("[")
    last_right_bracket = payload.rfind("]")

    if (
        first_left_bracket == -1
        or last_right_bracket == -1
        or last_right_bracket < first_left_bracket
        or payload[last_right_bracket + 1 :].strip()
    ):
        raise ParserError(
            "bad_action_format",
            "Action must use the format tool_name[input].",
        )

    action_name = payload[:first_left_bracket].strip()
    action_input = payload[first_left_bracket + 1 : last_right_bracket].strip()

    if not action_name:
        raise ParserError("empty_action_name", "Action name is empty.")

    if not ACTION_NAME_RE.fullmatch(action_name):
        raise ParserError(
            "bad_action_format",
            f"Invalid action name: {action_name!r}.",
        )

    if not action_input:
        raise ParserError("empty_action_input", "Action input is empty.")

    return action_name.lower(), action_input


def parse_llm_output(text: str) -> ParsedStep:
    """
    Strictly parse a single ReAct step.

    Expected format:
        Thought k: ...
        Action k: tool_name[input]
    """
    raw_text = normalize_multiline_text(text)
    if not raw_text:
        return ParsedStep.invalid(
            raw_text=raw_text,
            error_type="empty_output",
            error_message="Model output is empty.",
        )

    lines = raw_text.split("\n")
    thought_indices = [i for i, line in enumerate(lines) if THOUGHT_LINE_RE.match(line)]
    action_indices = [i for i, line in enumerate(lines) if ACTION_LINE_RE.match(line)]

    if not thought_indices:
        return ParsedStep.invalid(
            raw_text=raw_text,
            error_type="missing_thought",
            error_message="Could not find a valid 'Thought:' line.",
        )

    if not action_indices:
        return ParsedStep.invalid(
            raw_text=raw_text,
            error_type="missing_action",
            error_message="Could not find a valid 'Action:' line.",
        )

    if len(thought_indices) > 1 or len(action_indices) > 1:
        return ParsedStep.invalid(
            raw_text=raw_text,
            error_type="multiple_steps_generated",
            error_message="Model output contains multiple Thought/Action groups.",
        )

    thought_index = thought_indices[0]
    action_index = action_indices[0]

    if any(line.strip() for line in lines[:thought_index]):
        return ParsedStep.invalid(
            raw_text=raw_text,
            error_type="extraneous_text",
            error_message="Unexpected text appears before the Thought line.",
        )

    if action_index <= thought_index:
        return ParsedStep.invalid(
            raw_text=raw_text,
            error_type="action_before_thought",
            error_message="Action appears before Thought or without Thought content.",
        )

    thought_lines = lines[thought_index:action_index]
    thought_first_line = THOUGHT_PREFIX_RE.sub("", thought_lines[0], count=1)
    remaining_thought_lines = thought_lines[1:]
    thought = normalize_multiline_text(
        "\n".join([thought_first_line, *remaining_thought_lines]).strip()
    )

    if not thought:
        return ParsedStep.invalid(
            raw_text=raw_text,
            error_type="missing_thought",
            error_message="Thought content is empty.",
        )

    action_line = lines[action_index]
    try:
        action, action_input = parse_action_line(action_line)
    except ParserError as exc:
        return ParsedStep.invalid(
            raw_text=raw_text,
            error_type=exc.error_type,
            error_message=exc.error_message,
        )

    if any(line.strip() for line in lines[action_index + 1 :]):
        return ParsedStep.invalid(
            raw_text=raw_text,
            error_type="extraneous_text",
            error_message="Unexpected text appears after the Action line.",
        )

    return ParsedStep(
        thought=thought,
        action=action,
        action_input=action_input,
        raw_text=raw_text,
        is_valid=True,
    )
