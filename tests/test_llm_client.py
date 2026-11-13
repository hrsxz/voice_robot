import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
from openai import BadRequestError

from pc.llm.llm_client import LLMClient, LLMUnavailableError
from skills import load_skill_registry


def bad_request(message: str) -> BadRequestError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return BadRequestError(message, response=response, body=None)


def ok_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class PromptTest(unittest.TestCase):
    def test_prompt_lists_every_registry_action(self) -> None:
        client = LLMClient()
        for action in load_skill_registry().list_actions():
            with self.subTest(action=action):
                self.assertIn(f"- {action}:", client.intent_instructions)

    def test_prompt_is_compact(self) -> None:
        # 精简前约 7500 字符
        self.assertLess(len(LLMClient().intent_instructions), 3000)

    def test_messages_keep_instructions_as_fixed_prefix(self) -> None:
        client = LLMClient()
        messages = client.build_intent_messages("前进30厘米")
        self.assertEqual(messages[0], {"role": "system", "content": client.intent_instructions})
        self.assertEqual(messages[1], {"role": "user", "content": "前进30厘米"})


class RequestOptionsTest(unittest.IsolatedAsyncioTestCase):
    def make_client(self, **kwargs) -> tuple[LLMClient, AsyncMock]:
        client = LLMClient(**kwargs)
        create = AsyncMock()
        fake = MagicMock()
        fake.chat.completions.create = create
        client._openai_client = fake
        return client, create

    async def test_options_are_sent(self) -> None:
        client, create = self.make_client(max_completion_tokens=500, reasoning_effort="low")
        create.return_value = ok_response('{"steps":[]}')

        await client._call_openai([], "m")

        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs["max_completion_tokens"], 500)
        self.assertEqual(kwargs["reasoning_effort"], "low")

    async def test_reasoning_effort_not_sent_by_default(self) -> None:
        client, create = self.make_client()
        create.return_value = ok_response('{"steps":[]}')

        await client._call_openai([], "m")

        self.assertNotIn("reasoning_effort", create.await_args.kwargs)

    async def test_unsupported_param_is_dropped_and_remembered(self) -> None:
        client, create = self.make_client(reasoning_effort="minimal")
        create.side_effect = [
            bad_request("Unsupported parameter: 'reasoning_effort' is not supported"),
            ok_response('{"steps":[]}'),
            ok_response('{"steps":[]}'),
        ]

        out = await client._call_openai([], "m")
        await client._call_openai([], "m")

        self.assertEqual(out, '{"steps":[]}')
        self.assertEqual(create.await_count, 3)
        self.assertNotIn("reasoning_effort", create.await_args_list[1].kwargs)
        self.assertNotIn("reasoning_effort", create.await_args_list[2].kwargs)

    async def test_other_bad_request_is_not_retried(self) -> None:
        client, create = self.make_client()
        create.side_effect = bad_request("Invalid model")

        with self.assertRaises(RuntimeError):
            await client._call_openai([], "m")
        self.assertEqual(create.await_count, 1)

    async def test_no_llm_raises_unavailable(self) -> None:
        client = LLMClient(enable_ollama=False)
        client.openai_api_key = None
        with self.assertRaises(LLMUnavailableError):
            await client.generate("跳个舞")


if __name__ == "__main__":
    unittest.main()
