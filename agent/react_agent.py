from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import inspect
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from agent.parser import ParsedStep, parse_llm_output


ToolFn = Callable[[str], Any]
GenerateFn = Callable[..., str]

DEFAULT_STOP_TOKENS = ["\nObservation"]
LOOP_WARNING_TEXT = "LoopWarning: repeated action detected. Please change strategy."


@dataclass(slots=True)
class ReactConfig:
    max_steps: int = 7
    max_parse_retries: int = 2
    max_consecutive_repeats: int = 1
    fallback_enabled: bool = True
    cot_sc_samples: int = 5
    stop_tokens: list[str] | None = None
    generation_kwargs: dict[str, Any] | None = None
    fallback_generation_kwargs: dict[str, Any] | None = None
    max_observation_chars: int = 1200
    fallback_tie_break: str = "first_seen"
    fallback_case_insensitive: bool = True
    tool_aliases: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class StepRecord:
    step: int
    thought: str
    action: str
    action_input: str
    observation: str | None
    raw_llm_output: str
    status: str
    resolved_action: str | None = None


@dataclass(slots=True)
class ParserFailure:
    step: int
    attempt: int
    raw_llm_output: str
    error_type: str | None
    error_message: str | None


@dataclass(slots=True)
class FallbackSample:
    sample_id: int
    raw_output: str
    final_answer: str
    normalized_answer: str


@dataclass(slots=True)
class AgentResult:
    question: str
    final_answer: str
    trace: list[StepRecord]
    step_count: int
    tool_call_count: int
    used_fallback: bool
    finish_reason: str
    available_tools: list[str]
    parser_error_count: int
    raw_final_output: str | None = None
    fallback_trace: list[FallbackSample] = field(default_factory=list)
    parser_failures: list[ParserFailure] = field(default_factory=list)
    fallback_low_confidence: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_text_file(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def build_react_prompt(
    *,
    system_prompt: str,
    few_shot_examples: str,
    question: str,
    scratchpad: str,
    step: int,
) -> str:
    scratchpad_block = scratchpad.strip()
    scratchpad_text = f"{scratchpad_block}\n\n" if scratchpad_block else ""
    return (
        f"{system_prompt}\n\n"
        f"{few_shot_examples}\n\n"
        f"---\n"
        f"[真实问题]\n"
        f"Question: {question}\n"
        f"{scratchpad_text}"
        f"请继续只输出下一组 Thought 和 Action，严格遵守如下格式：\n"
        f"Thought {step}: ...\n"
        f"Action {step}: tool_name[input]"
    )


def build_parse_retry_prompt(
    *,
    previous_prompt: str,
    raw_output: str,
    error_message: str,
    step: int,
) -> str:
    return (
        f"{previous_prompt}\n\n"
        f"你上一轮的输出格式不合法，原因是：{error_message}\n\n"
        f"上一轮输出如下：\n"
        f"{raw_output}\n\n"
        f"现在请重新输出，注意：\n"
        f"1. 只能输出一组 Thought 和 Action\n"
        f"2. 不要输出 Observation\n"
        f"3. Action 必须严格符合 tool_name[input]\n"
        f"4. 严格按照下面格式：\n"
        f"Thought {step}: ...\n"
        f"Action {step}: tool_name[input]"
    )


def build_fallback_prompt(question: str, failure_summary: str | None = None) -> str:
    summary_block = (
        f"以下是前序 ReAct 失败摘要，仅供参考：\n{failure_summary}\n\n"
        if failure_summary
        else ""
    )
    return (
        "请直接回答下面的问题，只输出最终答案，不要输出 Thought、Action、"
        "Observation、工具调用、finish[...] 或额外解释。\n\n"
        f"{summary_block}"
        f"Question: {question}"
    )


def invoke_generate_fn(
    generate_fn: GenerateFn,
    *,
    prompt_text: str,
    stop: list[str] | None = None,
    generation_config: dict[str, Any] | None = None,
) -> str:
    generation_config = generation_config or {}

    try:
        signature = inspect.signature(generate_fn)
    except (TypeError, ValueError):
        signature = None

    kwargs: dict[str, Any] = {}
    has_var_kwargs = False
    if signature is not None:
        has_var_kwargs = any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )
        if "stop" in signature.parameters or has_var_kwargs:
            kwargs["stop"] = stop
        if "generation_config" in signature.parameters or has_var_kwargs:
            kwargs["generation_config"] = generation_config
        elif has_var_kwargs:
            kwargs.update(generation_config)
        elif generation_config:
            supported = set(signature.parameters)
            for key, value in generation_config.items():
                if key in supported:
                    kwargs[key] = value
    else:
        kwargs["stop"] = stop
        kwargs["generation_config"] = generation_config

    try:
        result = generate_fn(prompt_text, **kwargs)
    except TypeError as exc:
        if signature is None and ("unexpected keyword" in str(exc) or "positional" in str(exc)):
            result = generate_fn(prompt_text)
        else:
            raise

    if not isinstance(result, str):
        result = str(result)
    return result


