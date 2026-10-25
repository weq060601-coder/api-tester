#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 API 测试工具 (OpenAI 兼容)
================================
功能:
  1. 获取模型      -> GET  {base}/models          (列出全部模型 id)
  2. 测试连通性    -> POST {base}/chat/completions (非流式, 计延迟 + token)
  3. 流式测试      -> POST {base}/chat/completions (stream=true, 计首字延迟 TTFB)
  4. 批量测速      -> 对选中的多个模型并发跑连通性
  5. Embedding     -> POST {base}/embeddings       (可选)

依赖: 仅 Python 标准库 (urllib + tkinter), 无需 pip install。
用法:
  GUI : python api_tester.py
  CLI : python api_tester.py --base-url https://api.deepseek.com/v1 --api-key sk-xxx
  CLI : python api_tester.py --base-url ... --api-key ... --test-model deepseek-chat --stream
"""

from __future__ import annotations

import argparse
import base64
import json
import platform
import queue
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from app_paths import APP_DIRECTORY

from pelican import (PELICAN_DEFAULT_MAX_TOKENS, PELICAN_SETTINGS_VERSION, PELICAN_PROMPT, RESULTS_PATH,
                     PelicanPreview, PelicanStore, migrate_legacy_files,
                     run_pelican_test, saved_output_limit)

# GUI 的连接设置、API Key 和测试结果自动保存到这里, 下次打开时恢复
SETTINGS_PATH = APP_DIRECTORY / "settings.json"
PELICAN_RESULTS_PATH = RESULTS_PATH

APP_TITLE = "本地 API 测试台 · 模型获取 / 连通性检测"
TIMEOUT_DEFAULT = 30
UA = "dsh-local-api-tester/1.0"
# 1x1 红色 PNG, 测视觉模型时内嵌进请求, 不依赖外部文件
TEST_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

PRESETS = [
    ("OpenAI 官方", "https://api.openai.com/v1"),
    ("DeepSeek 官方", "https://api.deepseek.com/v1"),
    ("阿里 DashScope(兼容)", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("本地 Ollama", "http://127.0.0.1:11434/v1"),
    ("本地 vLLM/LMStudio", "http://127.0.0.1:8000/v1"),
    ("自定义中转站", ""),
]

# ----------------------------------------------------------------------------
# 客户端伪装 (字面量均取自本机已装客户端, 见交接文档 / README)
# ----------------------------------------------------------------------------
# 客户端版本: 本机实测值, 客户端升级后用 --client-version 覆盖即可, 不必改代码
CLIENT_VERSIONS = {"claude-code": "2.1.288", "codex": "0.160.0", "pi": "1.0.3"}
ANTHROPIC_SDK_VERSION = "0.128.0"  # claude.exe 内置 @anthropic-ai/sdk (var ZE)
OPENAI_SDK_VERSION = "7.19.0"      # pi 依赖的 openai npm 包
NODE_RUNTIME_VERSION = "v24.16.0"  # X-Stainless-Runtime-Version 默认值

PROTOCOLS = ("openai", "anthropic", "responses")
PELICAN_REASONING_LEVELS = ("默认", "低", "中", "高")
PELICAN_REASONING_VALUES = {"默认": "", "低": "low", "中": "medium", "高": "high"}
# profile -> (默认协议, 说明)
PROFILES = {
    "default": ("openai", "本工具原始请求头"),
    "claude-code": ("anthropic", "Claude Code CLI · /v1/messages"),
    "codex": ("responses", "Codex CLI (codex_cli_rs) · /v1/responses"),
    "pi": ("openai", "pi-coding-agent · /v1/chat/completions"),
}
AUTH_MODES = ("auto", "bearer", "x-api-key", "both")

_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _sys_info() -> dict:
    """本机 OS/arch, 分别按 Node 与 Rust 的命名习惯给出 (各客户端 UA 用法不同)。"""
    sysname = platform.system()
    mach = platform.machine().lower()
    release = platform.version() if sysname == "Windows" else platform.release()
    node_plat = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}.get(sysname, sysname.lower())
    node_arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(mach, mach)
    rust_arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(mach, mach)
    stainless_os = {"win32": "Windows", "darwin": "MacOS", "linux": "Linux"}.get(node_plat, f"Other:{node_plat}")
    return {"sysname": sysname or "unknown", "release": release or "unknown", "node_plat": node_plat,
            "node_arch": node_arch, "rust_arch": rust_arch, "stainless_os": stainless_os}


def _stainless_headers(pkg_version: str) -> dict:
    """Stainless 生成的 JS SDK (anthropic / openai) 在 node 运行时自动附带的头。"""
    si = _sys_info()
    return {
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": pkg_version,
        "X-Stainless-OS": si["stainless_os"],
        "X-Stainless-Arch": si["node_arch"],
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": NODE_RUNTIME_VERSION,
        "X-Stainless-Retry-Count": "0",
    }


def profile_headers(profile: str, version: str = "", session_id: str = "") -> dict:
    """返回某个伪装 profile 的请求头 (不含鉴权头)。"""
    ver = (version or CLIENT_VERSIONS.get(profile, "")).strip()
    si = _sys_info()
    if profile == "claude-code":
        # claude.exe: ZO() -> `claude-cli/${VERSION} (external, ${CLAUDE_CODE_ENTRYPOINT ?? "cli"})`
        # kw() -> x-app / User-Agent / X-Claude-Code-Session-Id; SDK 再补 anthropic-version + Stainless
        h = _stainless_headers(ANTHROPIC_SDK_VERSION)
        h.update({
            "X-Stainless-Timeout": "600",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14",
            "x-app": "cli",
            "User-Agent": f"claude-cli/{ver} (external, cli)",
            "X-Claude-Code-Session-Id": session_id,
        })
        return h
    if profile == "codex":
        # codex.exe login/src/auth/default_client.rs: "{originator}/{ver} ({os} {os_ver}; {arch}) {terminal}"
        return {
            "originator": "codex_cli_rs",
            "User-Agent": f"codex_cli_rs/{ver} ({si['sysname']} {si['release']}; {si['rust_arch']}) unknown",
            "session_id": session_id,
        }
    if profile == "pi":
        # pi openai-completions: {"User-Agent": getPiUserAgent()}, openai SDK 再补 Stainless
        h = _stainless_headers(OPENAI_SDK_VERSION)
        h["User-Agent"] = f"pi ({si['node_plat']} {si['release']}; {si['node_arch']})"
        return h
    return {"User-Agent": UA}


def parse_headers(lines) -> dict:
    """解析 'Key: Value' 形式的自定义头; 空行与 # 注释跳过; 格式非法抛 ValueError。"""
    if isinstance(lines, str):
        lines = lines.splitlines()
    out: dict = {}
    for raw in lines or []:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or not _HEADER_NAME_RE.match(name):
            raise ValueError(f"自定义头格式错误 (应为 'Key: Value'): {raw!r}")
        if "\r" in value or "\n" in value:
            raise ValueError(f"自定义头的值不能含换行: {name}")
        _merge(out, {name: value})
    return out


def _merge(dst: dict, src: dict) -> dict:
    """按 HTTP 语义 (大小写不敏感) 合并请求头, src 覆盖 dst。"""
    for k, v in src.items():
        for old in [x for x in dst if x.lower() == k.lower()]:
            del dst[old]
        dst[k] = v
    return dst


def mask_headers(headers: dict) -> dict:
    """展示用: 隐去鉴权头的值。"""
    out = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "x-api-key") and v:
            out[k] = v[:12] + "…(已隐藏)"
        else:
            out[k] = v
    return out


# ----------------------------------------------------------------------------
# 核心: HTTP 客户端
# ----------------------------------------------------------------------------
def normalize_base(url: str) -> str:
    """补全 base url: 去尾斜杠; 若无路径则自动补 /v1。"""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    if not slash or not path.strip():
        url = f"{scheme}://{host}/v1"
    return url.rstrip("/")


def join_url(base: str, path: str) -> str:
    return f"{normalize_base(base)}{path}"


class ApiResult:
    __slots__ = ("ok", "status", "data", "text", "error", "elapsed", "ttfb", "usage")

    def __init__(self, ok, status=0, data=None, text="", error="", elapsed=0.0, ttfb=None, usage=None):
        self.ok = ok
        self.status = status
        self.data = data
        self.text = text
        self.error = error
        self.elapsed = elapsed
        self.ttfb = ttfb
        reported = data.get("usage") if isinstance(data, dict) else None
        self.usage = dict(usage if isinstance(usage, dict) else reported if isinstance(reported, dict) else {})

    def speed_metrics(self):
        """Request-average throughput using reported tokens, never character guesses."""
        tokens = self.usage.get("completion_tokens", self.usage.get("output_tokens"))
        if type(tokens) is not int or tokens < 0:
            tokens = None
        return {"output_tokens": tokens,
                "tps": tokens / self.elapsed if tokens is not None and self.elapsed > 0 else None,
                "elapsed": self.elapsed, "ttfb": self.ttfb}

    def __repr__(self):
        return f"<ApiResult ok={self.ok} status={self.status} {self.elapsed:.2f}s>"


