#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 mock 服务: 模拟 OpenAI / Anthropic / Responses 兼容接口, 用于给 api_tester.py 做自测。
   运行: python mock_server.py [port] [--quiet]
   正确 key: sk-test   (Authorization: Bearer 或 x-api-key 均可; 其它 key 返回 401)
   默认把每个请求的全部请求头打到 stdout, 用来核对客户端伪装是否生效。
"""
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
QUIET = "--quiet" in sys.argv
PORT = int(ARGS[0]) if ARGS else 18080
GOOD_KEY = "sk-test"
MODELS = ["mock-small", "mock-large", "mock-embedding", "mock-vision"]
_LOG_LOCK = threading.Lock()

MOCK_PELICAN = '''<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:#dff4ff;display:grid;place-items:center;height:100vh}}
svg{{width:100%;max-height:100vh}}.wheel{{transform-box:fill-box;transform-origin:center;
animation:spin 2s linear infinite}}@keyframes spin{{to{{transform:rotate(360deg)}}}}
</style></head><body><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360">
<rect width="640" height="360" fill="#dff4ff"/><circle cx="550" cy="60" r="30" fill="#ffcc55"/>
<path d="M0 300H640" stroke="#94a3b8" stroke-width="12"/>
<g fill="none" stroke="#334155" stroke-width="6">
<g class="wheel"><circle cx="210" cy="245" r="52"/><path d="M158 245H262M210 193V297"/></g>
<g class="wheel"><circle cx="430" cy="245" r="52"/><path d="M378 245H482M430 193V297"/></g>
<path d="M210 245L270 160L330 245H210L375 160L430 245M255 155H292M375 160L368 135H400" stroke="{color}"/>
</g><ellipse cx="305" cy="130" rx="65" ry="36" fill="white" stroke="#94a3b8" stroke-width="3"/>
<path d="M335 123Q345 95 333 65Q323 30 354 27Q385 28 380 60L364 117" fill="white" stroke="#94a3b8" stroke-width="3"/>
<path d="M372 52L460 66L372 78Z" fill="#ffad33"/><circle cx="363" cy="46" r="4"/>
<path d="M292 157L320 193L330 245M320 148L350 177L386 162" fill="none" stroke="#e89923" stroke-width="8"/>
<text x="20" y="338" font-family="sans-serif" font-size="15" fill="#334155">MOCK · {model} (test fixture)</text>
</svg></body></html>'''


def response_text(body):
    messages = body.get("messages", body.get("input", []))
    fragments = []
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content", "")
        if isinstance(content, str):
            fragments.append(content)
        elif isinstance(content, list):
            fragments.extend(block.get("text", "") for block in content if isinstance(block, dict))
    prompt = "\n".join(fragments)
    if "这是输入长度接收测试" in prompt:
        return "OK"
    if "这是长文本检索测试" in prompt:
        identifiers = json.loads(re.findall(r'\{"front":.*?\}', prompt)[-1])
        answers = {}
        for name, identifier in identifiers.items():
            match = re.search(re.escape(identifier) + r" value=([0-9a-f]+)", prompt)
            answers[name] = match.group(1) if match else "NOT_FOUND"
        return json.dumps(answers)
    if "用 SVG 画一个鹈鹕骑自行车" in json.dumps(body, ensure_ascii=False):
        model = body.get("model", "mock")
        return MOCK_PELICAN.format(model=model, color="#f05252" if model == "mock-small" else "#3b82f6")
    if "SVG" in json.dumps(body, ensure_ascii=False):
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="30" fill="teal"/><text x="25" y="55">MOCK</text></svg>'
    return "在线 看到图片" if saw_image(body) else "在线"


def text_pieces(text):
    size = 128 if "<svg" in text else 1
    return [text[i:i + size] for i in range(0, len(text), size)]


def saw_image(obj) -> bool:
    """报文里是否带了视觉入参 (OpenAI image_url / Anthropic image / Responses input_image)。"""
    if isinstance(obj, dict):
        kind = obj.get("type")
        if kind == "image_url" and obj.get("image_url"):
            return True
        if kind == "image" and isinstance(obj.get("source"), dict):
            return True
        if kind == "input_image" and obj.get("image_url"):
            return True
        return any(saw_image(v) for v in obj.values())
    if isinstance(obj, list):
        return any(saw_image(v) for v in obj)
    return False


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _dump_headers(self):
        if QUIET:
            return
        lines = [f"--> {self.command} {self.path}"]
        for k, v in self.headers.items():
            if k.lower() in ("authorization", "x-api-key"):
                v = v[:12] + "..."
            lines.append(f"    {k}: {v}")
        with _LOG_LOCK:
            print("\n".join(lines), flush=True)

    def _sse(self, events):
        """events: [(event_name or None, payload_obj or raw str), ...]"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        time.sleep(0.15)  # 模拟首字延迟
        for name, payload in events:
            raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            if name:
                self.wfile.write(f"event: {name}\n".encode())
            self.wfile.write(b"data: " + raw.encode() + b"\n\n")
            self.wfile.flush()
            time.sleep(0.05)
        self.close_connection = True

    def _send(self, code, payload, ctype="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth_ok(self):
        bearer = self.headers.get("Authorization", "") == f"Bearer {GOOD_KEY}"
        xkey = self.headers.get("x-api-key", "") == GOOD_KEY
        if not (bearer or xkey):
            self._send(401, {"error": {"message": "Invalid API key", "type": "invalid_request_error"}})
            return False
        return True

    def do_GET(self):
        self._dump_headers()
        path = self.path.split("?")[0]
        if path.endswith("/models"):
            if not self._auth_ok():
                return
            self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
        elif path.endswith("/health"):
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": {"message": f"no route {path}"}})

    def do_POST(self):
        self._dump_headers()
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self._auth_ok():
            return

        if path.endswith("/embeddings"):
            self._send(200, {"object": "list", "data": [{"object": "embedding",
                                                         "index": 0, "embedding": [0.01] * 16}]})
            return
        if path.endswith("/images/generations"):
            self._image(body)
            return

        if path.endswith("/messages"):
            self._anthropic(body)
            return
        if path.endswith("/responses"):
            self._responses(body)
            return
        if not path.endswith("/chat/completions"):
            self._send(404, {"error": {"message": f"no route {path}"}})
            return

        model = body.get("model", "?")
        if model not in MODELS:
            self._send(404, {"error": {"message": f"model '{model}' not found"}})
            return

        text = response_text(body)
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            time.sleep(0.15)  # 模拟首字延迟
            for i, ch in enumerate(text_pieces(text)):
                piece = {"id": "mock-1", "object": "chat.completion.chunk", "model": model,
                         "choices": [{"index": 0, "delta": {"content": ch} if i == 0 else {"content": ch},
                                      "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(piece, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return

        time.sleep(0.2)
        self._send(200, {
            "id": "mock-1", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        })

    def _model_ok(self, body, err_shape):
        model = body.get("model", "?")
        if model in MODELS:
            return model
        self._send(404, err_shape(f"model '{model}' not found"))
        return None

    def _anthropic(self, body):
        """Anthropic Messages: 校验 anthropic-version + 报文结构。"""
        def err(msg, typ="not_found_error"):
            return {"type": "error", "error": {"type": typ, "message": msg}}
        if not self.headers.get("anthropic-version"):
            self._send(400, err("anthropic-version header is required", "invalid_request_error"))
            return
        if not isinstance(body.get("messages"), list) or "max_tokens" not in body:
            self._send(400, err("messages and max_tokens are required", "invalid_request_error"))
            return
        model = self._model_ok(body, err)
        if not model:
            return
        text = response_text(body)
        usage = {"input_tokens": 9, "output_tokens": 2}
        if body.get("stream"):
            msg = {"id": "msg_mock", "type": "message", "role": "assistant", "model": model,
                   "content": [], "stop_reason": None, "usage": {"input_tokens": 9, "output_tokens": 0}}
            ev = [("message_start", {"type": "message_start", "message": msg}),
                  ("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": ""}}),
                  ("ping", {"type": "ping"})]
            ev += [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": ch}}) for ch in text_pieces(text)]
            ev += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                   ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                      "usage": {"output_tokens": 2}}),
                   ("message_stop", {"type": "message_stop"})]
            self._sse(ev)
            return
        time.sleep(0.2)
        self._send(200, {"id": "msg_mock", "type": "message", "role": "assistant", "model": model,
                         "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                         "usage": usage})

    def _responses(self, body):
        """OpenAI Responses API。"""
        def err(msg):
            return {"error": {"message": msg, "type": "invalid_request_error"}}
        if "input" not in body:
            self._send(400, err("'input' is required"))
            return
        model = self._model_ok(body, err)
        if not model:
            return
        text = response_text(body)
        out = [{"type": "message", "id": "msg_mock", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}]
        usage = {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11}
        done = {"id": "resp_mock", "object": "response", "status": "completed", "model": model,
                "output": out, "usage": usage}
        if body.get("stream"):
            ev = [("response.created", {"type": "response.created",
                                        "response": {**done, "status": "in_progress", "output": []}})]
            ev += [("response.output_text.delta", {"type": "response.output_text.delta",
                                                   "item_id": "msg_mock", "output_index": 0,
                                                   "content_index": 0, "delta": ch}) for ch in text_pieces(text)]
            ev += [("response.completed", {"type": "response.completed", "response": done})]
            self._sse(ev)
            return
        time.sleep(0.2)
        self._send(200, done)

    def _image(self, body):
        """OpenAI Images: POST /images/generations。"""
        if not body.get("prompt"):
            self._send(400, {"error": {"message": "'prompt' is required", "type": "invalid_request_error"}})
            return
        if not self._model_ok(body, lambda msg: {"error": {"message": msg, "type": "invalid_request_error"}}):
            return
        time.sleep(0.2)
        self._send(200, {"created": 0, "data": [{"b64_json": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="}]})


if __name__ == "__main__":
    print(f"mock server -> http://127.0.0.1:{PORT}/v1  (key={GOOD_KEY}, models={MODELS})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
