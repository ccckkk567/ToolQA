import unittest

from agent.parser import parse_action_line, parse_llm_output


class ParserTests(unittest.TestCase):
    def test_parse_action_line_normalizes_action_name(self) -> None:
        action, action_input = parse_action_line("Action 2: SEARCH[Transformer paper]")
        self.assertEqual(action, "search")
        self.assertEqual(action_input, "Transformer paper")

    def test_parse_action_line_supports_brackets_inside_input(self) -> None:
        action, action_input = parse_action_line(
            "Action: search[paper section [2] and author]"
        )
        self.assertEqual(action, "search")
        self.assertEqual(action_input, "paper section [2] and author")

    def test_parse_llm_output_supports_multiline_thought(self) -> None:
        parsed = parse_llm_output(
            "Thought 1: 我需要先搜索论文标题。\n"
            "然后结合观察再决定下一步。\n"
            "Action 1: search[Attention Is All You Need]"
        )
        self.assertTrue(parsed.is_valid)
        self.assertEqual(
            parsed.thought,
            "我需要先搜索论文标题。\n然后结合观察再决定下一步。",
        )
        self.assertEqual(parsed.action, "search")
        self.assertEqual(parsed.action_input, "Attention Is All You Need")

    def test_parse_llm_output_rejects_multiple_steps(self) -> None:
        parsed = parse_llm_output(
            "Thought 1: 先搜索。\n"
            "Action 1: search[x]\n"
            "Observation 1: result\n"
            "Thought 2: 再回答。\n"
            "Action 2: finish[y]"
        )
        self.assertFalse(parsed.is_valid)
        self.assertEqual(parsed.error_type, "multiple_steps_generated")

    def test_parse_llm_output_rejects_extraneous_text_after_action(self) -> None:
        parsed = parse_llm_output(
            "Thought 1: 先搜索。\n"
            "Action 1: search[x]\n"
            "多余文本"
        )
        self.assertFalse(parsed.is_valid)
        self.assertEqual(parsed.error_type, "extraneous_text")

    def test_parse_llm_output_accepts_code_fenced_response(self) -> None:
        parsed = parse_llm_output(
            "```text\n"
            "Thought 1: 需要搜索作者。\n"
            "Action 1: search[Attention Is All You Need author]\n"
            "```"
        )
        self.assertTrue(parsed.is_valid)
        self.assertEqual(parsed.action, "search")


if __name__ == "__main__":
    unittest.main()