class ApiClient:
    def __init__(self, base_url: str, api_key: str = "", timeout: int = TIMEOUT_DEFAULT,
                 proxy: str = "", insecure: bool = False, profile: str = "default",
                 protocol: str = "", auth_mode: str = "auto", client_version: str = "",
                 ua: str = "", extra_headers: dict | None = None):
        self.base_url = normalize_base(base_url)
        self.api_key = (api_key or "").strip()
        self.timeout = max(1, int(timeout))
        self.insecure = insecure
        if profile not in PROFILES:
            raise ValueError(f"未知 profile: {profile} (可选: {', '.join(PROFILES)})")
        self.profile = profile
        self.protocol = protocol or PROFILES[profile][0]
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"未知协议: {self.protocol} (可选: {', '.join(PROTOCOLS)})")
        if auth_mode not in AUTH_MODES:
            raise ValueError(f"未知鉴权方式: {auth_mode} (可选: {', '.join(AUTH_MODES)})")
        self.auth_mode = auth_mode
        self.session_id = str(uuid.uuid4())
        # 优先级: profile < --ua < --header; 鉴权头单独处理, profile 永不覆盖它
        self.static_headers = profile_headers(profile, client_version, self.session_id)
        if ua.strip():
            _merge(self.static_headers, {"User-Agent": ua.strip()})
        self.extra_headers = dict(extra_headers or {})
        handlers = []
        if proxy.strip():
            handlers.append(urllib.request.ProxyHandler({"http": proxy.strip(), "https": proxy.strip()}))
        else:
            handlers.append(urllib.request.ProxyHandler())  # 遵守系统代理
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    # -- 底层请求 -----------------------------------------------------------
    def auth_headers(self) -> dict:
        if not self.api_key:
            return {}
        mode = self.auth_mode
        if mode == "auto":
            mode = "x-api-key" if self.protocol == "anthropic" else "bearer"
        h = {}
        if mode in ("bearer", "both"):
            h["Authorization"] = f"Bearer {self.api_key}"
        if mode in ("x-api-key", "both"):
            h["x-api-key"] = self.api_key
        return h

    def effective_headers(self, stream: bool = False, has_body: bool = True) -> dict:
        """实际发出的请求头 (除 urllib 自动加的 Host/Content-Length/Connection/Accept-Encoding)。"""
        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        if has_body:
            headers["Content-Type"] = "application/json"
        _merge(headers, self.static_headers)
        _merge(headers, self.auth_headers())
        _merge(headers, self.extra_headers)
        return headers

    def chat_path(self) -> str:
        if self.protocol == "anthropic":
            # claude.exe: this._client.post("/v1/messages?beta=true", ...)
            return "/messages?beta=true" if self.profile == "claude-code" else "/messages"
        if self.protocol == "responses":
            return "/responses"
        return "/chat/completions"

    def _build(self, path: str, body: dict | None, stream: bool):
        headers = self.effective_headers(stream, body is not None)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(join_url(self.base_url, path), data=data,
                                     headers=headers, method="POST" if body is not None else "GET")
        return req

    def request(self, path: str, body: dict | None = None) -> ApiResult:
        """普通 JSON 请求 (非流式)。"""
        req = self._build(path, body, stream=False)
        t0 = time.perf_counter()
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                cost = time.perf_counter() - t0
                text = raw.decode("utf-8", "replace")
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = None
                return ApiResult(True, resp.status, data, text, elapsed=cost)
        except urllib.error.HTTPError as e:
            cost = time.perf_counter() - t0
            text = e.read().decode("utf-8", "replace") if hasattr(e, "read") else str(e)
            return ApiResult(False, e.code, None, text, error=f"HTTP {e.code} {e.reason}", elapsed=cost)
        except urllib.error.URLError as e:
            return ApiResult(False, 0, None, "", error=f"网络错误: {e.reason}",
                             elapsed=time.perf_counter() - t0)
        except socket.timeout:
            return ApiResult(False, 0, None, "", error=f"超时 (>{self.timeout}s)",
                             elapsed=time.perf_counter() - t0)
        except Exception as e:  # noqa: BLE001
            return ApiResult(False, 0, None, "", error=f"{type(e).__name__}: {e}",
                             elapsed=time.perf_counter() - t0)

    def request_stream(self, path: str, body: dict, on_delta=None) -> ApiResult:
        """SSE 流式请求, 统计首字延迟 TTFB 与总耗时。"""
        req = self._build(path, body, stream=True)
        t0 = time.perf_counter()
        ttfb = None
        chunks: list[str] = []
        stream_err = ""
        ended = False
        analysis_items = set()
        usage = {}

        def result(ok, status, text, error=""):
            return ApiResult(ok, status, None, text, error=error,
                             elapsed=time.perf_counter() - t0, ttfb=ttfb, usage=usage)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                if resp.status >= 400:
                    raw = resp.read().decode("utf-8", "replace")
                    return ApiResult(False, resp.status, None, raw, error=f"HTTP {resp.status}",
                                     elapsed=time.perf_counter() - t0)
                for rawline in resp:
                    line = rawline.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        ended = True
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    reported = obj.get("usage")
                    if obj.get("type") == "message_start":
                        reported = (obj.get("message") or {}).get("usage")
                    elif obj.get("type") in ("response.completed", "response.incomplete", "response.failed"):
                        reported = (obj.get("response") or {}).get("usage")
                    if isinstance(reported, dict):
                        usage.update(reported)
                    if obj.get("type") == "response.output_item.added":
                        item = obj.get("item") or {}
                        if item.get("type") == "reasoning" or item.get("channel") in ("analysis", "commentary"):
                            analysis_items.add(item.get("id"))
                    if (obj.get("type") == "response.output_text.delta"
                            and (obj.get("item_id") in analysis_items
                                 or obj.get("channel") in ("analysis", "commentary"))):
                        continue
                    pieces, done, err = parse_stream_event(obj)
                    if done and not chunks and not pieces and not err:
                        final = extract_content(obj.get("response"))
                        if final:
                            pieces = [final]
                    for piece in pieces:
                        if piece and ttfb is None:
                            ttfb = time.perf_counter() - t0
                        chunks.append(piece)
                        if on_delta:
                            on_delta(piece)
                    if err:
                        stream_err = err
                    if done or err:
                        ended = True
                        # Chat usage arrives AFTER finish_reason in a separate
                        # chunk. Keep reading it until [DONE] or clean EOF.
                        if err or not obj.get("choices"):
                            break
                text = "".join(chunks)
                if stream_err:
                    return result(False, resp.status, text or stream_err, f"流内错误: {stream_err[:200]}")
                if not ended:
                    return result(False, resp.status, text, "流式响应中断，未收到结束事件；已保留收到的正文")
                return result(True, resp.status, text)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace") if hasattr(e, "read") else str(e)
            return result(False, e.code, text, f"HTTP {e.code} {e.reason}")
        except urllib.error.URLError as e:
            return result(False, 0, "".join(chunks), f"网络错误: {e.reason}")
        except Exception as e:  # noqa: BLE001
            return result(False, 0, "".join(chunks), f"{type(e).__name__}: {e}")

    # -- 业务封装 -----------------------------------------------------------
    def list_models(self) -> tuple[list[str], ApiResult]:
        """获取模型列表; 兼容 {data:[{id}]} / {data:[str]} / {models:[...]} 多种返回。"""
        res = self.request("/models")
        ids: list[str] = []
        if res.ok:
            raw = res.data
            items = None
            if isinstance(raw, dict):
                items = raw.get("data") or raw.get("models") or raw.get("result")
            elif isinstance(raw, list):
                items = raw
            for it in items or []:
                if isinstance(it, str):
                    ids.append(it)
                elif isinstance(it, dict):
                    mid = it.get("id") or it.get("name") or it.get("model")
                    if mid:
                        ids.append(str(mid))
            ids = sorted(dict.fromkeys(ids))
            if not ids:
                res = ApiResult(False, res.status, res.data, res.text,
                                error="接口通但没解析到模型 id", elapsed=res.elapsed)
        return ids, res

    def test_chat(self, model: str, prompt: str = "只回复两个字：在线", stream: bool = False,
                  max_tokens: int = 32, temperature: float = 0.0, on_delta=None,
                  with_image: bool = False, reasoning_effort: str = "") -> ApiResult:
        body = self.chat_body(model, prompt, stream, max_tokens, temperature, with_image,
                              reasoning_effort)
        path = self.chat_path()
        res = self.request_stream(path, body, on_delta) if stream else self.request(path, body)
        if res.ok:
            if stream:
                res.data = {"choices": [{"message": {"content": res.text}}], "usage": res.usage}
            content = extract_content(res.data)
            if content is None:
                res.ok = False
                res.error = f"接口 200 但响应里没解析出正文 (协议={self.protocol})"
        return res

    def chat_body(self, model: str, prompt: str, stream: bool, max_tokens: int,
                  temperature: float, with_image: bool = False,
                  reasoning_effort: str = "") -> dict:
        """按协议构造最小对话报文。with_image 时附带内置测试图, 走各协议的视觉入参。"""
        effort = PELICAN_REASONING_VALUES.get(reasoning_effort, reasoning_effort or "")
        if self.protocol == "anthropic":
            content = [{"type": "text", "text": prompt}]
            if with_image:
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": TEST_PNG_B64}})
            body = {
                "model": model,
                "max_tokens": int(max_tokens),
                "messages": [{"role": "user", "content": content}],
                "temperature": float(temperature),
                "stream": bool(stream),
            }
            if effort:
                budgets = {"low": 1024, "medium": 2048, "high": 4096}
                budget = budgets[effort]
                if int(max_tokens) <= budget:
                    raise ValueError("Anthropic 推理强度需要更高的输出上限（至少高于对应 thinking budget）")
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
                body.pop("temperature", None)
            return body
        if self.protocol == "responses":
            content = [{"type": "input_text", "text": prompt}]
            if with_image:
                content.append({"type": "input_image",
                                "image_url": f"data:image/png;base64,{TEST_PNG_B64}"})
            body = {
                "model": model,
                "input": [{"type": "message", "role": "user", "content": content}],
                "max_output_tokens": max(16, int(max_tokens)),  # OpenAI 要求 >= 16
                "store": False,
                "stream": bool(stream),
            }
            if effort:
                body["reasoning"] = {"effort": effort}
            return body
        content: str | list = prompt
        if with_image:
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{TEST_PNG_B64}"}},
            ]
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": bool(stream),
        }
        if effort:
            body["reasoning_effort"] = effort
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def test_embedding(self, model: str, text: str = "ping") -> ApiResult:
        return self.request("/embeddings", {"model": model, "input": text})

    def test_image(self, model: str, prompt: str = "a red square") -> ApiResult:
        """绘图模型: POST /images/generations, 成功标准是返回 url 或 b64。"""
        res = self.request("/images/generations", {
            "model": model,
            "prompt": prompt or "a red square",
            "n": 1,
            "size": "256x256",
            "response_format": "url",
        })
        if res.ok:
            summary = extract_image(res.data)
            if summary:
                res.text = summary
            else:
                res.ok = False
                res.error = "接口 200 但响应里没有 data[0].url 或 b64_json"
        return res


def extract_content(data) -> str | None:
    """从 chat/completions 响应里挖出正文。"""
    if not isinstance(data, dict):
        return None
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        ch = choices[0]
        msg = ch.get("message") or {}
        if isinstance(msg, dict) and msg.get("content") is not None:
            return str(msg["content"])
        if ch.get("text") is not None:
            return str(ch["text"])
        delta = ch.get("delta") or {}
        if isinstance(delta, dict) and delta.get("content"):
            return str(delta["content"])
    if data.get("output_text"):
        return str(data["output_text"])
    content = data.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # Anthropic Messages: content=[{type:text,text}]
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            return "".join(texts)
    if isinstance(data.get("output"), list):  # Responses: output=[{type:message,content:[output_text]}]
        texts = []
        for item in data["output"]:
            if (isinstance(item, dict) and item.get("type") == "message"
                    and item.get("channel") not in ("analysis", "commentary")):
                for b in item.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "output_text":
                        texts.append(b.get("text", ""))
        if texts:
            return "".join(texts)
    return None


def extract_image(data) -> str | None:
    """从 /images/generations 响应里取出图片结果的可读摘要。"""
    if not isinstance(data, dict):
        return None
    items = data.get("data") or []
    if not items or not isinstance(items[0], dict):
        return None
    if items[0].get("url"):
        return f"url: {items[0]['url']}"
    if items[0].get("b64_json"):
        return f"b64 {len(items[0]['b64_json'])} 字符"
    return None

def parse_stream_event(obj: dict) -> tuple[list[str], bool, str]:
    """解析一条 SSE data, 兼容 Chat Completions / Anthropic Messages / Responses 三种事件。
       返回 (正文片段, 是否结束, 错误信息)。"""
    typ = obj.get("type")
    if typ == "content_block_delta":  # Anthropic
        d = obj.get("delta") or {}
        return ([d["text"]] if d.get("type") == "text_delta" and d.get("text") else []), False, ""
    if typ == "response.output_text.delta":  # Responses
        d = obj.get("delta")
        return ([d] if isinstance(d, str) and d else []), False, ""
    if typ == "message_delta" and (obj.get("delta") or {}).get("stop_reason") == "max_tokens":
        return [], True, "输出达到 max_tokens 上限，模型未完成作答；请提高输出上限或降低推理强度"
    if typ == "response.incomplete" or (typ == "response.completed" and (obj.get("response") or {}).get("status") == "incomplete"):
        reason = ((obj.get("response") or {}).get("incomplete_details") or {}).get("reason", "unknown")
        return [], True, f"模型输出未完成 ({reason})；若为输出上限，请提高上限或降低推理强度"
    if typ in ("message_stop", "response.completed"):
        return [], True, ""
    if typ in ("error", "response.failed"):
        err = obj.get("error") or (obj.get("response") or {}).get("error") or obj
        return [], True, json.dumps(err, ensure_ascii=False)
    pieces = []
    for ch in obj.get("choices", []) or []:
        if isinstance(ch, dict):
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                pieces.append(piece)
            reason = ch.get("finish_reason")
            if reason in ("length", "max_tokens"):
                return pieces, True, "输出达到 max_tokens 上限，模型未完成作答；请提高输出上限或降低推理强度"
            if reason == "content_filter":
                return pieces, True, "供应商内容过滤中止了输出"
            if reason == "stop":
                return pieces, True, ""
    return pieces, False, ""


def extract_usage(data) -> str:
    u = data.get("usage") if isinstance(data, dict) else None
    if isinstance(u, dict) and u:
        return "tokens: " + ", ".join(f"{k}={v}" for k, v in u.items() if isinstance(v, int))
    return ""


