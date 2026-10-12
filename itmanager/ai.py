"""Standard-library AI draft generation; never executes generated content.

API keys are read only from the named environment variable. Failures exposed to
callers are deliberately generic: no key, request prompt, URL or response body
is included in an AIError. No request/response logging is performed.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class AIError(Exception):
    """A safe, user-facing AI configuration, rendering or request error."""


DEFAULT_PROVIDERS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat", "api_key_env": "DEEPSEEK_API_KEY"},
    "kimi": {"name": "Kimi", "base_url": "https://api.moonshot.cn/v1",
             "model": "moonshot-v1-8k", "api_key_env": "MOONSHOT_API_KEY"},
    "qwen": {"name": "Qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "model": "qwen-plus", "api_key_env": "DASHSCOPE_API_KEY"},
}

_DRAFT_RULE = "仅生成供人工审核的草稿，不执行操作，不编造事实；缺失信息标注待确认。\n"
DEFAULT_PROMPTS = {
    "周报": _DRAFT_RULE + "根据以下材料形成周报，包含完成事项、进行中事项、问题风险和下周计划：\n{{content}}",
    "任务拆解": _DRAFT_RULE + "将以下目标拆解为任务，列出建议负责人、依赖、交付物和验收标准；时间和人员不足时标注待确认：\n{{content}}",
    "会议纪要": _DRAFT_RULE + "整理以下会议材料，区分讨论、已明确的决议及待确认事项，列出行动项、负责人和期限：\n{{content}}",
    "风险分析": _DRAFT_RULE + "分析以下材料的风险、依据、可能影响、预警信号和建议缓解措施，区分事实与推测：\n{{content}}",
    "考核证据摘要": _DRAFT_RULE + "汇总以下考核材料的事实、成果、证据出处及证据缺口。不能自动评级、打分、排名或作出人事决定；评价必须由人工完成：\n{{content}}",
    "邮件草稿": _DRAFT_RULE + "根据以下材料撰写邮件草稿，包含主题、正文和待确认信息；不要发送邮件：\n{{content}}",
}

_ALLOWED_HOSTS = {
    "deepseek": frozenset({"api.deepseek.com"}),
    "kimi": frozenset({"api.moonshot.cn", "api.moonshot.ai"}),
    "qwen": frozenset({"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
                       "dashscope-us.aliyuncs.com"}),
}
_PLACEHOLDER = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def render_prompt(template: str, variables: dict) -> str:
    """Replace {{identifier}} once, without evaluating or recursively rendering.

    Missing variables and malformed/expression placeholders raise AIError.
    Values must be strings, numbers, booleans or None; arbitrary objects are not
    stringified. Extra variables are allowed.
    """
    if not isinstance(template, str) or not isinstance(variables, dict):
        raise AIError("提示词模板和变量格式无效。")
    remainder = _PLACEHOLDER.sub("", template)
    if "{{" in remainder or "}}" in remainder:
        raise AIError("提示词仅支持 {{variable}}，不支持表达式。")

    def replace(match):
        key = match.group(1)
        if key not in variables:
            raise AIError("提示词缺少必需变量。")
        value = variables[key]
        if value is not None and type(value) not in (str, int, float, bool):
            raise AIError("提示词变量类型无效。")
        return "" if value is None else str(value)

    return _PLACEHOLDER.sub(replace, template)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Refuse even same-host redirects: Authorization never reaches a second URL.
        return None


def _endpoint(provider, base_url):
    if not isinstance(base_url, str) or not base_url or any(
        c.isspace() or ord(c) < 32 or ord(c) == 127 for c in base_url
    ):
        raise AIError("AI 服务地址无效。")
    try:
        url = urllib.parse.urlsplit(base_url)
        valid = (url.scheme == "https" and url.hostname in _ALLOWED_HOSTS[provider]
                 and url.username is None and url.password is None
                 and url.port in (None, 443) and not url.query and not url.fragment
                 and "\\" not in base_url)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise AIError("AI 服务地址必须使用该提供商的官方 HTTPS 域名。")
    path = url.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urllib.parse.urlunsplit(("https", url.netloc, path, "", ""))


def generate(config: dict, system_prompt: str, user_prompt: str) -> dict:
    """Return {'text': str, 'usage': dict, 'model': str} for an AI draft.

    Required config: provider, explicit model, api_key_env. base_url defaults to
    the provider's official endpoint. timeout defaults to 60 seconds (0 < t <=
    120); temperature defaults to 0.7 (0..2); max_tokens defaults to 2048 (> 0).
    No tools, streaming, retries or generated actions are enabled.
    """
    if not isinstance(config, dict):
        raise AIError("AI 配置必须为字典。")
    provider = config.get("provider")
    if not isinstance(provider, str) or provider not in _ALLOWED_HOSTS:
        raise AIError("不支持的 AI 提供商。")
    model = config.get("model")
    if not isinstance(model, str) or not model.strip():
        raise AIError("必须明确指定 AI 模型。")
    env_name = config.get("api_key_env")
    if not isinstance(env_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise AIError("必须指定有效的 API 密钥环境变量名。")
    key = os.environ.get(env_name, "")
    if not key.strip() or any(c.isspace() or ord(c) < 32 or ord(c) > 126 for c in key):
        raise AIError("API 密钥环境变量未设置或格式无效。")
    endpoint = _endpoint(provider, config.get("base_url", DEFAULT_PROVIDERS[provider]["base_url"]))
    timeout = config.get("timeout", 60)
    temperature = config.get("temperature", 0.7)
    max_tokens = config.get("max_tokens", 2048)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise AIError("请求超时必须大于 0 且不超过 120 秒。")
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise AIError("temperature 必须在 0 到 2 之间。")
    if type(max_tokens) is not int or max_tokens <= 0:
        raise AIError("max_tokens 必须为正整数。")
    if not isinstance(system_prompt, str) or not isinstance(user_prompt, str):
        raise AIError("提示词必须为字符串。")
    payload = {"model": model, "messages": [
        {"role": "system", "content": _DRAFT_RULE + system_prompt},
        {"role": "user", "content": user_prompt}],
        "temperature": temperature, "max_tokens": max_tokens, "stream": False}
    try:
        request = urllib.request.Request(
            endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "Accept": "application/json"}, method="POST")
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise AIError("AI 服务返回非成功状态，请稍后重试。")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise AIError("AI 服务响应过大。")
        data = json.loads(raw.decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        response_model = data.get("model", model)
        if not isinstance(text, str) or not isinstance(usage, dict) or not isinstance(response_model, str):
            raise AIError("AI 服务响应格式无效。")
        # Return only standard numeric usage counters, not arbitrary response metadata.
        usage = {k: v for k, v in usage.items() if k in (
            "prompt_tokens", "completion_tokens", "total_tokens") and type(v) is int and v >= 0}
        return {"text": text, "usage": usage, "model": response_model}
    except urllib.error.HTTPError as exc:
        exc.close()
        raise AIError("AI 服务请求失败，请检查配置或稍后重试。") from None
    except AIError:
        raise
    except Exception:
        # urllib/JSON/SSL error messages may contain remote content or secrets.
        raise AIError("AI 请求失败或响应无效，请检查网络和配置后重试。") from None
