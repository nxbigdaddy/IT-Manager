"""Offline tests; all transport access is mocked."""
import io
import json
import os
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from itmanager.ai import AIError, DEFAULT_PROMPTS, DEFAULT_PROVIDERS, generate, render_prompt
from itmanager.ai import _NoRedirect


class AITests(unittest.TestCase):
    def setUp(self):
        self.config = {"provider": "deepseek", "model": "deepseek-chat", "api_key_env": "TEST_AI_KEY"}
        self.env = patch.dict(os.environ, {"TEST_AI_KEY": "secret-test-key"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transport = patch("itmanager.ai.urllib.request.build_opener")
        self.build = self.transport.start()
        self.addCleanup(self.transport.stop)
        self.response = MagicMock()
        self.response.status = 200
        self.response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "草稿，不执行"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            "model": "returned-model"}).encode()
        self.build.return_value.open.return_value.__enter__.return_value = self.response

    def call(self, **changes):
        return generate(dict(self.config, **changes), "系统", "用户")

    def test_generate_all_providers(self):
        for provider, defaults in DEFAULT_PROVIDERS.items():
            with self.subTest(provider=provider):
                result = self.call(provider=provider, model=defaults["model"])
                self.assertEqual(result, {"text": "草稿，不执行", "usage": {
                    "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}, "model": "returned-model"})
                args, kwargs = self.build.return_value.open.call_args
                request = args[0]
                self.assertEqual(request.full_url, defaults["base_url"] + "/chat/completions")
                self.assertEqual(request.get_header("Authorization"), "Bearer secret-test-key")
                self.assertEqual(request.method, "POST")
                payload = json.loads(request.data)
                self.assertFalse(payload["stream"])
                self.assertNotIn("tools", payload)
                self.assertIn("草稿", payload["messages"][0]["content"])
                self.assertEqual(payload["messages"][1]["content"], "用户")
                self.assertLessEqual(kwargs["timeout"], 120)
                self.assertIsInstance(self.build.call_args.args[0], _NoRedirect)

    def test_official_regional_hosts(self):
        for provider, hosts in {"kimi": ["api.moonshot.cn", "api.moonshot.ai"], "qwen": [
            "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"]}.items():
            for host in hosts:
                with self.subTest(host=host):
                    self.call(provider=provider, base_url="https://" + host + "/v1/chat/completions")
                    self.assertEqual(self.build.return_value.open.call_args.args[0].full_url,
                                     "https://" + host + "/v1/chat/completions")

    def test_bad_urls_never_open(self):
        for url in ["http://api.deepseek.com/v1", "https://evil.com", "https://api.deepseek.com.evil.com",
                    "https://api.deepseek.com@evil.com", "https://user:pass@api.deepseek.com",
                    "https://api.deepseek.com:444/v1", "https://api.deepseek.com/v1?key=x",
                    "https://api.deepseek.com/#x", "https://api.moonshot.cn/v1",
                    "https://api.deepseek.com\\@evil.com", "https://api.deepseek.com\n/v1",
                    "https://api.deepseek.com:bad", "https://127.0.0.1", "file:///tmp/key", None]:
            with self.subTest(url=url), self.assertRaises(AIError):
                self.call(base_url=url)
        self.build.assert_not_called()

    def test_invalid_config_never_open(self):
        for changes in [{"provider": "other"}, {"provider": []}, {"model": ""}, {"model": None},
                        {"api_key_env": ""}, {"api_key_env": "MISSING_TEST_AI_KEY_83637"},
                        {"timeout": 121}, {"timeout": 0}, {"timeout": True}, {"timeout": float("nan")},
                        {"temperature": -1}, {"temperature": 3}, {"temperature": float("inf")},
                        {"max_tokens": 0}, {"max_tokens": True}, {"max_tokens": 1.5}]:
            with self.subTest(changes=changes), self.assertRaises(AIError):
                self.call(**changes)
        with self.assertRaises(AIError):
            generate({}, "", "")
        with self.assertRaises(AIError):
            generate(None, "", "")
        with self.assertRaises(AIError):
            generate(self.config, None, "")
        self.build.assert_not_called()

    def test_parameters_forwarded(self):
        self.call(timeout=120, temperature=0, max_tokens=123)
        self.assertEqual(self.build.return_value.open.call_args.kwargs["timeout"], 120)
        payload = json.loads(self.build.return_value.open.call_args.args[0].data)
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["max_tokens"], 123)

    def test_header_injection_rejected(self):
        with patch.dict(os.environ, {"TEST_AI_KEY": "secret\r\nInjected: header"}):
            with self.assertRaises(AIError):
                self.call()
        self.build.assert_not_called()

    def test_redirect_handler_refuses_every_redirect(self):
        handler = _NoRedirect()
        request = urllib.request.Request("https://api.deepseek.com/v1/chat/completions",
                                         headers={"Authorization": "Bearer secret-test-key"})
        for status in [301, 302, 303, 307, 308]:
            for target in ["https://evil.com", "https://api.deepseek.com/other"]:
                self.assertIsNone(handler.redirect_request(request, None, status, "", {}, target))

    def test_safe_errors_hide_secret_and_response(self):
        sensitive = "secret-test-key PRIVATE_RAW_RESPONSE"
        for error in [urllib.error.HTTPError("https://api.deepseek.com", 401, sensitive, {}, io.BytesIO(sensitive.encode())),
                      urllib.error.URLError(sensitive), TimeoutError(sensitive), ValueError(sensitive)]:
            with self.subTest(error=type(error)):
                self.build.return_value.open.side_effect = error
                with self.assertRaises(AIError) as caught:
                    self.call()
                self.assertNotIn("secret-test-key", str(caught.exception))
                self.assertNotIn("PRIVATE_RAW_RESPONSE", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_bad_responses_are_safe(self):
        for raw in [b"PRIVATE_RAW_RESPONSE", b"{}", b"null", b"[]", b' {"choices": []}',
                    json.dumps({"choices": [{"message": {"content": None}}]}).encode(),
                    b"x" * (2 * 1024 * 1024 + 1)]:
            with self.subTest(size=len(raw)), self.assertRaises(AIError) as caught:
                self.response.read.return_value = raw
                self.call()
            self.assertNotIn("PRIVATE_RAW_RESPONSE", str(caught.exception))

    def test_optional_metadata(self):
        self.response.read.return_value = b'{"choices":[{"message":{"content":"draft"}}]}'
        self.assertEqual(self.call(), {"text": "draft", "usage": {}, "model": "deepseek-chat"})

    def test_render_variables_once(self):
        self.assertEqual(render_prompt("{{ name }} {{count}} {{name}}", {"name": "{{literal}}", "count": 2}),
                         "{{literal}} 2 {{literal}}")
        self.assertEqual(render_prompt("JSON: {ok}", {}), "JSON: {ok}")

    def test_render_rejects_missing_and_expressions(self):
        for template in ["{{missing}}", "{{x.upper()}}", "{{__import__('os')}}", "{{x[0]}}", "{{x", "x}}", "{{}}"]:
            with self.subTest(template=template), self.assertRaises(AIError):
                render_prompt(template, {"x": "hello"})
        with self.assertRaises(AIError):
            render_prompt("{{x}}", {"x": object()})

    def test_default_templates_are_drafts(self):
        self.assertEqual(len(DEFAULT_PROMPTS), 6)
        for template in DEFAULT_PROMPTS.values():
            rendered = render_prompt(template, {"content": "已知事实"})
            self.assertIn("草稿", rendered)
            self.assertIn("已知事实", rendered)
        self.assertIn("不能自动评级", DEFAULT_PROMPTS["考核证据摘要"])


if __name__ == "__main__":
    unittest.main()