def force_utf8_console() -> None:
    """Windows 控制台默认 GBK, 中文会乱码 —— 强制切 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # py3.7+
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass
    

# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def run_cli(args) -> int:
    try:
        extra = parse_headers(args.header)
        cli = ApiClient(args.base_url, args.api_key, args.timeout, args.proxy, args.insecure,
                        profile=args.profile, protocol=args.protocol, auth_mode=args.auth_mode,
                        client_version=args.client_version, ua=args.ua, extra_headers=extra)
    except ValueError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2
    print(f"[*] Base URL : {cli.base_url}")
    print(f"[*] API Key  : {'已提供 (' + cli.api_key[:6] + '...)' if cli.api_key else '未提供'}")
    print(f"[*] 伪装     : {cli.profile} | 协议 {cli.protocol} -> {cli.chat_path()}")
    if args.show_headers:
        for k, v in mask_headers(cli.effective_headers(args.stream)).items():
            print(f"     {k}: {v}")
    print("-" * 62)

    ids: list[str] = []
    if not args.skip_models:
        ids, res = cli.list_models()
        if res.ok:
            print(f"[OK] 获取模型成功: {len(ids)} 个, 耗时 {res.elapsed * 1000:.0f} ms")
            for m in ids[: args.limit]:
                print("     -", m)
            if len(ids) > args.limit:
                print(f"     ... 其余 {len(ids) - args.limit} 个已省略")
        else:
            print(f"[!!] 获取模型失败: {res.error}  ({res.elapsed * 1000:.0f} ms)")
            if res.text:
                print("     响应:", res.text[:400].replace("\n", " "))
        print("-" * 62)

    if args.test_model:
        targets = args.test_model
    elif args.all:
        targets = ids
    elif ids:
        targets = [ids[0]]
    else:
        targets = []
    if not targets:
        print("[!] 没有可测试的模型 (接口未返回模型, 且未用 --test-model 指定)")
        return 2

    def report(model: str, res: ApiResult) -> bool:
        if res.ok:
            content = res.text if (args.stream or args.image_gen) else (extract_content(res.data) or "")
            usage = "" if args.stream else extract_usage(res.data)
            extra = f" | 首字 {res.ttfb * 1000:.0f} ms" if res.ttfb is not None else ""
            print(f"[OK] {model} | 耗时 {res.elapsed * 1000:.0f} ms{extra} | {usage}")
            print(f"     回复: {content[:200]}")
            return True
        print(f"[!!] {model} | 失败 {res.elapsed * 1000:.0f} ms | {res.error}")
        if res.text:
            print("     响应:", res.text[:400].replace("\n", " "))
        return False

    if len(targets) > 1 and not args.stream:
        print(f"[*] 并发测试 {len(targets)} 个模型 (线程={args.concurrency})")
        t_all = time.perf_counter()
        ok_n = 0
        results: dict[str, ApiResult] = {}
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            if args.image_gen:
                futs = {pool.submit(cli.test_image, m, args.prompt): m for m in targets}
            else:
                futs = {pool.submit(cli.test_chat, m, args.prompt, False, args.max_tokens,
                                    with_image=args.image): m for m in targets}
            for fut in as_completed(futs):
                m = futs[fut]
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001
                    r = ApiResult(False, 0, error=f"{type(e).__name__}: {e}")
                results[m] = r
                ok_n += 1 if report(m, r) else 0
        print(f"\n[汇总] 通 {ok_n} / {len(targets)} | 墙钟总耗时 {time.perf_counter() - t_all:.2f}s")
        return 0 if ok_n == len(targets) else 1

    rc = 0
    for model in targets:
        cb = (lambda p: sys.stdout.write(p) or sys.stdout.flush()) if args.stream else None
        res = cli.test_image(model, args.prompt) if args.image_gen else \
            cli.test_chat(model, args.prompt, args.stream, args.max_tokens,
                          on_delta=cb, with_image=args.image)
        if args.stream and res.ok:
            print()
        if not report(model, res):
            rc = 1
    return rc


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
def build_gui():  # noqa: C901
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk, filedialog, simpledialog

    migrate_legacy_files()

    if sys.platform == "win32":
        try:  # 声明 DPI 感知, 否则高分屏下 Windows 会把整个窗口位图拉伸, 文字发糊
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass

    # 深色配色 (接近 GitHub Dark), 强调色为青绿
    C = {
        "bg": "#0d1117", "panel": "#161b22", "card": "#161b22", "input": "#0d1117",
        "line": "#2a3341", "line_hi": "#3b4656", "hover": "#222b38", "stripe": "#1a212b",
        "fg": "#e6edf3", "muted": "#8b949e", "dim": "#6e7681",
        "acc": "#2dd4bf", "acc_hi": "#5eead4", "acc_dn": "#14b8a6", "acc_lo": "#12332f",
        "acc_fg": "#03201b", "ok": "#3fb950", "err": "#f85149", "warn": "#d29922", "sel": "#1a3a36",
    }

    root = tk.Tk()
    root.withdraw()  # 布局完成前先隐藏, 避免启动时闪一下未上色的窗口
    root.title(APP_TITLE)
    root.configure(bg=C["bg"])
    scale = root.winfo_fpixels("1i") / 96.0

    def px(v: float) -> int:
        return int(round(v * scale))

    fams = set(tkfont.families(root))
    ui_fam = next((f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                               "Noto Sans CJK SC", "Segoe UI") if f in fams), "TkDefaultFont")
    mono_fam = next((f for f in ("Cascadia Mono", "JetBrains Mono", "Consolas", "Menlo",
                                 "DejaVu Sans Mono") if f in fams), "Courier")
    F = {"ui": (ui_fam, 10), "small": (ui_fam, 9), "bold": (ui_fam, 10, "bold"),
         "title": (ui_fam, 14, "bold"), "sec": (ui_fam, 9, "bold"),
         "mono": (mono_fam, 10), "mono_s": (mono_fam, 9)}
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family=ui_fam, size=10)
        except tk.TclError:
            pass

    # 按屏幕自适应尺寸; 小屏下底部状态栏也不能被挤出可见区域
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w = max(min(px(1280), sw - px(40)), min(px(960), sw))
    h = max(min(px(820), sh - px(90)), min(px(620), sh - px(40)))
    root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
    root.minsize(min(px(940), sw), min(px(600), sh - px(40)))

    # ---------------- ttk 样式 ----------------
    def bd(color: str) -> dict:
        return {"bordercolor": color, "lightcolor": color, "darkcolor": color}

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=C["bg"], foreground=C["fg"], fieldbackground=C["input"],
                    troughcolor=C["bg"], selectbackground=C["sel"], selectforeground=C["fg"],
                    insertcolor=C["fg"], font=F["ui"], focuscolor=C["acc"], **bd(C["line"]))
    style.configure("TFrame", background=C["bg"])
    style.configure("Card.TFrame", background=C["card"])
    style.configure("Panel.TFrame", background=C["panel"])
    style.configure("TLabel", background=C["bg"], foreground=C["fg"])
    style.configure("Card.TLabel", background=C["card"], foreground=C["muted"])
    style.configure("Sec.TLabel", background=C["card"], foreground=C["acc"], font=F["sec"])
    style.configure("Hint.TLabel", background=C["card"], foreground=C["dim"], font=F["small"])
    style.configure("Head.TLabel", background=C["bg"], foreground=C["fg"], font=F["bold"])
    style.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"], font=F["small"])

    style.configure("TEntry", padding=(px(8), px(4)), fieldbackground=C["input"], foreground=C["fg"])
    style.map("TEntry", **{k: [("focus", C["acc"]), ("hover", C["line_hi"])] for k in bd("")})
    style.configure("TCombobox", padding=(px(8), px(3)), arrowsize=px(12), background=C["card"])
    style.map("TCombobox",
              fieldbackground=[("readonly", C["input"])], foreground=[("readonly", C["fg"])],
              selectbackground=[("readonly", C["input"])], selectforeground=[("readonly", C["fg"])],
              background=[("active", C["hover"])], arrowcolor=[("", C["muted"])],
              **{k: [("focus", C["acc"]), ("hover", C["line_hi"])] for k in bd("")})
    root.option_add("*TCombobox*Listbox.background", C["panel"])
    root.option_add("*TCombobox*Listbox.foreground", C["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["sel"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["acc_hi"])
    root.option_add("*TCombobox*Listbox.font", F["ui"])
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    style.configure("TButton", background=C["hover"], foreground=C["fg"], padding=(px(11), px(4)),
                    focusthickness=0, relief="flat", **bd(C["line"]))
    style.map("TButton", background=[("disabled", C["card"]), ("pressed", C["line"]), ("active", "#2a3441")],
              foreground=[("disabled", C["dim"])], bordercolor=[("active", C["line_hi"])])
    style.configure("Primary.TButton", background=C["acc"], foreground=C["acc_fg"], font=F["bold"],
                    padding=(px(11), px(4)),
                    **bd(C["acc"]))
    style.map("Primary.TButton",
              background=[("disabled", C["acc_lo"]), ("pressed", C["acc_dn"]), ("active", C["acc_hi"])],
              foreground=[("disabled", "#4f7d76")],
              **{k: [("disabled", C["acc_lo"]), ("pressed", C["acc_dn"]), ("active", C["acc_hi"])]
                 for k in bd("")})
    style.configure("Ghost.TButton", background=C["bg"], foreground=C["muted"], padding=(px(8), px(2)),
                    font=F["small"], **bd(C["bg"]))
    style.configure("Compact.TButton", padding=(px(7), px(4)), font=F["small"])
    style.map("Ghost.TButton", background=[("active", C["hover"])], foreground=[("active", C["acc_hi"])],
              **{k: [("active", C["hover"])] for k in bd("")})

    for name, bg in (("TCheckbutton", C["card"]), ("Bar.TCheckbutton", C["bg"])):
        style.configure(name, background=bg, foreground=C["fg"], indicatorbackground=C["input"],
                        indicatorforeground=C["acc_fg"], indicatormargin=(0, 0, px(6), 0),
                        indicatorsize=px(14),
                        focusthickness=0, upperbordercolor=C["line_hi"], lowerbordercolor=C["line_hi"])
        style.map(name, background=[("active", bg)], foreground=[("active", C["acc_hi"])],
                  indicatorbackground=[("selected", C["acc"]), ("active", C["hover"])])

    style.configure("Treeview", background=C["card"], fieldbackground=C["card"], foreground=C["fg"],
                    rowheight=px(28), borderwidth=0, font=F["ui"], **bd(C["card"]))
    style.map("Treeview", background=[("selected", C["sel"])], foreground=[("selected", C["acc_hi"])])
    style.configure("Treeview.Heading", background=C["panel"], foreground=C["muted"], font=F["sec"],
                    padding=(px(8), px(6)), relief="flat", **bd(C["line"]))
    style.map("Treeview.Heading", background=[("active", C["hover"])])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background=C["line"], troughcolor=C["card"], arrowcolor=C["muted"],
                        gripcount=0, arrowsize=px(11), relief="flat", **bd(C["card"]))
        style.map(name, background=[("active", C["line_hi"])])

    style.configure("TNotebook", background=C["bg"], borderwidth=0, tabmargins=0, **bd(C["line"]))
    style.configure("TNotebook.Tab", background=C["bg"], foreground=C["muted"], padding=(px(14), px(5)),
                    **bd(C["bg"]))
    style.map("TNotebook.Tab", background=[("selected", C["card"]), ("active", C["hover"])],
              foreground=[("selected", C["acc"])], lightcolor=[("selected", C["acc"])],
              bordercolor=[("selected", C["line"])])
    style.configure("Busy.Horizontal.TProgressbar", background=C["acc"], troughcolor=C["hover"],
                    thickness=px(4), **bd(C["hover"]))

    def card(parent, title: str):
        """带 1px 边框的卡片, 返回 (外框, 内容区)。内容区第 0 行是标题。"""
        outer = tk.Frame(parent, bg=C["card"], highlightthickness=1,
                         highlightbackground=C["line"], highlightcolor=C["line"])
        inner = ttk.Frame(outer, style="Card.TFrame", padding=(px(12), px(6), px(12), px(7)))
        inner.pack(fill="both", expand=True)
        ttk.Label(inner, text=title, style="Sec.TLabel").grid(row=0, column=0, columnspan=2,
                                                              sticky="w", pady=(0, px(4)))
        inner.columnconfigure(1, weight=1)
        return outer, inner

    def row(inner, r: int, label: str):
        """卡片里的一行: 左侧标签 + 右侧可扩展容器。"""
        ttk.Label(inner, text=label, style="Card.TLabel").grid(row=r, column=0, sticky="nw",
                                                               padx=(0, px(8)), pady=(px(3), 0))
        fr = ttk.Frame(inner, style="Card.TFrame")
        fr.grid(row=r, column=1, sticky="we", pady=px(1))
        return fr

    def sublabel(parent, text: str):
        ttk.Label(parent, text=text, style="Card.TLabel").pack(side="left", padx=(px(12), px(6)))

    state = {"models": [], "results": {}, "rows": {}, "client": None, "testing": False}
    uiq: queue.Queue = queue.Queue()

    # ---------------- 标题栏 ----------------
    header = ttk.Frame(root, padding=(px(18), px(8), px(18), px(7)))
    header.pack(fill="x")
    tk.Label(header, text="◆", bg=C["bg"], fg=C["acc"], font=(ui_fam, 15)).pack(side="left")
    tk.Label(header, text="API 测试台", bg=C["bg"], fg=C["fg"], font=F["title"]).pack(
        side="left", padx=(px(8), px(10)))
    ttk.Label(header, text="模型获取 · 连通性 · 首字延迟 · 客户端伪装", style="Muted.TLabel").pack(
        side="left", pady=(px(4), 0))
    chip_var = tk.StringVar()
    tk.Label(header, textvariable=chip_var, bg=C["acc_lo"], fg=C["acc_hi"], font=F["mono_s"],
             padx=px(10), pady=px(3)).pack(side="right")
    config_toggle = ttk.Button(header, text="收起设置", style="Ghost.TButton",
                              command=lambda: set_config_visible(state.get("cfg_collapsed", False)))
    config_toggle.pack(side="right", padx=(0, px(12)))
    tk.Frame(root, bg=C["line"], height=1).pack(fill="x")

    # ---------------- 配置区: 两张卡片 ----------------
    cfg = ttk.Frame(root, padding=(px(18), px(8), px(18), 0))
    cfg.pack(fill="x")
    cfg.columnconfigure(0, weight=5, uniform="cfg")
    cfg.columnconfigure(1, weight=4, uniform="cfg")

    conn_o, conn = card(cfg, "连接")
    conn_o.grid(row=0, column=0, sticky="nsew", padx=(0, px(12)))
    fr = row(conn, 1, "Base URL")
    base_var = tk.StringVar(value="https://api.deepseek.com/v1")
    base_entry = ttk.Entry(fr, textvariable=base_var, font=F["mono"], width=12)
    base_entry.pack(side="left", fill="x", expand=True)
    preset_box = ttk.Combobox(fr, state="readonly", width=13, values=[p[0] for p in PRESETS])
    preset_box.current(1)
    preset_box.pack(side="left", padx=(px(8), 0))

    fr = row(conn, 2, "供应商档案")
    provider_box = ttk.Combobox(fr, state="readonly", width=12, values=[])
    provider_box.pack(side="left", fill="x", expand=True)
    ttk.Button(fr, text="保存供应商", style="Compact.TButton", command=lambda: save_provider()).pack(side="left", padx=(px(6), 0))
    ttk.Button(fr, text="删除", style="Compact.TButton", width=3, command=lambda: delete_provider()).pack(side="left", padx=(px(4), 0))

    fr = row(conn, 3, "API Key")
    key_var = tk.StringVar(value="")
    key_entry = ttk.Entry(fr, textvariable=key_var, show="●", font=F["mono"], width=16)
    key_entry.pack(side="left", fill="x", expand=True)
    show_key = tk.BooleanVar(value=False)
    ttk.Checkbutton(fr, text="显示", variable=show_key,
                    command=lambda: key_entry.configure(show="" if show_key.get() else "●")).pack(
        side="left", padx=(px(6), 0))

    fr = row(conn, 4, "命名 Key")
    key_box = ttk.Combobox(fr, state="readonly", width=8, values=[])
    key_box.pack(side="left", fill="x", expand=True)
    ttk.Button(fr, text="保存/新增 Key", style="Compact.TButton", command=lambda: save_key()).pack(side="left", padx=(px(6), 0))
    ttk.Button(fr, text="重命名", style="Compact.TButton", command=lambda: rename_key()).pack(side="left", padx=(px(4), 0))
    ttk.Button(fr, text="删除", style="Compact.TButton", width=3, command=lambda: delete_key()).pack(side="left", padx=(px(4), 0))

    fr = row(conn, 5, "超时 / 代理")
    timeout_var = tk.StringVar(value=str(TIMEOUT_DEFAULT))
    ttk.Entry(fr, textvariable=timeout_var, width=5, justify="center").pack(side="left")
    ttk.Label(fr, text="秒", style="Card.TLabel").pack(side="left", padx=(px(4), px(12)))
    proxy_var = tk.StringVar(value="")
    ttk.Entry(fr, textvariable=proxy_var, font=F["mono"]).pack(side="left", fill="x", expand=True)
    insecure_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(fr, text="忽略证书", variable=insecure_var).pack(side="left", padx=(px(10), 0))

    fr = row(conn, 6, "测试提示词")
    prompt_var = tk.StringVar(value="只回复两个字：在线")
    ttk.Entry(fr, textvariable=prompt_var).pack(side="left", fill="x", expand=True)
    sublabel(fr, "max_tokens")
    max_tok_var = tk.StringVar(value="32")
    ttk.Entry(fr, textvariable=max_tok_var, width=6, justify="center").pack(side="left")

    fr = row(conn, 7, "鹈鹕测试")
    ttk.Label(fr, text="输出上限", style="Card.TLabel").pack(side="left")
    pelican_tokens_var = tk.StringVar(value=str(PELICAN_DEFAULT_MAX_TOKENS))
    ttk.Entry(fr, textvariable=pelican_tokens_var, width=7, justify="center").pack(side="left", padx=px(5))
    ttk.Label(fr, text="推理", style="Card.TLabel").pack(side="left", padx=(px(8), px(4)))
    pelican_reasoning_var = tk.StringVar(value="默认")
    pelican_reasoning_box = ttk.Combobox(fr, textvariable=pelican_reasoning_var,
                                         state="readonly", width=6,
                                         values=PELICAN_REASONING_LEVELS)
    pelican_reasoning_box.pack(side="left")
    ttk.Label(fr, text="超时(秒)", style="Card.TLabel").pack(side="left")
    pelican_timeout_var = tk.StringVar(value="180")
    ttk.Entry(fr, textvariable=pelican_timeout_var, width=5, justify="center").pack(side="left", padx=px(5))
    ttk.Button(fr, text="查看作品", command=lambda: open_pelican_history()).pack(side="right")

    dis_o, dis = card(cfg, "客户端伪装")
    dis_o.grid(row=0, column=1, sticky="nsew")
    fr = row(dis, 1, "Profile")
    profile_box = ttk.Combobox(fr, state="readonly", width=12, values=list(PROFILES))
    profile_box.set("default")
    profile_box.pack(side="left")
    sublabel(fr, "版本")
    cver_var = tk.StringVar(value="")
    ttk.Entry(fr, textvariable=cver_var, width=10, font=F["mono"]).pack(side="left", fill="x", expand=True)

    fr = row(dis, 2, "协议")
    protocol_box = ttk.Combobox(fr, state="readonly", width=12, values=["跟随", *PROTOCOLS])
    protocol_box.set("跟随")
    protocol_box.pack(side="left")
    sublabel(fr, "鉴权")
    auth_box = ttk.Combobox(fr, state="readonly", width=10, values=list(AUTH_MODES))
    auth_box.set("auto")
    auth_box.pack(side="left", fill="x", expand=True)

    fr = row(dis, 3, "UA 覆盖")
    ua_var = tk.StringVar(value="")
    ttk.Entry(fr, textvariable=ua_var, font=F["mono"]).pack(side="left", fill="x", expand=True)

    fr = row(dis, 4, "自定义头")
    hdr_wrap = tk.Frame(fr, bg=C["input"], highlightthickness=1, highlightbackground=C["line"],
                        highlightcolor=C["acc"])
    hdr_wrap.pack(fill="x", expand=True)
    hdr_txt = tk.Text(hdr_wrap, height=2, bg=C["input"], fg=C["fg"], insertbackground=C["acc"],
                      relief="flat", bd=0, font=F["mono_s"], wrap="none", padx=px(8), pady=px(4),
                      selectbackground=C["sel"], highlightthickness=0)
    hdr_txt.pack(fill="x")
    hdr_txt.insert("1.0", "# 每行一个 Key: Value, 优先级最高\n")
    hdr_txt.bind("<FocusIn>", lambda _e: hdr_wrap.configure(highlightbackground=C["acc"]))
    hdr_txt.bind("<FocusOut>", lambda _e: hdr_wrap.configure(highlightbackground=C["line"]))
    note_var = tk.StringVar()
    ttk.Label(dis, textvariable=note_var, style="Hint.TLabel").grid(row=5, column=1, sticky="w")

    # ---------------- 操作栏 ----------------
    bar = ttk.Frame(root, padding=(px(18), px(8), px(18), px(7)))
    bar.pack(fill="x")

    def set_config_visible(visible):
        if visible:
            if not cfg.winfo_manager():
                cfg.pack(fill="x", before=bar)
            config_toggle.configure(text="收起设置")
        else:
            cfg.pack_forget()
            config_toggle.configure(text="展开设置")
        state["cfg_collapsed"] = not visible

    # ---------------- 底部状态栏 (先 pack, 保证不被主体挤出窗口) ----------------
    sbar = tk.Frame(root, bg=C["panel"])
    sbar.pack(side="bottom", fill="x")
    tk.Frame(root, bg=C["line"], height=1).pack(side="bottom", fill="x")
    dot = tk.Label(sbar, text="●", bg=C["panel"], fg=C["ok"], font=F["small"])
    dot.pack(side="left", padx=(px(16), px(4)), pady=px(5))
    stat_var = tk.StringVar(value="就绪")
    tk.Label(sbar, textvariable=stat_var, bg=C["panel"], fg=C["fg"], font=F["small"]).pack(side="left")
    detail_var = tk.StringVar(value="")
    tk.Label(sbar, textvariable=detail_var, bg=C["panel"], fg=C["muted"], font=F["small"]).pack(
        side="left", padx=(px(14), 0))
    count_var = tk.StringVar(value="")
    tk.Label(sbar, textvariable=count_var, bg=C["panel"], fg=C["muted"], font=F["small"]).pack(
        side="right", padx=(0, px(16)))
    tk.Label(sbar, text="F5 获取模型 · Ctrl+Enter 测试 · 右键更多", bg=C["panel"], fg=C["dim"],
             font=F["small"]).pack(side="right", padx=(0, px(18)))

    # ---------------- 主体: 模型列表 | 输出 ----------------
    # 固定两栏网格，不使用可拖动分隔条，避免鼠标误拖导致
    # 模型列表或右侧预览被压缩、偏移或看起来“整个 UI 乱飞”。
    pw = ttk.Frame(root, style="Panel.TFrame")
    pw.pack(fill="both", expand=True, padx=px(18), pady=(0, px(12)))
    pw.columnconfigure(0, weight=2, uniform="main-columns")
    pw.columnconfigure(1, weight=3, uniform="main-columns")
    pw.rowconfigure(0, weight=1)

    left_o = tk.Frame(pw, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
    left_o.grid(row=0, column=0, sticky="nsew", padx=(0, px(8)))
    lhead = ttk.Frame(left_o, style="Card.TFrame", padding=(px(12), px(6), px(8), px(5)))
    lhead.pack(fill="x")
    ttk.Label(lhead, text="模型", style="Sec.TLabel").pack(side="left")
    mcount_var = tk.StringVar(value="0")
    tk.Label(lhead, textvariable=mcount_var, bg=C["hover"], fg=C["muted"], font=F["small"],
             padx=px(7)).pack(side="left", padx=(px(8), 0))
    filter_var = tk.StringVar(value="")
    filt = ttk.Entry(lhead, textvariable=filter_var, width=18)
    filt.pack(side="right")
    ttk.Label(lhead, text="筛选", style="Card.TLabel").pack(side="right", padx=(0, px(6)))
    model_tools = ttk.Frame(left_o, style="Card.TFrame", padding=(px(12), 0, px(8), px(5)))
    model_tools.pack(fill="x")
    model_tools.columnconfigure(0, weight=1)
    ttk.Label(model_tools, text="手动模型 ID（多个用逗号分隔）", style="Card.TLabel").grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, px(3)))
    model_input_var = tk.StringVar()
    model_input = ttk.Entry(model_tools, textvariable=model_input_var, width=12)
    model_input.grid(row=1, column=0, sticky="ew", padx=(0, px(6)))
    ttk.Button(model_tools, text="添加模型", command=lambda: add_models()).grid(row=1, column=1)
    model_options = ttk.Frame(model_tools, style="Card.TFrame")
    model_options.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(3), 0))
    save_models_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(model_options, text="保存模型列表", variable=save_models_var,
                    command=lambda: change_model_persistence()).pack(side="left")
    ttk.Button(model_options, text="删除选中模型", command=lambda: delete_models()).pack(side="right")
    tk.Frame(left_o, bg=C["line"], height=1).pack(fill="x")
    tfr = ttk.Frame(left_o, style="Card.TFrame")
    tfr.pack(fill="both", expand=True)
    cols = ("model", "last", "status")
    tree = ttk.Treeview(tfr, columns=cols, show="headings", selectmode="extended")
    tree.heading("model", text="模型 ID", anchor="w")
    tree.heading("last", text="耗时")
    tree.heading("status", text="结果")
    tree.column("model", width=px(240), anchor="w", stretch=True)
    tree.column("last", width=px(76), anchor="e", stretch=False)
    tree.column("status", width=px(84), anchor="center", stretch=False)
    tree.tag_configure("odd", background=C["stripe"])
    tree.tag_configure("ok", foreground=C["ok"])
    tree.tag_configure("fail", foreground=C["err"])
    tree.tag_configure("none", foreground=C["muted"])
    vsb = ttk.Scrollbar(tfr, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    # A native read-only Entry over the hovered ID cell supplies real text
    # selection (I-beam, drag, Ctrl+C), while the Treeview keeps batch selection.
    model_text = tk.Entry(tree, name="model_text", readonlybackground=C["card"],
                          fg=C["fg"], relief="flat", bd=0, font=F["ui"],
                          cursor="xterm", exportselection=False,
                          selectbackground=C["sel"], selectforeground=C["acc_hi"])
    text_cell = {"iid": ""}

    def hide_model_text(_event=None):
        model_text.place_forget()
        text_cell["iid"] = ""

    def show_model_text(event):
        iid = tree.identify_row(event.y)
        if not iid or tree.identify_column(event.x) != "#1":
            hide_model_text()
            return
        box = tree.bbox(iid, "model")
        if not box:
            return
        if text_cell["iid"] != iid:
            model_text.configure(state="normal")
            model_text.delete(0, "end")
            model_text.insert(0, tree.set(iid, "model"))
            model_text.configure(state="readonly")
            text_cell["iid"] = iid
        x, y, width, height = box
        color = C["sel"] if iid in tree.selection() else (
            C["stripe"] if tree.index(iid) % 2 else C["card"])
        model_text.configure(readonlybackground=color)
        model_text.place(x=x + px(3), y=y, width=max(1, width - px(3)), height=height)

    def select_text_row(event):
        iid = text_cell["iid"]
        if not iid:
            return
        tree.focus(iid)
        if event.state & 0x0004:
            tree.selection_toggle(iid)
        elif event.state & 0x0001:
            rows = list(tree.get_children())
            anchor = tree.selection()[0] if tree.selection() else iid
            lo, hi = sorted((rows.index(anchor), rows.index(iid)))
            tree.selection_set(rows[lo:hi + 1])
        else:
            tree.selection_set(iid)
        model_text.configure(readonlybackground=C["sel"])
        # Leave Entry's native press/drag/release bindings in control of text.

    tree.bind("<Motion>", show_model_text)
    model_text.bind("<Button-1>", select_text_row)
    model_text.bind("<Leave>", lambda e: hide_model_text() if not e.state & 0x0100 else None)
    model_text.bind("<Escape>", lambda e: (hide_model_text(), tree.focus_set()))
    tree.bind("<MouseWheel>", hide_model_text, add="+")
    def scroll_from_model_text(event):
        hide_model_text()
        tree.event_generate("<MouseWheel>", delta=event.delta)
        return "break"

    model_text.bind("<MouseWheel>", scroll_from_model_text)
    tree.bind("<Configure>", hide_model_text, add="+")
    tree.configure(yscrollcommand=lambda first, last: (vsb.set(first, last), hide_model_text()))
    empty_lbl = tk.Label(tfr, text="还没有模型\n在上方手动添加，或按 F5 获取", bg=C["card"], fg=C["dim"],
                         font=F["ui"], justify="center")
    right_o = ttk.Frame(pw)
    right_o.grid(row=0, column=1, sticky="nsew")
    nb = ttk.Notebook(right_o)
    nb.pack(fill="both", expand=True)

    def text_tab(title: str, fg: str):
        fr_ = tk.Frame(nb, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
        txt = tk.Text(fr_, bg=C["card"], fg=fg, insertbackground=C["acc"], wrap="word", relief="flat",
                      bd=0, font=F["mono"], padx=px(12), pady=px(10), spacing1=px(1), spacing3=px(1),
                      selectbackground=C["sel"], highlightthickness=0)
        sb = ttk.Scrollbar(fr_, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        nb.add(fr_, text=title)
        return txt

    log_txt = text_tab("日志", C["fg"])
    resp_txt = text_tab("原始响应", "#a5d6ff")
    pelican_tab = ttk.Frame(nb, style="Card.TFrame")
    nb.add(pelican_tab, text="鹈鹕测试")
    preview_host = tk.Frame(pelican_tab, bg=C["card"], highlightthickness=0)
    preview_host.pack(fill="both", expand=True)
    preview_hint = tk.Label(preview_host, text="正在加载内嵌鹈鹕预览…", bg=C["card"], fg=C["muted"],
                            font=F["ui"], justify="center", wraplength=px(360))
    preview_hint.place(relx=0.5, rely=0.5, anchor="center")
    speed_tab = ttk.Frame(nb, style="Card.TFrame")
    nb.add(speed_tab, text="t/s 速度")
    from speed_view import SpeedView
    speed_view = SpeedView(speed_tab, colors=C, font=F["mono"])
    for tag, color in (("ok", C["ok"]), ("err", C["err"]), ("warn", C["warn"]), ("info", C["fg"]),
                       ("dim", C["muted"]), ("req", C["acc"]), ("ts", C["dim"])):
        log_txt.tag_configure(tag, foreground=color)
    log_txt.tag_configure("req", font=(mono_fam, 10, "bold"))
    # 折行时续行对齐到时间戳之后
    hang = tkfont.Font(root, font=F["mono"]).measure("00:00:00  ")
    for tag in ("ok", "err", "warn", "info", "dim", "req"):
        log_txt.tag_configure(tag, lmargin2=hang)
    ttk.Button(right_o, text="清空", style="Ghost.TButton",
               command=lambda: (log_txt.delete("1.0", "end"), resp_txt.delete("1.0", "end"))).place(
        relx=1.0, y=0, anchor="ne")

    # ---------------- 工具函数 ----------------
    def ui_log(msg: str, tag: str = "info"):
        uiq.put(("log", msg, tag))

    def drain_log():
        try:
            while True:
                kind, *rest = uiq.get_nowait()
                if kind == "log":
                    msg, tag = rest
                    msg = msg.lstrip("\n")
                    if msg.startswith(">>>"):
                        tag = "req"
                        log_txt.insert("end", "\n")
                    elif msg.startswith("     ") and tag == "info":
                        tag = "dim"
                    log_txt.insert("end", time.strftime("%H:%M:%S  "), "ts")
                    log_txt.insert("end", msg + "\n", tag)
                    log_txt.see("end")
                elif kind == "resp":
                    resp_txt.delete("1.0", "end")
                    resp_txt.insert("1.0", rest[0])
                elif kind == "delta":
                    resp_txt.insert("end", rest[0])
                    resp_txt.see("end")
                elif kind == "preview_error":
                    state["preview_loading"] = False
                    preview_hint.configure(text=rest[0], fg=C["err"])
                    preview_hint.place(relx=0.5, rely=0.5, anchor="center")
                elif kind == "preview_ready":
                    state["preview_loading"] = False
                    preview_hint.place_forget()
                elif kind == "pelican_settings":
                    options, done, reply = rest
                    try:
                        for key, var in (("max_tokens", pelican_tokens_var),
                                         ("reasoning", pelican_reasoning_var),
                                         ("timeout", pelican_timeout_var)):
                            if key in options:
                                var.set(options[key])
                        for key, state_key in (("prompt_mode", "svg_prompt_mode"),
                                               ("custom_prompt", "svg_custom_prompt")):
                            if key in options:
                                state[state_key] = options[key]
                        if "template_action" in options:
                            templates = dict(state.get("svg_templates", {}))
                            action, name = options["template_action"], options["template_name"]
                            if action == "save":
                                if name not in templates and len(templates) >= 100:
                                    raise ValueError("最多保存 100 个模板")
                                templates[name] = options["template_prompt"]
                            elif action == "delete":
                                if name not in templates:
                                    raise ValueError("模板不存在")
                                del templates[name]
                            else:
                                target = options["template_target"]
                                if name not in templates or target in templates:
                                    raise ValueError("原模板不存在或新名称已被使用")
                                templates[target] = templates.pop(name)
                            previous = state.get("svg_templates", {})
                            state["svg_templates"] = templates
                        if save_now() is False:
                            if "template_action" in options:
                                state["svg_templates"] = previous
                            raise OSError("设置写入失败，请查看日志")
                        sync_pelican_sources()
                        reply["saved"] = True
                        if "template_action" in options:
                            reply["templates"] = dict(state["svg_templates"])
                    except Exception as exc:
                        reply["error"] = str(exc)
                    finally:
                        done.set()
                elif kind == "stat":
                    stat_var.set(rest[0])
                elif kind == "detail":
                    detail_var.set(rest[0])
                elif kind == "busy":
                    apply_busy(*rest)
                elif kind == "speed":
                    speed_view.add(rest[0])
                elif kind == "model_row":
                    mid, last, st = rest
                    state["results"][mid] = (last, st)
                    if mid not in state["models"]:
                        state["models"].append(mid)
                    if mid in state["rows"]:
                        iid = state["rows"][mid]
                        tree.item(iid, values=(mid, last, st), tags=row_tags(tree.index(iid), st))
                    else:
                        refresh_tree()
                    update_counts()
                    schedule_save()
                elif kind == "pelican_row":
                    source_id, mid, last, status = rest
                    active_source = "saved:" + state["active_provider"] if state.get("active_provider") else "current"
                    if source_id == active_source:
                        if mid in state["models"]:
                            uiq.put(("model_row", mid, last, status))
                    elif source_id.startswith("saved:"):
                        cfg = state.get("providers", {}).get(source_id[6:])
                        if cfg is not None and mid in cfg.get("models", []):
                            cfg.setdefault("results", {})[mid] = (last, status)
                            schedule_save()
                elif kind == "models":
                    refresh_tree()
                    schedule_save()
        except queue.Empty:
            pass
        sync_pelican_sources(force=False)
        state["drain_id"] = root.after(60, drain_log)

    def row_tags(i: int, st: str) -> tuple:
        kind = "ok" if st.startswith("✔") else "fail" if st.startswith("✘") else "none"
        return (kind, "odd") if i % 2 else (kind,)

    def update_counts():
        res = state["results"].values()
        ok_n = sum(1 for _l, s in res if s.startswith("✔"))
        bad_n = sum(1 for _l, s in res if s.startswith("✘"))
        count_var.set(f"{len(state['models'])} 个模型 · 通 {ok_n} · 失败 {bad_n}" if state["models"] else "")

    def refresh_tree():
        hide_model_text()
        selected = {tree.set(iid, "model") for iid in tree.selection()}
        tree.delete(*tree.get_children())
        state["rows"].clear()
        kw = filter_var.get().strip().lower()
        shown = [m for m in state["models"] if kw in m.lower()]
        for i, m in enumerate(shown):
            last, st = state["results"].get(m, ("", "未测"))
            state["rows"][m] = tree.insert("", "end", values=(m, last, st), tags=row_tags(i, st))
        for m in selected:
            if m in state["rows"]:
                tree.selection_add(state["rows"][m])
        mcount_var.set(f"{len(shown)}/{len(state['models'])}" if kw else str(len(state["models"])))
        if state["models"]:
            empty_lbl.place_forget()
        else:
            empty_lbl.place(relx=0.5, rely=0.42, anchor="center")
        update_counts()

    filter_var.trace_add("write", lambda *_a: refresh_tree())

    def add_models(_event=None):
        if state["testing"]:
            ui_log("[!] 请等待当前测试完成再修改模型列表", "warn")
            return "break"
        ids = list(dict.fromkeys(m for m in re.split(r"[,，;；\s]+", model_input_var.get()) if m))
        if not ids:
            model_input.focus_set()
            return "break"
        added = [m for m in ids if m not in state["models"]]
        state["models"].extend(added)
        filter_var.set("")
        refresh_tree()
        tree.selection_set([state["rows"][m] for m in ids])
        tree.see(state["rows"][ids[0]])
        model_input_var.set("")
        save_now()
        sync_pelican_sources()
        ui_log(f"[OK] 新增 {len(added)} 个模型，已选中 {len(ids)} 个；添加不会开始测试", "ok")
        return "break"

    def delete_models(_event=None):
        if state["testing"]:
            ui_log("[!] 请等待当前测试完成再修改模型列表", "warn")
            return "break"
        selected = {tree.set(iid, "model") for iid in tree.selection()}
        if not selected:
            return "break"
        state["models"] = [m for m in state["models"] if m not in selected]
        for m in selected:
            state["results"].pop(m, None)
        refresh_tree()
        save_now()
        sync_pelican_sources()
        ui_log(f"[i] 已从模型列表删除 {len(selected)} 个模型，鹈鹕历史作品保留", "info")
        return "break"

    def change_model_persistence():
        if save_now():
            ui_log("[i] 模型列表及测试结果将随当前供应商保存" if save_models_var.get()
                   else "[i] 当前模型列表仅在本次使用，已清除配置中保存的列表及测试结果", "info")

    model_input.bind("<Return>", add_models)
    tree.bind("<Delete>", delete_models)

    def update_chip(*_a):
        p = profile_box.get()
        proto = protocol_box.get()
        proto = PROFILES[p][0] if proto == "跟随" else proto
        chip_var.set(f"{p} · {proto}")
        note_var.set(PROFILES[p][1])
        schedule_save()

    def on_profile(_evt=None):
        cver_var.set(CLIENT_VERSIONS.get(profile_box.get(), ""))
        update_chip()

    profile_box.bind("<<ComboboxSelected>>", on_profile)
    protocol_box.bind("<<ComboboxSelected>>", update_chip)

    preset_url = dict(PRESETS)
    url_preset = {u: n for n, u in PRESETS if u}
    custom_preset = "自定义中转站"
    guard = {"preset": False, "loading": True}  # loading 在恢复存档完成前挡住自动保存

    def apply_preset(_evt=None):
        """只有选中带地址的预设才填入 URL。自定义项地址为空, 选它什么都不做,
        否则会把用户手输的地址清空或覆盖回默认值。"""
        url = preset_url.get(preset_box.get(), "")
        if not url or url == base_var.get():
            return
        guard["preset"] = True
        base_var.set(url)
        guard["preset"] = False
        state["models"] = []
        state["results"].clear()
        refresh_tree()
        schedule_save()

    def sync_preset(*_a):
        """手改 URL 后让下拉框立刻反映当前地址。否则框里还显示着旧预设,
        再点一下就会按旧预设把地址覆盖回去。"""
        if guard["preset"]:
            return
        name = url_preset.get(base_var.get().strip(), custom_preset)
        if preset_box.get() != name:
            preset_box.set(name)

    preset_box.bind("<<ComboboxSelected>>", apply_preset)
    base_var.trace_add("write", sync_preset)
    # 滚轮在关闭的下拉框上滚动会直接改选中项, 顺手把地址带跑。打开列表后滚轮不受影响
    for box in (preset_box, provider_box, key_box, profile_box, protocol_box, auth_box, pelican_reasoning_box):
        box.bind("<MouseWheel>", lambda _e: "break")

    # ---------------- 自动保存 ----------------
    def schedule_save(*_a):
        if guard["loading"]:
            return
        pending = state.get("save_id")
        if pending:
            root.after_cancel(pending)
        state["save_id"] = root.after(500, save_now)

    def current_keys():
        active = state.get("active_provider", "")
        if active in state.get("providers", {}):
            return state["providers"][active].setdefault("named_keys", {})
        return state.setdefault("named_keys", {})

    def model_slot(key_name=None):
        """模型列表的存档槽位只使用命名 Key 名称，不保存原始 Key。"""
        name = key_box.get() if key_name is None else str(key_name)
        return name.strip() or "__unnamed__"

    def ensure_model_slots(cfg):
        """读取新旧供应商档案，统一成按命名 Key 分组的模型存档。"""
        slots = cfg.get("key_models")
        if not isinstance(slots, dict):
            legacy_slot = str(cfg.get("active_key") or "").strip() or "__unnamed__"
            slots = {legacy_slot: {
                "models": list(cfg.get("models", [])) if isinstance(cfg.get("models"), list) else [],
                "results": dict(cfg.get("results", {})) if isinstance(cfg.get("results"), dict) else {},
                "save_models": cfg.get("save_models", True) is not False,
            }}
        clean = {}
        for slot, value in slots.items():
            if not isinstance(value, dict):
                continue
            models = list(dict.fromkeys(m for m in value.get("models", [])
                                        if isinstance(m, str) and m.strip()))
            results = {m: tuple(v) for m, v in value.get("results", {}).items()
                       if isinstance(m, str) and isinstance(v, (list, tuple)) and len(v) == 2}
            clean[str(slot).strip() or "__unnamed__"] = {
                "models": models, "results": results,
                "save_models": value.get("save_models", True) is not False,
            }
        if not clean:
            clean[model_slot(cfg.get("active_key"))] = {"models": [], "results": {}, "save_models": True}
        cfg["key_models"] = clean
        return clean

    def stash_current_model_slot(cfg=None, key_name=None):
        cfg = cfg or state.get("providers", {}).get(state.get("active_provider", ""))
        if not isinstance(cfg, dict):
            return
        slots = ensure_model_slots(cfg)
        slots[model_slot(key_name)] = {"models": list(state["models"]),
                               "results": dict(state["results"]),
                               "save_models": save_models_var.get()}

    def load_model_slot(cfg, key_name=None):
        slots = ensure_model_slots(cfg)
        slot = model_slot(key_name)
        value = slots.get(slot, {"models": [], "results": {}, "save_models": True})
        save_models_var.set(value.get("save_models", True) is not False)
        state["models"] = list(value.get("models", []))
        state["results"] = dict(value.get("results", {}))
        cfg["active_key"] = "" if slot == "__unnamed__" else slot
        return slot

    def connection_snapshot():
        active_cfg = state.get("providers", {}).get(state.get("active_provider", ""))
        if isinstance(active_cfg, dict):
            stash_current_model_slot(active_cfg)
            slots = {k: dict(v) for k, v in ensure_model_slots(active_cfg).items()}
        else:
            slots = {model_slot(): {"models": list(state["models"]),
                                    "results": dict(state["results"]),
                                    "save_models": save_models_var.get()}}
        return {
            "base_url": base_var.get().strip(), "api_key": key_var.get(),
            "named_keys": dict(current_keys()), "active_key": key_box.get(),
            "timeout": timeout_var.get(), "proxy": proxy_var.get(), "insecure": insecure_var.get(),
            "profile": profile_box.get(), "protocol": protocol_box.get(),
            "auth_mode": auth_box.get(), "client_version": cver_var.get(), "ua": ua_var.get(),
            "headers": hdr_txt.get("1.0", "end").rstrip("\n"),
            "save_models": save_models_var.get(),
            "models": list(state["models"]), "results": dict(state["results"]),
            "key_models": slots,
        }

    def capture_provider():
        active = state.get("active_provider", "")
        if active in state.get("providers", {}):
            state["providers"][active] = connection_snapshot()

    def save_now():
        pending = state.get("save_id")
        if pending:
            root.after_cancel(pending)
        state["save_id"] = None
        capture_provider()
        # Keep unsaved lists usable during this session, but never serialize them.
        providers = {}
        for name, cfg in state.get("providers", {}).items():
            providers[name] = dict(cfg)
            slots = ensure_model_slots(cfg)
            providers[name]["key_models"] = {}
            for slot, value in slots.items():
                providers[name]["key_models"][slot] = dict(value)
                if not value.get("save_models", True):
                    providers[name]["key_models"][slot].update(models=[], results={})
            current = providers[name]["key_models"].get(model_slot(cfg.get("active_key")))
            if current is not None:
                providers[name]["models"] = list(current["models"])
                providers[name]["results"] = dict(current["results"])
                providers[name]["save_models"] = current["save_models"]
        data = {
            "base_url": base_var.get(), "api_key": key_var.get(), "timeout": timeout_var.get(),
            "proxy": proxy_var.get(), "insecure": insecure_var.get(), "stream": stream_var.get(),
            "with_image": image_var.get(),
            "pelican_max_tokens": pelican_tokens_var.get(), "pelican_reasoning": pelican_reasoning_var.get(),
            "pelican_settings_version": PELICAN_SETTINGS_VERSION,
            "pelican_timeout": pelican_timeout_var.get(),
            "svg_prompt_mode": state.get("svg_prompt_mode", "fixed"),
            "svg_custom_prompt": state.get("svg_custom_prompt", ""),
            "svg_templates": state.get("svg_templates", {}),
            "providers": providers, "named_keys": state.get("named_keys", {}),
            "active_provider": provider_box.get(), "active_key": key_box.get(),
            "profile": profile_box.get(), "protocol": protocol_box.get(),
            "auth_mode": auth_box.get(), "client_version": cver_var.get(), "ua": ua_var.get(),
            "headers": hdr_txt.get("1.0", "end").rstrip("\n"), "prompt": prompt_var.get(),
            "max_tokens": max_tok_var.get(), "save_models": save_models_var.get(),
            "models": list(state["models"]) if save_models_var.get() else [],
            "results": {m: list(v) for m, v in state["results"].items()} if save_models_var.get() else {},
            "geometry": root.geometry(),
        }
        try:
            tmp = SETTINGS_PATH.with_name("settings.json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(SETTINGS_PATH)
            return True
        except OSError as exc:
            ui_log(f"[!!] 设置保存失败: {exc}", "err")
            return False

    def load_settings():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return

        def put(var, key):
            if isinstance(data.get(key), str):
                var.set(data[key])

        put(base_var, "base_url")
        put(key_var, "api_key")
        put(timeout_var, "timeout")
        put(proxy_var, "proxy")
        put(prompt_var, "prompt")
        put(max_tok_var, "max_tokens")
        put(ua_var, "ua")
        pelican_tokens_var.set(saved_output_limit(data))
        state["pelican_settings_migrated"] = not data.get("pelican_settings_version")
        if data.get("pelican_reasoning") in PELICAN_REASONING_LEVELS:
            pelican_reasoning_var.set(data["pelican_reasoning"])
        put(pelican_timeout_var, "pelican_timeout")
        state["svg_prompt_mode"] = "custom" if data.get("svg_prompt_mode") == "custom" else "fixed"
        custom = data.get("svg_custom_prompt", "")
        state["svg_custom_prompt"] = custom if isinstance(custom, str) and len(custom) <= 12000 else ""
        templates = data.get("svg_templates", {})
        state["svg_templates"] = {name: text for name, text in templates.items()
                                  if isinstance(name, str) and 0 < len(name) <= 80
                                  and isinstance(text, str) and 0 < len(text) <= 12000
                                  } if isinstance(templates, dict) else {}
        state["svg_templates"] = dict(list(state["svg_templates"].items())[:100])
        if isinstance(data.get("insecure"), bool):
            insecure_var.set(data["insecure"])
        if isinstance(data.get("stream"), bool):
            stream_var.set(data["stream"])
        if isinstance(data.get("with_image"), bool):
            image_var.set(data["with_image"])
        state["providers"] = data.get("providers", {}) if isinstance(data.get("providers"), dict) else {}
        state["named_keys"] = data.get("named_keys", {}) if isinstance(data.get("named_keys"), dict) else {}
        state["providers"] = {name: cfg for name, cfg in state["providers"].items() if isinstance(cfg, dict)}
        for cfg in state["providers"].values():
            keys = cfg.get("named_keys")
            cfg["named_keys"] = {n: v for n, v in keys.items() if isinstance(v, str)} if isinstance(keys, dict) else {}
            ensure_model_slots(cfg)
        provider_box.configure(values=list(state["providers"]))
        if data.get("active_provider") in state["providers"]:
            provider_box.set(data["active_provider"])
            state["active_provider"] = data["active_provider"]
            cfg = state["providers"][data["active_provider"]]
            # Old versions stored all keys globally; keep them and adopt into the last active supplier.
            if "api_key" not in cfg:
                cfg.update(api_key=key_var.get(), named_keys=dict(state["named_keys"]),
                           active_key=data.get("active_key", ""))
        key_box.configure(values=list(current_keys()))
        if data.get("active_key") in current_keys():
            key_box.set(data["active_key"])
        if data.get("profile") in PROFILES:
            profile_box.set(data["profile"])
        if data.get("protocol") in ("跟随", *PROTOCOLS):
            protocol_box.set(data["protocol"])
        if data.get("auth_mode") in AUTH_MODES:
            auth_box.set(data["auth_mode"])
        on_profile()
        put(cver_var, "client_version")  # 放在 on_profile 之后, 保留用户覆盖的版本号
        if isinstance(data.get("headers"), str):
            hdr_txt.delete("1.0", "end")
            hdr_txt.insert("1.0", data["headers"])
        save_models_var.set(data.get("save_models", True) is not False)
        if save_models_var.get() and isinstance(data.get("models"), list):
            state["models"] = [m for m in data["models"] if isinstance(m, str)]
        if save_models_var.get() and isinstance(data.get("results"), dict):
            state["results"] = {m: (v[0], v[1]) for m, v in data["results"].items()
                                if isinstance(v, list) and len(v) == 2}
        if state.get("active_provider") in state.get("providers", {}):
            cfg = state["providers"][state["active_provider"]]
            key_name = str(data.get("active_key") or cfg.get("active_key") or "")
            key_box.set(key_name if key_name in current_keys() else "")
            load_model_slot(cfg, key_name)
        geo = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", str(data.get("geometry") or ""))
        if geo:
            w_, h_, x_, y_ = (int(v) for v in geo.groups())
            sw_, sh_ = root.winfo_screenwidth(), root.winfo_screenheight()
            w_, h_ = min(max(w_, root.minsize()[0]), sw_), min(max(h_, root.minsize()[1]), sh_)
            root.geometry(f"{w_}x{h_}+{min(max(x_, 0), sw_ - w_)}+{min(max(y_, 0), sh_ - h_)}")

    def save_provider():
        if state["testing"]:
            ui_log("[!] 请等待当前测试完成再保存供应商", "warn")
            return
        name = simpledialog.askstring("保存供应商", "给这组接口设置一个名称:",
                                      initialvalue=provider_box.get(), parent=root)
        if not name or not name.strip():
            return
        name = name.strip()
        if not base_var.get().strip():
            ui_log("[!] 先填写供应商 Base URL", "warn")
            return
        capture_provider()
        snapshot = connection_snapshot()
        state.setdefault("providers", {})[name] = snapshot
        if not state.get("active_provider"):
            state["named_keys"] = {}
        state["active_provider"] = name
        provider_box.configure(values=list(state["providers"]))
        provider_box.set(name)
        ui_log(f"[OK] 已保存供应商档案: {name}", "ok")
        save_now()

    def apply_provider(_evt=None):
        if state["testing"]:
            provider_box.set(state.get("active_provider", ""))
            ui_log("[!] 请等待当前测试完成再切换供应商", "warn")
            return
        if provider_box.get() == state.get("active_provider", ""):
            return
        cfg = state.get("providers", {}).get(provider_box.get())
        if not isinstance(cfg, dict):
            return
        capture_provider()
        state["active_provider"] = provider_box.get()
        was_loading = guard["loading"]
        guard["loading"] = True
        if isinstance(cfg.get("base_url"), str):
            base_var.set(cfg["base_url"])
        if cfg.get("profile") in PROFILES:
            profile_box.set(cfg["profile"])
            on_profile()
        if cfg.get("protocol") in ("跟随", *PROTOCOLS):
            protocol_box.set(cfg["protocol"])
        if cfg.get("auth_mode") in AUTH_MODES:
            auth_box.set(cfg["auth_mode"])
        if isinstance(cfg.get("client_version"), str):
            cver_var.set(cfg["client_version"])
        if isinstance(cfg.get("ua"), str):
            ua_var.set(cfg["ua"])
        if isinstance(cfg.get("headers"), str):
            hdr_txt.delete("1.0", "end")
            hdr_txt.insert("1.0", cfg["headers"])
        timeout_var.set(str(cfg.get("timeout", TIMEOUT_DEFAULT)))
        proxy_var.set(cfg.get("proxy", ""))
        insecure_var.set(bool(cfg.get("insecure", False)))
        key_var.set(cfg.get("api_key", ""))
        key_box.configure(values=list(current_keys()))
        active_key = cfg.get("active_key", "")
        key_box.set(active_key if active_key in current_keys() else "")
        load_model_slot(cfg, key_box.get())
        filter_var.set("")
        refresh_tree()
        update_chip()
        guard["loading"] = was_loading
        ui_log(f"[i] 已载入供应商档案: {provider_box.get()}", "info")
        save_now()

    def delete_provider():
        if state["testing"]:
            return
        name = provider_box.get()
        if name and name in state.get("providers", {}):
            del state["providers"][name]
            provider_box.configure(values=list(state["providers"]))
            provider_box.set("")
            state["active_provider"] = ""
            key_var.set("")
            key_box.configure(values=list(current_keys()))
            key_box.set("")
            save_now()

    def save_key():
        value = key_var.get().strip()
        if not value:
            ui_log("[!] 先输入 API Key 再保存", "warn")
            return
        name = simpledialog.askstring("保存 API Key", "给这个 Key 设置一个名称:",
                                      initialvalue=key_box.get(), parent=root)
        if not name or not name.strip():
            return
        name = name.strip()
        old_slot = model_slot()
        current_keys()[name] = value
        active_cfg = state.get("providers", {}).get(state.get("active_provider", ""))
        if isinstance(active_cfg, dict):
            slots = ensure_model_slots(active_cfg)
            slots[name] = slots.pop(old_slot, {"models": list(state["models"]),
                                               "results": dict(state["results"]),
                                               "save_models": save_models_var.get()})
        key_box.configure(values=list(current_keys()))
        key_box.set(name)
        if isinstance(active_cfg, dict):
            active_cfg["active_key"] = name
        ui_log(f"[OK] 已保存命名 Key: {name}", "ok")
        save_now()

    def apply_key(_evt=None):
        if state["testing"]:
            key_box.set(state.get("active_key", ""))
            return
        target = key_box.get()
        active_cfg = state.get("providers", {}).get(state.get("active_provider", ""))
        old_key = active_cfg.get("active_key", "") if isinstance(active_cfg, dict) else ""
        if isinstance(active_cfg, dict):
            stash_current_model_slot(active_cfg, old_key)
        value = current_keys().get(target)
        if isinstance(value, str):
            was_loading = guard["loading"]
            guard["loading"] = True
            state["active_key"] = target
            key_var.set(value)
            key_box.set(target)
            if isinstance(active_cfg, dict):
                active_cfg["active_key"] = target
                load_model_slot(active_cfg, target)
            else:
                state["models"] = []
                state["results"] = {}
            guard["loading"] = was_loading
            filter_var.set("")
            refresh_tree()
            sync_pelican_sources()
            save_now()

    def rename_key():
        old = key_box.get()
        if old not in current_keys():
            ui_log("[!] 先选择要重命名的 Key", "warn")
            return
        name = simpledialog.askstring("重命名 API Key", "新的 Key 名称:", initialvalue=old, parent=root)
        if not name or not name.strip() or name.strip() == old:
            return
        name = name.strip()
        if name in current_keys():
            ui_log("[!] 已有同名 Key，请换一个名称", "warn")
            return
        current_keys()[name] = current_keys().pop(old)
        active_cfg = state.get("providers", {}).get(state.get("active_provider", ""))
        if isinstance(active_cfg, dict):
            slots = ensure_model_slots(active_cfg)
            slots[name] = slots.pop(old, {"models": list(state["models"]),
                                          "results": dict(state["results"]),
                                          "save_models": save_models_var.get()})
        key_box.configure(values=list(current_keys()))
        key_box.set(name)
        if isinstance(active_cfg, dict):
            active_cfg["active_key"] = name
        save_now()

    def delete_key():
        name = key_box.get()
        if name and name in current_keys():
            deleted = current_keys().pop(name)
            active_cfg = state.get("providers", {}).get(state.get("active_provider", ""))
            if isinstance(active_cfg, dict):
                ensure_model_slots(active_cfg).pop(name, None)
            key_box.configure(values=list(current_keys()))
            key_box.set("")
            if key_var.get() == deleted:
                key_var.set("")
                if isinstance(active_cfg, dict):
                    active_cfg["active_key"] = ""
                    load_model_slot(active_cfg, "")
                    refresh_tree()
            save_now()

    provider_box.bind("<<ComboboxSelected>>", apply_provider)
    key_box.bind("<<ComboboxSelected>>", apply_key)

    def detach_changed_provider(*_args):
        if guard["loading"]:
            return
        active = state.get("active_provider", "")
        cfg = state.get("providers", {}).get(active)
        if cfg and normalize_base(base_var.get()) != normalize_base(cfg.get("base_url", "")):
            # A new URL starts a draft supplier; do not overwrite the saved supplier or send its key.
            state["active_provider"] = ""
            provider_box.set("")
            key_var.set("")
            key_box.configure(values=list(current_keys()))
            key_box.set("")
            state["models"] = []
            state["results"] = {}
            hdr_txt.delete("1.0", "end")
            refresh_tree()

    base_var.trace_add("write", detach_changed_provider)

    def sync_key_name(*_args):
        if not guard["loading"] and key_box.get() and current_keys().get(key_box.get()) != key_var.get():
            active_cfg = state.get("providers", {}).get(state.get("active_provider", ""))
            if isinstance(active_cfg, dict):
                # Preserve the old named slot before a manually edited Key
                # detaches from it; the new raw Key must not see its models.
                stash_current_model_slot(active_cfg)
                active_cfg["active_key"] = ""
            key_box.set("")
            state["models"] = []
            state["results"] = {}
            refresh_tree()

    key_var.trace_add("write", sync_key_name)

    def make_client() -> ApiClient | None:
        if state["testing"]:
            return None
        base = base_var.get().strip()
        if not base:
            ui_log("[!] Base URL 不能为空", "err")
            base_entry.focus_set()
            return None
        try:
            to = int(float(timeout_var.get() or TIMEOUT_DEFAULT))
        except ValueError:
            to = TIMEOUT_DEFAULT
        try:
            extra = parse_headers(hdr_txt.get("1.0", "end"))
            proto = protocol_box.get()
            cli = ApiClient(base, key_var.get(), to, proxy_var.get(), insecure_var.get(),
                            profile=profile_box.get(), protocol="" if proto == "跟随" else proto,
                            auth_mode=auth_box.get(), client_version=cver_var.get(),
                            ua=ua_var.get(), extra_headers=extra)
        except ValueError as e:
            ui_log(f"[!] {e}", "err")
            return None
        ui_log(f"[*] 伪装 {cli.profile} · 协议 {cli.protocol} · 请求头:", "info")
        for k, v in mask_headers(cli.effective_headers(stream_var.get())).items():
            ui_log(f"     {k}: {v}", "info")
        state["client"] = cli
        return cli

    def set_busy(busy: bool, note: str = ""):
        """可在工作线程调用: 只改标志位, 控件更新交给主线程。"""
        state["testing"] = busy
        uiq.put(("busy", busy, note))

    def apply_busy(busy: bool, note: str):
        for b in action_btns:
            b.configure(state="disabled" if busy else "normal")
        if busy:
            controls = {}

            def disable(widget):
                for child in widget.winfo_children():
                    if isinstance(child, (ttk.Entry, ttk.Button, ttk.Checkbutton, tk.Text)):
                        controls[child] = child.cget("state")
                        child.configure(state="disabled")
                    disable(child)

            disable(cfg)
            state["disabled_controls"] = controls
        else:
            for widget, previous in state.pop("disabled_controls", {}).items():
                widget.configure(state=previous)
        dot.configure(fg=C["warn"] if busy else C["ok"])
        stat_var.set(note or ("测试中..." if busy else "就绪"))
        if busy:
            prog.pack(side="right", padx=(0, px(4)))
            prog.start(12)
        else:
            prog.stop()
            prog.pack_forget()

    def ask_model() -> str:
        dlg = tk.Toplevel(root)
        dlg.title("输入模型 ID")
        dlg.configure(bg=C["card"])
        dlg.transient(root)
        dlg.resizable(False, False)
        body_ = ttk.Frame(dlg, style="Card.TFrame", padding=px(18))
        body_.pack(fill="both", expand=True)
        ttk.Label(body_, text="要测试的模型 ID", style="Sec.TLabel").pack(anchor="w")
        v = tk.StringVar(value=state["models"][0] if state["models"] else "")
        e = ttk.Entry(body_, textvariable=v, width=44, font=F["mono"])
        e.pack(fill="x", pady=(px(8), px(14)))
        e.focus_set()
        e.select_range(0, "end")
        out = {}

        def ok(_e=None):
            out["v"] = v.get().strip()
            dlg.destroy()

        bf = ttk.Frame(body_, style="Card.TFrame")
        bf.pack(fill="x")
        ttk.Button(bf, text="开始测试", style="Primary.TButton", command=ok).pack(side="right")
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side="right", padx=(0, px(8)))
        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.update_idletasks()
        dlg.geometry(f"+{root.winfo_rootx() + (root.winfo_width() - dlg.winfo_width()) // 2}"
                     f"+{root.winfo_rooty() + root.winfo_height() // 3}")
        dlg.grab_set()
        root.wait_window(dlg)
        return out.get("v", "")

    # ---------------- 动作: 获取模型 ----------------
    def act_models():
        cli = make_client()
        if not cli:
            return
        set_busy(True, "正在获取模型列表...")
        ui_log(f">>> GET {join_url(cli.base_url, '/models')}")

        def work():
            ids, res = cli.list_models()
            if res.ok:
                state["models"] = list(dict.fromkeys([*state["models"], *ids]))
                ui_log(f"[OK] 获取到 {len(ids)} 个模型, 耗时 {res.elapsed * 1000:.0f} ms", "ok")
                uiq.put(("resp", json.dumps(res.data, ensure_ascii=False, indent=2)[:20000]))
                uiq.put(("models",))
                uiq.put(("detail", f"上次获取: {len(ids)} 个模型 · {res.elapsed * 1000:.0f} ms"))
            else:
                ui_log(f"[!!] 获取失败: {res.error} | 耗时 {res.elapsed * 1000:.0f} ms", "err")
                uiq.put(("resp", res.text[:20000]))
            set_busy(False)

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 动作: 单模型连通性 ----------------
    def speed_source():
        """Read the selected connection in Tk before starting a worker."""
        return {"provider": state.get("active_provider") or normalize_base(base_var.get()),
                "key_name": key_box.get() or "未命名 Key"}

    def report_speed(model, result, kind, source):
        metrics = result.speed_metrics()
        uiq.put(("speed", {"model": model, **source, "kind": kind, "metrics": metrics,
                           "ok": result.ok, "error": result.error}))
        return f"{metrics['tps']:.2f} t/s" if metrics["tps"] is not None else "t/s 未报告"

    def act_chat():
        if state["testing"]:
            return
        sel = tree.selection()
        model = tree.set(sel[0], "model") if sel else ask_model()
        if not model:
            return
        cli = make_client()
        if not cli:
            return
        stream = stream_var.get()
        try:
            mt = int(float(max_tok_var.get() or 32))
        except ValueError:
            mt = 32
        set_busy(True, f"测试中: {model}")
        image = image_var.get()
        prompt = prompt_var.get() or "ping"
        source = speed_source()
        ui_log(f">>> POST {join_url(cli.base_url, cli.chat_path())}  model={model} "
               f"stream={stream} image={image}")
        resp_txt.delete("1.0", "end")

        def work():
            t0 = time.perf_counter()
            st = {"last": 0.0}

            def cb(p):
                now = time.perf_counter()
                if now - st["last"] > 0.2:
                    uiq.put(("stat", f"流式接收中... {int((now - t0) * 1000)} ms"))
                    st["last"] = now
                uiq.put(("delta", p))

            res = cli.test_chat(model, prompt, stream, mt,
                                on_delta=cb if stream else None, with_image=image)
            speed = report_speed(model, res, "流式连通性" if stream else "连通性", source)
            ttfb = f" | 首字 {res.ttfb * 1000:.0f} ms" if res.ttfb is not None else ""
            if res.ok:
                content = res.text if stream else (extract_content(res.data) or "")
                usage = extract_usage(res.data)
                ui_log(f"[OK] {model} 连通 | 总耗时 {res.elapsed * 1000:.0f} ms{ttfb} | {speed} | {usage}", "ok")
                ui_log(f"     回复: {content[:300]}", "ok")
                uiq.put(("model_row", model, f"{res.elapsed * 1000:.0f} ms", "✔ 通"))
                if not stream:
                    uiq.put(("resp", json.dumps(res.data, ensure_ascii=False, indent=2)[:20000]))
            else:
                ui_log(f"[!!] {model} 不通 | 耗时 {res.elapsed * 1000:.0f} ms | {res.error}{ttfb}", "err")
                ui_log(f"     响应: {res.text[:400]}", "err")
                uiq.put(("model_row", model, f"{res.elapsed * 1000:.0f} ms", "✘ 失败"))
                uiq.put(("resp", res.text[:20000]))
            uiq.put(("detail", f"上次测试: {model} · {res.elapsed * 1000:.0f} ms · {speed}{ttfb.replace(' |', ' ·')}"))
            set_busy(False)

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 动作: 批量测速 ----------------
    def act_batch():
        if state["testing"]:
            return
        sel = [tree.set(i, "model") for i in tree.selection()]
        targets = sel or [tree.set(i, "model") for i in tree.get_children()]
        if not targets:
            ui_log("[!] 没有模型可测，请在左侧手动添加模型 ID 或点『获取模型』", "warn")
            return
        cli = make_client()
        if not cli:
            return
        targets = targets[:40]
        try:
            mt = int(float(max_tok_var.get() or 16))
        except ValueError:
            mt = 16
        set_busy(True, f"批量测试 {len(targets)} 个模型...")
        image = image_var.get()
        prompt = prompt_var.get() or "ping"
        source = speed_source()
        ui_log(f">>> 批量并发测试 {len(targets)} 个模型 (非流式, max_tokens={mt}, 附带图片={image})")

        def work():
            ok_n = 0
            t_all = time.perf_counter()
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(cli.test_chat, m, prompt, False, mt,
                                    with_image=image): m for m in targets}
                for done_n, fut in enumerate(as_completed(futs), 1):
                    m = futs[fut]
                    uiq.put(("stat", f"批量测试 {done_n}/{len(targets)}"))
                    try:
                        res = fut.result()
                    except Exception as e:  # noqa: BLE001
                        ui_log(f"[!!] {m} 异常: {e}", "err")
                        uiq.put(("model_row", m, "-", "✘ 异常"))
                        continue
                    speed = report_speed(m, res, "批量测速", source)
                    if res.ok:
                        ok_n += 1
                        ui_log(f"[OK] {m} | {res.elapsed * 1000:.0f} ms | {speed}", "ok")
                        uiq.put(("model_row", m, f"{res.elapsed * 1000:.0f} ms", "✔ 通"))
                    else:
                        ui_log(f"[!!] {m} | {res.elapsed * 1000:.0f} ms | {res.error}", "err")
                        uiq.put(("model_row", m, f"{res.elapsed * 1000:.0f} ms", "✘ 失败"))
            total = time.perf_counter() - t_all
            ui_log(f"[汇总] 通 {ok_n} / {len(targets)}, 墙钟总耗时 {total:.2f}s", "ok")
            uiq.put(("detail", f"批量: {ok_n}/{len(targets)} 可用 · 总 {total:.2f}s"))
            set_busy(False)

        threading.Thread(target=work, daemon=True).start()

    # ---------------- 动作: Embedding ----------------
    def act_emb():
        if state["testing"]:
            return
        sel = tree.selection()
        model = tree.set(sel[0], "model") if sel else ""
        if not model:
            ui_log("[!] 先在列表里选一个 embedding 模型", "warn")
            return
        cli = make_client()
        if not cli:
            return
        set_busy(True, f"测试 embedding: {model}")
        ui_log(f">>> POST {join_url(cli.base_url, '/embeddings')}  model={model}")

        def work():
            res = cli.test_embedding(model)
            if res.ok and isinstance(res.data, dict) and res.data.get("data"):
                vec = res.data["data"][0].get("embedding") or []
                ui_log(f"[OK] {model} embedding 可用 | 维度 {len(vec)} | {res.elapsed * 1000:.0f} ms", "ok")
                uiq.put(("model_row", model, f"{res.elapsed * 1000:.0f} ms", f"✔ {len(vec)}维"))
            else:
                ui_log(f"[!!] embedding 失败 | {res.error}", "err")
                ui_log(f"     {res.text[:300]}", "err")
                uiq.put(("model_row", model, f"{res.elapsed * 1000:.0f} ms", "✘ 失败"))
            uiq.put(("resp", (res.text or json.dumps(res.data, ensure_ascii=False))[:20000]))
            set_busy(False)

        threading.Thread(target=work, daemon=True).start()

    def get_pelican_preview():
        if "pelican_preview" not in state:
            migrate_legacy_files()
            store = PelicanStore(PELICAN_RESULTS_PATH)
            state["pelican_executor"] = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pelican")
            state["pelican_preview"] = PelicanPreview(
                store, on_generate=submit_pelican, on_settings=save_pelican_options)
        sync_pelican_sources()
        return state["pelican_preview"]

    def sync_pelican_sources(force=True):
        preview = state.get("pelican_preview")
        if not preview:
            return
        preview.set_active(nb.select() == str(pelican_tab) and root.state() != "iconic"
                           and bool(preview_host.winfo_ismapped()))
        now = time.monotonic()
        if not force and now - state.get("pelican_sources_at", 0) < 0.3:
            return
        state["pelican_sources_at"] = now
        sources = {}
        active = state.get("active_provider", "")
        for name, saved in state.get("providers", {}).items():
            cfg = connection_snapshot() if name == active else dict(saved)
            cfg.update(name=name, pelican_tokens=pelican_tokens_var.get(),
                       pelican_reasoning=pelican_reasoning_var.get(),
                       pelican_timeout=pelican_timeout_var.get())
            sources["saved:" + name] = cfg
        selected_provider = "saved:" + active if active else "current"
        if not active and base_var.get().strip():
            cfg = connection_snapshot()
            cfg.update(name="当前连接 · " + normalize_base(base_var.get()),
                       pelican_tokens=pelican_tokens_var.get(),
                       pelican_reasoning=pelican_reasoning_var.get(),
                       pelican_timeout=pelican_timeout_var.get())
            sources["current"] = cfg
        selected_models = [tree.set(i, "model") for i in tree.selection()]
        preview.set_sources(sources, selected_provider, selected_models,
                            pelican_reasoning_var.get(), pelican_tokens_var.get(), pelican_timeout_var.get(),
                            state.get("svg_prompt_mode", "fixed"), state.get("svg_custom_prompt", ""),
                            state.get("svg_templates", {}))

    def save_pelican_options(payload):
        """Validate in the HTTP thread; apply and save only in the Tk thread."""
        options = {}
        for key, minimum in (("max_tokens", 256), ("timeout", 1)):
            if key in payload:
                try:
                    value = int(str(payload[key]).strip())
                except (ValueError, TypeError):
                    raise ValueError("输出上限须为 ≥256 的整数，超时须为正整数") from None
                if value < minimum:
                    raise ValueError("输出上限须为 ≥256 的整数，超时须为正整数")
                options[key] = str(value)
        if "reasoning" in payload:
            if payload["reasoning"] not in PELICAN_REASONING_LEVELS:
                raise ValueError("请选择有效的推理强度")
            options["reasoning"] = payload["reasoning"]
        if "prompt_mode" in payload:
            if payload["prompt_mode"] not in ("fixed", "custom"):
                raise ValueError("请选择固定鹈鹕题目或自定义 SVG 提示词")
            options["prompt_mode"] = payload["prompt_mode"]
        if "custom_prompt" in payload:
            value = payload["custom_prompt"]
            if not isinstance(value, str) or len(value) > 12000:
                raise ValueError("自定义 SVG 提示词须为不超过 12000 字的文本")
            options["custom_prompt"] = value
        if "template_action" in payload:
            action = payload["template_action"]
            if action not in ("save", "rename", "delete"):
                raise ValueError("无效的模板操作")
            options["template_action"] = action
            for key in ("template_name", "template_target") if action == "rename" else ("template_name",):
                value = payload.get(key)
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
                    raise ValueError("模板名称须为 1 至 80 个字符")
                options[key] = value.strip()
            if action == "save":
                value = payload.get("template_prompt")
                if not isinstance(value, str) or not value.strip() or len(value) > 12000:
                    raise ValueError("模板提示词须为 1 至 12000 个字符")
                options["template_prompt"] = value
        if not options:
            raise ValueError("未提供可保存的参数")
        done, reply = threading.Event(), {}
        uiq.put(("pelican_settings", options, done, reply))
        if not done.wait(5):
            raise ValueError("参数保存超时，请重试")
        if "error" in reply:
            raise ValueError(reply["error"])
        return reply

    def open_preview(preview):
        if state.get("preview_loading"):
            return
        if preview.native_handle and any(p.poll() is None for p in preview.processes):
            return
        state["preview_loading"] = True
        preview_hint.configure(text="正在加载内嵌鹈鹕预览…", fg=C["muted"])
        preview_hint.place(relx=0.5, rely=0.5, anchor="center")
        parent_handle = preview_host.winfo_id()

        def launch():
            try:
                preview.embed(parent_handle)
                uiq.put(("preview_ready",))
            except Exception as exc:
                repair = ("请确认系统已安装 Microsoft Edge WebView2 Runtime" if getattr(sys, "frozen", False)
                          else "可运行 python -m pelican.runtime 修复依赖")
                message = f"内嵌预览加载失败：{exc}\n{repair}"
                uiq.put(("preview_error", message))
                ui_log(f"[!!] {message}", "err")
        threading.Thread(target=launch, daemon=True).start()

    def open_pelican_history():
        set_config_visible(False)
        nb.select(pelican_tab)
        root.update_idletasks()
        try:
            preview = get_pelican_preview()
        except OSError as exc:
            ui_log(f"[!!] 无法读取作品: {exc}", "err")
            return
        open_preview(preview)

    def submit_pelican(payload):
        """Called by the local window's HTTP thread; use immutable snapshots, never read Tk here."""
        preview = state["pelican_preview"]
        source_id = payload.get("provider", "")
        if not isinstance(source_id, str):
            raise ValueError("请选择供应商")
        cfg = preview.get_source(source_id)
        models = payload.get("models")
        if (not isinstance(models, list) or not 1 <= len(models) <= 40
                or any(not isinstance(m, str) or not m.strip() or len(m) > 300 for m in models)):
            raise ValueError("请选择或输入 1 至 40 个模型")
        models = list(dict.fromkeys(m.strip() for m in models))
        prompt_mode = payload.get("prompt_mode", "fixed")
        if prompt_mode not in ("fixed", "custom"):
            raise ValueError("无效的提示词模式")
        custom_prompt = payload.get("custom_prompt", "")
        if not isinstance(custom_prompt, str) or len(custom_prompt) > 12000:
            raise ValueError("自定义 SVG 提示词须为不超过 12000 字的文本")
        if prompt_mode == "custom" and not custom_prompt.strip():
            raise ValueError("请先填写自定义 SVG 提示词")
        prompt = custom_prompt if prompt_mode == "custom" else PELICAN_PROMPT
        try:
            requested_tokens = payload.get("max_tokens", cfg.get("pelican_tokens", PELICAN_DEFAULT_MAX_TOKENS))
            tokens = int(requested_tokens)
            reasoning_label = payload.get("reasoning", cfg.get("pelican_reasoning", "默认"))
            if not isinstance(reasoning_label, str):
                reasoning_label = "默认"
            if reasoning_label not in PELICAN_REASONING_LEVELS:
                reasoning_label = "默认"
            reasoning_effort = PELICAN_REASONING_VALUES[reasoning_label]
            timeout = int(payload.get("timeout", cfg.get("pelican_timeout", 180)))
            if tokens < 256 or timeout < 1:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("鹈鹕测试输出上限须为 ≥256 的整数，超时须为正整数") from None
        if not cfg.get("base_url", "").strip():
            raise ValueError("请先填写供应商 Base URL")
        headers = parse_headers(cfg.get("headers", ""))
        clients = [ApiClient(cfg["base_url"], cfg.get("api_key", ""), timeout,
                             cfg.get("proxy", ""), cfg.get("insecure", False),
                             profile=cfg.get("profile", "default"),
                             protocol="" if cfg.get("protocol", "跟随") == "跟随" else cfg["protocol"],
                             auth_mode=cfg.get("auth_mode", "auto"),
                             client_version=cfg.get("client_version", ""), ua=cfg.get("ua", ""),
                             extra_headers=headers) for _ in models]
        records = [preview.store.begin(model, cfg["name"], client.protocol, tokens, reasoning_effort,
                                       prompt=prompt, prompt_mode=prompt_mode)
                   for model, client in zip(models, clients)]
        ui_log(f"[*] 已加入 {len(records)} 个鹈鹕测试：{cfg['name']}；结果显示在主界面鹈鹕标签页", "info")

        def run(record, client):
            ui_log(f">>> POST {join_url(client.base_url, client.chat_path())}  model={record['model']} 鹈鹕测试")
            try:
                outcome = run_pelican_test(client, preview.store, record)
                success = outcome["status"] == "complete"
                status = "✔ 鹈鹕已生成" if success else "✘ 鹈鹕失败"
                uiq.put(("speed", {"model": record["model"], "provider": cfg["name"],
                    "key_name": cfg.get("active_key") or "未命名 Key", "kind": "鹈鹕 / SVG",
                    "metrics": {"output_tokens": outcome.get("output_tokens"), "tps": outcome.get("tps"),
                                "elapsed": outcome["elapsed"], "ttfb": outcome.get("ttfb")},
                    "ok": success, "error": outcome.get("error", "")}))
                uiq.put(("pelican_row", source_id, record["model"],
                         f"{outcome['elapsed'] * 1000:.0f} ms", status))
                ui_log(f"[{'OK' if success else '!!'}] {record['model']}: "
                       + ("作品已加入测试记录" if success else outcome["error"]), "ok" if success else "err")
            except Exception as exc:
                ui_log(f"[!!] 鹈鹕测试异常: {exc}", "err")

        for record, client in zip(records, clients):
            state["pelican_executor"].submit(run, record, client)
        return {"ids": [r["id"] for r in records]}

    def act_pelican():
        """Open the page; only its Generate button submits model requests."""
        open_pelican_history()

    def export():
        if not state["models"]:
            ui_log("[!] 没有可导出的结果", "warn")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv"), ("JSON", "*.json")],
                                            initialfile="api_test_result.csv")
        if not path:
            return
        rows = [(m, *state["results"].get(m, ("", "未测"))) for m in state["models"]]
        if path.lower().endswith(".json"):
            with open(path, "w", encoding="utf-8") as f:
                json.dump([{"model": r[0], "latency": r[1], "result": r[2]} for r in rows],
                          f, ensure_ascii=False, indent=2)
        else:
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write("model,latency,result\n")
                for r in rows:
                    f.write(",".join(str(x).replace(",", " ") for x in r) + "\n")
        ui_log(f"[OK] 已导出 {len(rows)} 行 -> {path}", "ok")

    # ---------------- 操作栏按钮 ----------------
    btn_models = ttk.Button(bar, text="获取模型", style="Primary.TButton", command=act_models)
    btn_models.pack(side="left")
    btn_chat = ttk.Button(bar, text="测试连通性", style="Primary.TButton", command=act_chat)
    btn_chat.pack(side="left", padx=(px(8), 0))
    tk.Frame(bar, bg=C["line"], width=1).pack(side="left", fill="y", padx=px(12), pady=px(4))
    btn_batch = ttk.Button(bar, text="批量测速", command=act_batch)
    btn_batch.pack(side="left")
    btn_emb = ttk.Button(bar, text="Embedding", command=act_emb)
    btn_emb.pack(side="left", padx=(px(8), 0))
    btn_image = ttk.Button(bar, text="鹈鹕测试", command=act_pelican)
    btn_image.pack(side="left", padx=(px(8), 0))
    stream_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(bar, text="流式 SSE", variable=stream_var, style="Bar.TCheckbutton").pack(
        side="left", padx=(px(16), 0))
    image_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(bar, text="附带测试图", variable=image_var, style="Bar.TCheckbutton").pack(
        side="left", padx=(px(10), 0))
    ttk.Button(bar, text="导出结果", command=export).pack(side="right")
    prog = ttk.Progressbar(bar, mode="indeterminate", length=px(120), style="Busy.Horizontal.TProgressbar")
    action_btns = (btn_models, btn_chat, btn_batch, btn_emb, btn_image)

    # ---------------- 事件 ----------------
    def copy_model_id(iid=None, _event=None):
        iid = iid or tree.focus() or (tree.selection()[0] if tree.selection() else "")
        if not iid or iid not in tree.get_children(""):
            ui_log("[!] 先选中一个模型行，再复制 ID", "warn")
            return
        root.clipboard_clear()
        root.clipboard_append(tree.set(iid, "model"))
        root.update()
        ui_log(f"[i] 已复制模型 ID: {tree.set(iid, 'model')}", "info")
        return "break"

    def on_right(e):
        iid = text_cell["iid"] if e.widget is model_text else tree.identify_row(e.y)
        if iid and iid not in tree.selection():
            tree.selection_set(iid)
        if not tree.selection():
            return
        menu = tk.Menu(root, tearoff=0, bg=C["panel"], fg=C["fg"], activebackground=C["sel"],
                       activeforeground=C["acc_hi"], bd=0, relief="flat", font=F["ui"])
        menu.add_command(label="  复制此模型 ID", command=lambda row=iid: copy_model_id(row))
        menu.add_separator()
        menu.add_command(label="  测试选中模型", command=act_chat)
        menu.add_command(label="  批量测速选中", command=act_batch)
        menu.add_command(label="  查看 t/s 速度", command=lambda: nb.select(speed_tab))
        menu.add_command(label="  测试 Embedding", command=act_emb)
        menu.add_command(label="  打开鹈鹕测试页", command=act_pelican)
        menu.add_command(label="  查看鹈鹕作品", command=open_pelican_history)
        menu.add_command(label="  复制测试结果", command=lambda: (
            root.clipboard_clear(),
            root.clipboard_append("\n".join(" ".join(map(str, tree.item(i, "values")))
                                            for i in tree.selection()))))
        menu.add_separator()
        menu.add_command(label="  删除选中模型", command=delete_models)
        menu.tk_popup(e.x_root, e.y_root)

    tree.bind("<Double-1>", lambda e: act_chat() if tree.identify_row(e.y) else None)
    tree.bind("<Button-3>", on_right)
    model_text.bind("<Button-3>", on_right)
    tree.bind("<Control-c>", lambda e: copy_model_id(_event=e))
    tree.bind("<Control-KeyPress-c>", lambda e: copy_model_id(_event=e))
    tree.bind("<Control-Insert>", lambda e: copy_model_id(_event=e))
    root.bind("<Control-Return>", lambda _e: act_chat())
    root.bind("<F5>", lambda _e: act_models())
    nb.bind("<<NotebookTabChanged>>", lambda _e: open_pelican_history()
            if nb.select() == str(pelican_tab) else set_config_visible(nb.select() != str(speed_tab)))

    ui_log("=== API 测试台 ===", "ok")
    ui_log("1. 填 Base URL + API Key，手动添加模型 ID 或按 F5 获取模型", "info")
    ui_log("2. 选中模型后双击, 或按 Ctrl+Enter 测连通性; 多选后可批量测速", "info")
    ui_log("3. 「客户端伪装」可以模拟 Claude Code / Codex / pi 的请求头和协议", "info")
    ui_log("4. 点「鹈鹕测试」打开页面，确认模型与参数后点页面内「开始生成」才发送请求", "info")
    ui_log("5. 「保存供应商」保存整组设置；「保存/新增 Key」命名保存多个 Key，随供应商切换", "info")
    ui_log("提示: 中转站 Base URL 一般以 /v1 结尾, 不带路径会自动补 /v1", "dim")
    ui_log("设置与 API Key 自动保存；勾选「保存模型列表」时恢复模型及测试结果", "dim")

    for var in (base_var, key_var, timeout_var, proxy_var, insecure_var, stream_var,
                image_var, prompt_var, max_tok_var, cver_var, ua_var, pelican_tokens_var,
                pelican_reasoning_var, pelican_timeout_var):
        var.trace_add("write", schedule_save)
    hdr_txt.bind("<KeyRelease>", schedule_save)
    auth_box.bind("<<ComboboxSelected>>", schedule_save)

    on_profile()
    load_settings()
    guard["loading"] = False
    if state.get("pelican_settings_migrated"):
        schedule_save()
    refresh_tree()
    drain_log()

    def on_close():
        for key in ("save_id", "drain_id"):
            pending = state.get(key)
            if pending:
                root.after_cancel(pending)
                state[key] = None
        save_now()
        if "pelican_preview" in state:
            state["pelican_executor"].shutdown(wait=False, cancel_futures=True)
            state["pelican_preview"].close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.deiconify()
    base_entry.focus_set()
    root.mainloop()
    return 0


