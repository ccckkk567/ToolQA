from pathlib import Path
import tempfile
import unittest

from agent.react_agent import ReactConfig, react_agent


def make_generator(responses, prompt_log=None):
    queue = list(responses)

    def _generate(prompt_text, stop=None, generation_config=None):
        if prompt_log is not None:
            prompt_log.append(
                {
                    "prompt_text": prompt_text,
                    "stop": stop,
                    "generation_config": generation_config,
                }
            )
        if not queue:
            raise AssertionError("Mock generator has no remaining responses.")
        return queue.pop(0)

    return _generate


class ReactAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_path = Path(self.temp_dir.name)
        self.system_prompt_path = temp_path / "system_prompt.txt"
        self.few_shot_path = temp_path / "few_shot_examples.txt"
        self.system_prompt_path.write_text(
            "你是一个测试用智能体。", encoding="utf-8"
        )
        self.few_shot_path.write_text(
            "[示例]\nQuestion: demo\nThought 1: 示例\nAction 1: finish[demo]",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def call_agent(self, *, llm_generate, tools, config=None, fallback_generate=None):
        return react_agent(
            "测试问题",
            tools=tools,
            llm_generate=llm_generate,
            system_prompt_path=self.system_prompt_path,
            few_shot_path=self.few_shot_path,
            config=config or ReactConfig(),
            fallback_generate=fallback_generate,
        )

    def test_react_agent_happy_path(self) -> None:
        llm_generate = make_generator(
            [
                "Thought 1: 我需要查询数据库。\n"
                "Action 1: sql_interpreter[SELECT answer FROM table]",
                "Thought 2: 我已经得到答案。\n"
                "Action 2: finish[21:43]",
            ]
        )

        result = self.call_agent(
            llm_generate=llm_generate,
            tools={"sql_interpreter": lambda _: [("21:43",)]},
            config=ReactConfig(max_steps=3),
        )

        self.assertEqual(result.final_answer, "21:43")
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.finish_reason, "finish")
        self.assertEqual(result.step_count, 2)
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(result.trace[0].status, "ok")
        self.assertEqual(result.trace[1].status, "finish")

    def test_parse_retry_does_not_increase_step_count(self) -> None:
        llm_generate = make_generator(
            [
                "Thought 1: 先搜索。\nAction 1 search[bad format]",
                "Thought 1: 先搜索。\nAction 1: search[good query]",
                "Thought 2: 已经得到答案。\nAction 2: finish[done]",
            ]
        )

        result = self.call_agent(
            llm_generate=llm_generate,
            tools={"search": lambda _: "found"},
            config=ReactConfig(max_steps=3, max_parse_retries=1),
        )

        self.assertEqual(result.final_answer, "done")
        self.assertEqual(result.parser_error_count, 1)
        self.assertEqual(len(result.parser_failures), 1)
        self.assertEqual(result.step_count, 2)
        self.assertEqual(result.tool_call_count, 1)

    def test_alias_mapping_resolves_tool_name(self) -> None:
        llm_generate = make_generator(
            [
                "Thought 1: 我需要查表。\nAction 1: lookup[SELECT count(*) FROM papers]",
                "Thought 2: 已得出答案。\nAction 2: finish[5]",
            ]
        )

        result = self.call_agent(
            llm_generate=llm_generate,
            tools={"sql_interpreter": lambda _: [(5,)]},
            config=ReactConfig(
                max_steps=3,
                tool_aliases={"lookup": "sql_interpreter"},
            ),
        )

        self.assertEqual(result.final_answer, "5")
        self.assertEqual(result.trace[0].action, "lookup")
        self.assertEqual(result.trace[0].resolved_action, "sql_interpreter")
        self.assertEqual(result.tool_call_count, 1)

    def test_loop_block_then_fallback(self) -> None:
        llm_generate = make_generator(
            [
                "Thought 1: 先搜索。\nAction 1: search[repeat query]",
                "Thought 2: 再搜一次。\nAction 2: search[repeat query]",
                "Thought 3: 再试一次。\nAction 3: search[repeat query]",
            ]
        )
        fallback_prompt_log = []
        fallback_generate = make_generator(
            ["最终答案A", "最终答案A", "最终答案B"],
            prompt_log=fallback_prompt_log,
        )

        result = self.call_agent(
            llm_generate=llm_generate,
            fallback_generate=fallback_generate,
            tools={"search": lambda _: "empty"},
            config=ReactConfig(
                max_steps=5,
                max_consecutive_repeats=1,
                cot_sc_samples=3,
            ),
        )

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.finish_reason, "loop_fallback")
        self.assertEqual(result.final_answer, "最终答案A")
        self.assertEqual(result.step_count, 2)
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(result.trace[1].status, "loop_blocked")
        self.assertEqual(len(result.fallback_trace), 3)
        self.assertFalse(result.fallback_low_confidence)
        self.assertIn("只输出最终答案", fallback_prompt_log[0]["prompt_text"])

    def test_unknown_tool_does_not_count_as_tool_call(self) -> None:
        llm_generate = make_generator(
            [
                "Thought 1: 我先调用不存在的工具。\nAction 1: unknown_tool[input]",
                "Thought 2: 我结束。\nAction 2: finish[done]",
            ]
        )

        result = self.call_agent(
            llm_generate=llm_generate,
            tools={"search": lambda _: "unused"},
            config=ReactConfig(max_steps=3),
        )

        self.assertEqual(result.final_answer, "done")
        self.assertEqual(result.tool_call_count, 0)
        self.assertEqual(result.trace[0].status, "tool_error")
        self.assertIn("unknown tool", result.trace[0].observation)

    def test_long_observation_is_truncated(self) -> None:
        long_text = "A" * 200
        llm_generate = make_generator(
            [
                "Thought 1: 先搜索长文本。\nAction 1: search[long doc]",
                "Thought 2: 我结束。\nAction 2: finish[done]",
            ]
        )

        result = self.call_agent(
            llm_generate=llm_generate,
            tools={"search": lambda _: long_text},
            config=ReactConfig(max_steps=3, max_observation_chars=60),
        )

        self.assertEqual(result.final_answer, "done")
        self.assertIn("[TRUNCATED]", result.trace[0].observation)
        self.assertLessEqual(len(result.trace[0].observation), 60)
        self.assertEqual(result.tool_call_count, 1)

    def test_fallback_tie_uses_first_seen_and_marks_low_confidence(self) -> None:
        llm_generate = make_generator(
            [
                "Thought 1: 先搜一下。\nAction 1: search[q]",
            ]
        )
        fallback_generate = make_generator(
            ["答案A", "答案B", "答案A", "答案B"]
        )

        result = self.call_agent(
            llm_generate=llm_generate,
            fallback_generate=fallback_generate,
            tools={"search": lambda _: "result"},
            config=ReactConfig(
                max_steps=1,
                cot_sc_samples=4,
                fallback_tie_break="first_seen",
            ),
        )

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.finish_reason, "max_steps_fallback")
        self.assertEqual(result.final_answer, "答案A")
        self.assertTrue(result.fallback_low_confidence)
        self.assertEqual(len(result.fallback_trace), 4)
        self.assertEqual(result.step_count, 1)
        self.assertEqual(result.tool_call_count, 1)

    def test_fallback_strips_finish_wrapper_and_normalizes_answers(self) -> None:
        llm_generate = make_generator(
            [
                "Thought 1: 先搜索。\nAction 1: search[q]",
            ]
        )
        fallback_generate = make_generator(
            [" finish[Answer ] ", "answer", "ANSWER"]
        )

        result = self.call_agent(
            llm_generate=llm_generate,
            fallback_generate=fallback_generate,
            tools={"search": lambda _: "result"},
            config=ReactConfig(
                max_steps=1,
                cot_sc_samples=3,
                fallback_case_insensitive=True,
            ),
        )

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.final_answer, "Answer")
        self.assertFalse(result.fallback_low_confidence)
        self.assertEqual(
            [sample.normalized_answer for sample in result.fallback_trace],
            ["answer", "answer", "answer"],
        )


if __name__ == "__main__":
    unittest.main()