def resolve_action_name(
    action_name: str,
    tools: Mapping[str, ToolFn],
    tool_aliases: Mapping[str, str] | None = None,
) -> str | None:
    aliases = tool_aliases or {}

    if action_name in tools:
        return action_name

    direct_target = aliases.get(action_name)
    if direct_target and direct_target in tools:
        return direct_target

    for alias, target in aliases.items():
        if target == action_name and alias in tools:
            return alias

    return None


def stringify_observation(raw: Any) -> str:
    if isinstance(raw, Exception):
        return f"ToolError: {raw}"
    if raw is None:
        return "No result returned."
    if isinstance(raw, str):
        text = raw.strip()
        return text or "Could not find results."
    if isinstance(raw, tuple):
        text = repr(raw)
        return text or "Could not find results."
    if isinstance(raw, list):
        if not raw:
            return "Could not find results."
        if any(isinstance(item, tuple) for item in raw):
            return repr(raw)
        return json.dumps(raw, ensure_ascii=False, default=str)
    if isinstance(raw, dict):
        if not raw:
            return "Could not find results."
        return json.dumps(raw, ensure_ascii=False, default=str)
    return str(raw)


def truncate_observation(observation: str, max_chars: int) -> str:
    if max_chars <= 0 or len(observation) <= max_chars:
        return observation
    half = max(1, (max_chars - len("...[TRUNCATED]...")) // 2)
    return f"{observation[:half]}...[TRUNCATED]...{observation[-half:]}"


def format_step_for_scratchpad(step_record: StepRecord) -> str:
    lines = [
        f"Thought {step_record.step}: {step_record.thought}",
        f"Action {step_record.step}: {step_record.action}[{step_record.action_input}]",
    ]
    if step_record.observation is not None:
        lines.append(f"Observation {step_record.step}: {step_record.observation}")
    return "\n".join(lines)


def normalize_answer_for_voting(
    answer: str,
    *,
    case_insensitive: bool = True,
) -> str:
    normalized = " ".join((answer or "").strip().split())
    return normalized.casefold() if case_insensitive else normalized


def strip_finish_wrapper(text: str) -> str:
    candidate = (text or "").strip()
    match = re.search(r"finish\[(.*)\]\s*$", candidate, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return candidate


def summarize_failure(
    *,
    reason: str,
    trace: list[StepRecord],
    parser_failures: list[ParserFailure],
) -> str:
    parts = [f"finish_reason={reason}"]
    if trace:
        last = trace[-1]
        parts.extend(
            [
                f"last_step={last.step}",
                f"last_action={last.action}[{last.action_input}]",
                f"last_status={last.status}",
            ]
        )
        if last.observation:
            parts.append(f"last_observation={last.observation}")
    if parser_failures:
        last_failure = parser_failures[-1]
        parts.append(
            f"last_parser_error={last_failure.error_type}:{last_failure.error_message}"
        )
    return " | ".join(parts)


def count_consecutive_action_repeats(
    trace: list[StepRecord],
    action: str,
    action_input: str,
) -> int:
    count = 0
    for record in reversed(trace):
        if record.action == action and record.action_input == action_input:
            count += 1
        else:
            break
    return count


def execute_tool_call(
    *,
    action: str,
    action_input: str,
    tools: Mapping[str, ToolFn],
    tool_aliases: Mapping[str, str],
    max_observation_chars: int,
) -> tuple[str, str, str | None, bool]:
    resolved_action = resolve_action_name(action, tools, tool_aliases)
    if resolved_action is None:
        available = ", ".join(sorted(tools))
        return (
            f"ToolError: unknown tool '{action}'. Available tools: [{available}]",
            "tool_error",
            None,
            False,
        )

    try:
        raw_result = tools[resolved_action](action_input)
    except Exception as exc:  # noqa: BLE001
        return (
            truncate_observation(f"ToolError: {exc}", max_observation_chars),
            "tool_error",
            resolved_action,
            True,
        )

    observation = truncate_observation(
        stringify_observation(raw_result),
        max_observation_chars,
    )
    return observation, "ok", resolved_action, True


def run_cot_sc_fallback(
    *,
    question: str,
    generate_fn: GenerateFn,
    config: ReactConfig,
    failure_summary: str | None = None,
) -> tuple[str, list[FallbackSample], bool, str | None]:
    prompt_text = build_fallback_prompt(question, failure_summary)
    samples: list[FallbackSample] = []

    for sample_id in range(1, config.cot_sc_samples + 1):
        raw_output = invoke_generate_fn(
            generate_fn,
            prompt_text=prompt_text,
            stop=None,
            generation_config=config.fallback_generation_kwargs
            or config.generation_kwargs,
        )
        final_answer = strip_finish_wrapper(raw_output)
        normalized = normalize_answer_for_voting(
            final_answer,
            case_insensitive=config.fallback_case_insensitive,
        )
        samples.append(
            FallbackSample(
                sample_id=sample_id,
                raw_output=raw_output,
                final_answer=final_answer,
                normalized_answer=normalized,
            )
        )

    if not samples:
        return "", [], True, None

    counter = Counter(sample.normalized_answer for sample in samples)
    highest = max(counter.values(), default=0)
    winners = {answer for answer, count in counter.items() if count == highest}
    low_confidence = len(winners) != 1

    chosen_sample: FallbackSample | None = None
    if config.fallback_tie_break == "first_seen":
        for sample in samples:
            if sample.normalized_answer in winners:
                chosen_sample = sample
                break
    else:
        for sample in samples:
            if sample.normalized_answer in winners:
                chosen_sample = sample
                break

    if chosen_sample is None:
        chosen_sample = samples[0]

    return chosen_sample.final_answer, samples, low_confidence, chosen_sample.raw_output


def react_agent(
    question: str,
    tools: Mapping[str, ToolFn],
    llm_generate: GenerateFn,
    *,
    system_prompt_path: str | Path = "prompt/system_prompt.txt",
    few_shot_path: str | Path = "prompt/few_shot_examples.txt",
    config: ReactConfig | None = None,
    fallback_generate: GenerateFn | None = None,
) -> AgentResult:
    config = config or ReactConfig()
    fallback_generate = fallback_generate or llm_generate

    system_prompt = read_text_file(system_prompt_path)
    few_shot_examples = read_text_file(few_shot_path)

    trace: list[StepRecord] = []
    parser_failures: list[ParserFailure] = []
    scratchpad = ""
    tool_call_count = 0
    parser_error_count = 0

    def finalize_without_fallback(reason: str, raw_final_output: str | None = None) -> AgentResult:
        return AgentResult(
            question=question,
            final_answer="",
            trace=trace,
            step_count=len(trace),
            tool_call_count=tool_call_count,
            used_fallback=False,
            finish_reason=reason,
            available_tools=sorted(tools),
            parser_error_count=parser_error_count,
            raw_final_output=raw_final_output,
            parser_failures=parser_failures,
        )

    def finalize_with_fallback(reason: str) -> AgentResult:
        if not config.fallback_enabled:
            return finalize_without_fallback(reason)

        failure_summary = summarize_failure(
            reason=reason,
            trace=trace,
            parser_failures=parser_failures,
        )
        final_answer, fallback_trace, low_confidence, raw_final_output = run_cot_sc_fallback(
            question=question,
            generate_fn=fallback_generate,
            config=config,
            failure_summary=failure_summary,
        )
        return AgentResult(
            question=question,
            final_answer=final_answer,
            trace=trace,
            step_count=len(trace),
            tool_call_count=tool_call_count,
            used_fallback=True,
            finish_reason=reason,
            available_tools=sorted(tools),
            parser_error_count=parser_error_count,
            raw_final_output=raw_final_output,
            fallback_trace=fallback_trace,
            parser_failures=parser_failures,
            fallback_low_confidence=low_confidence,
        )

    for step in range(1, config.max_steps + 1):
        prompt_text = build_react_prompt(
            system_prompt=system_prompt,
            few_shot_examples=few_shot_examples,
            question=question,
            scratchpad=scratchpad,
            step=step,
        )

        parsed: ParsedStep | None = None
        raw_output = ""
        current_prompt = prompt_text

        for attempt in range(1, config.max_parse_retries + 2):
            raw_output = invoke_generate_fn(
                llm_generate,
                prompt_text=current_prompt,
                stop=config.stop_tokens or DEFAULT_STOP_TOKENS,
                generation_config=config.generation_kwargs,
            )
            parsed = parse_llm_output(raw_output)
            if parsed.is_valid:
                break

            parser_error_count += 1
            parser_failures.append(
                ParserFailure(
                    step=step,
                    attempt=attempt,
                    raw_llm_output=raw_output,
                    error_type=parsed.error_type,
                    error_message=parsed.error_message,
                )
            )
            if attempt >= config.max_parse_retries + 1:
                break

            current_prompt = build_parse_retry_prompt(
                previous_prompt=prompt_text,
                raw_output=raw_output,
                error_message=parsed.error_message or "Unknown parse error.",
                step=step,
            )

        if parsed is None or not parsed.is_valid:
            return finalize_with_fallback("parse_failure_fallback")

        if parsed.action == "finish":
            finish_record = StepRecord(
                step=step,
                thought=parsed.thought,
                action=parsed.action,
                action_input=parsed.action_input,
                observation=None,
                raw_llm_output=raw_output,
                status="finish",
                resolved_action="finish",
            )
            trace.append(finish_record)
            return AgentResult(
                question=question,
                final_answer=parsed.action_input,
                trace=trace,
                step_count=len(trace),
                tool_call_count=tool_call_count,
                used_fallback=False,
                finish_reason="finish",
                available_tools=sorted(tools),
                parser_error_count=parser_error_count,
                raw_final_output=raw_output,
                parser_failures=parser_failures,
            )

        repeat_count = count_consecutive_action_repeats(
            trace,
            parsed.action,
            parsed.action_input,
        )
        if repeat_count > config.max_consecutive_repeats:
            return finalize_with_fallback("loop_fallback")

        if repeat_count > 0:
            observation = LOOP_WARNING_TEXT
            status = "loop_blocked"
            resolved_action = resolve_action_name(
                parsed.action,
                tools,
                config.tool_aliases,
            )
            did_call_tool = False
        else:
            observation, status, resolved_action, did_call_tool = execute_tool_call(
                action=parsed.action,
                action_input=parsed.action_input,
                tools=tools,
                tool_aliases=config.tool_aliases,
                max_observation_chars=config.max_observation_chars,
            )

        if did_call_tool:
            tool_call_count += 1

        step_record = StepRecord(
            step=step,
            thought=parsed.thought,
            action=parsed.action,
            action_input=parsed.action_input,
            observation=observation,
            raw_llm_output=raw_output,
            status=status,
            resolved_action=resolved_action,
        )
        trace.append(step_record)
        scratchpad = (
            f"{scratchpad}\n\n{format_step_for_scratchpad(step_record)}".strip()
            if scratchpad
            else format_step_for_scratchpad(step_record)
        )

    return finalize_with_fallback("max_steps_fallback")