# ----------------------------------------------------------------------------
def main():
    force_utf8_console()
    ap = argparse.ArgumentParser(description="本地 API 测试工具 (OpenAI 兼容)")
    ap.add_argument("--gui", action="store_true", help="强制打开图形界面")
    ap.add_argument("--base-url", default="", help="如 https://api.deepseek.com/v1")
    ap.add_argument("--api-key", default="", help="Bearer Key")
    ap.add_argument("--timeout", type=int, default=TIMEOUT_DEFAULT)
    ap.add_argument("--proxy", default="", help="如 http://127.0.0.1:7890")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    ap.add_argument("--test-model", default="", help="指定要测的模型 (可重复传多个用逗号分隔)")
    ap.add_argument("--all", action="store_true", help="测试 /models 返回的全部模型")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=50, help="打印最多多少个模型")
    ap.add_argument("--stream", action="store_true", help="用流式模式测连通性")
    ap.add_argument("--skip-models", action="store_true", help="跳过获取模型, 直接测 --test-model")
    ap.add_argument("--prompt", default="只回复两个字：在线")
    ap.add_argument("--max-tokens", type=int, default=32)
    ap.add_argument("--profile", default="default", choices=list(PROFILES),
                    help="客户端伪装: " + " / ".join(f"{k}={v[1]}" for k, v in PROFILES.items()))
    ap.add_argument("--protocol", default="", choices=["", *PROTOCOLS],
                    help="对话协议, 默认跟随 profile (openai/anthropic/responses)")
    ap.add_argument("--auth-mode", default="auto", choices=list(AUTH_MODES),
                    help="鉴权头: auto(anthropic 用 x-api-key, 其余 Bearer) / bearer / x-api-key / both")
    ap.add_argument("--client-version", default="", help="覆盖 UA 里的客户端版本号")
    ap.add_argument("--ua", default="", help="直接指定 User-Agent (优先于 profile)")
    ap.add_argument("--header", action="append", default=[], metavar="'K: V'",
                    help="追加/覆盖请求头, 可重复 (优先级最高)")
    ap.add_argument("--show-headers", action="store_true", help="打印实际发出的请求头 (key 已隐藏)")
    ap.add_argument("--image", action="store_true", help="对话请求附带一张 1x1 测试图, 用于测视觉模型")
    ap.add_argument("--image-gen", action="store_true",
                    help="改走 POST /images/generations, 用于测绘图模型")
    args = ap.parse_args()

    if args.test_model:
        args.test_model = [m.strip() for m in args.test_model.split(",") if m.strip()]

    if args.gui or not args.base_url:
        try:
            return build_gui()
        except ImportError:
            print("[!] 当前 Python 无 tkinter, 请改用 CLI 参数运行", file=sys.stderr)
            return 1
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())






