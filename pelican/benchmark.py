"""Independent text-model pelican benchmark and sandboxed native WebView preview."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .config import PELICAN_DEFAULT_MAX_TOKENS
from .paths import DIAGNOSTICS_PATH


PELICAN_PROMPT = """用 SVG 画一个鹈鹕骑自行车的 2D 动画。
请独立创作，直接输出完整、可运行的 HTML 文件。不要输出构思、分析、解释、草稿、省略号或 Markdown 代码围栏。
在 HTML 内嵌 SVG 绘制鹈鹕、自行车和场景，用 CSS 或 SVG 动画呈现骑行。
不使用 JavaScript、外部图片、字体、脚本、CDN、网络资源或本地文件，所有画面均由内嵌 SVG 绘制。
画面适应窗口大小，主体清晰可见。"""

ARTWORK_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
               "font-src data:; base-uri 'none'; form-action 'none'; sandbox")


def still_artwork(content: bytes) -> bytes:
    """A derived thumbnail document; original artwork/response stay untouched."""
    document = content.decode("utf-8")
    # SMIL does not obey animation-play-state. Remove animation elements only
    # in this derived preview. Disable keyframes too, including animations
    # declared with a more specific or inline !important rule.
    document = re.sub(r"<(?:animate|animateMotion|animateTransform|set|discard)\b[^>]*(?:/>|>[\s\S]*?</(?:animate|animateMotion|animateTransform|set|discard)\s*>)",
                      "", document, flags=re.I)
    document = re.sub(r"@(?:-webkit-)?keyframes\b", "@-api-tester-static-keyframes",
                      document, flags=re.I)
    style = ('<style>*,*::before,*::after{animation-play-state:paused!important;animation-delay:-1s!important;'
             'transition:none!important;caret-color:transparent!important}</style>')
    return re.sub(r"</head\s*>", lambda m: style + m.group(), document, count=1,
                  flags=re.I).encode("utf-8") if re.search(r"</head\s*>", document, re.I) else (document + style).encode("utf-8")


class _SvgContent(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.drawable = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if ("svg" in self.stack and not any(t in self.stack for t in
                ("defs", "symbol", "clippath", "mask", "pattern", "marker", "script", "style"))
                and tag in ("path", "circle", "ellipse", "rect", "line", "polyline", "polygon", "text", "use", "image")):
            self.drawable += 1
        if tag not in ("meta", "link", "br", "hr", "img", "input"):
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]


def extract_artwork(text: str) -> str:
    """Choose final complete code, never a defs-only draft from reasoning text."""
    candidates = re.findall(r"```(?:html|svg|xml)?\s*\n(.*?)```", text, re.I | re.S)
    documents, svgs = [], []
    for candidate in [text, *candidates]:
        documents.extend(re.findall(r"(?:<!doctype\s+html\b[^>]*>\s*)?<html\b[\s\S]*?</html\s*>",
                                    candidate, re.I))
        if not re.search(r"<(?:html|body|head)\b", candidate, re.I):
            svgs.extend(re.findall(r"<svg\b[\s\S]*?</svg\s*>", candidate, re.I))
    found_empty = False
    for document in [*reversed(documents), *reversed(svgs)]:
        if not re.search(r"<svg\b[\s\S]*?</svg\s*>", document, re.I):
            continue
        content = _SvgContent()
        content.feed(document)
        if not content.drawable:
            found_empty = True
            continue
        if re.search(r"<html\b", document, re.I):
            return document
        return ('<!doctype html><html><head><meta charset="utf-8">'
                '<style>html,body{margin:0;width:100%;height:100%;display:grid;place-items:center}'
                'svg{width:100%;height:100%;max-height:100vh}</style></head><body>'
                + document + '</body></html>')
    if found_empty:
        raise ValueError("回复中只有 SVG 草稿/定义，没有实际图形，不能作为作品预览；原始回复已保留")
    raise ValueError("模型尚未输出完整作品，回复可能停在构思阶段或已被截断；原始回复已保留")


class PelicanStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.lock = threading.RLock()
        self.records = {}
        if self.directory.is_dir():
            for path in self.directory.glob("*/run.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if (isinstance(record, dict) and record.get("id") == path.parent.name
                            and re.fullmatch(r"[0-9a-f]{32}", record["id"])):
                        if record.get("status") == "running":
                            record.update(status="failed", error="上次运行中断，请重新测试")
                        if (record.get("validation_version") != 2
                                and (record.get("status") == "complete"
                                     or record.get("error", "").startswith("模型未返回"))):
                            raw_path = path.parent / "response.txt"
                            if raw_path.is_file():
                                raw = raw_path.read_text(encoding="utf-8")
                                try:
                                    document = extract_artwork(raw)
                                except ValueError as exc:
                                    record.update(status="failed", error=str(exc))
                                else:
                                    (path.parent / "artwork.html").write_text(document, encoding="utf-8")
                                    record.update(status="complete", error="")
                                record.update(validation_version=2, characters=len(raw))
                                self._write(record)
                        self.records[record["id"]] = record
                except (OSError, ValueError):
                    continue

    def _write(self, record):
        folder = self.directory / record["id"]
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "run.json.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(folder / "run.json")

    def begin(self, model: str, provider: str, protocol: str, max_tokens: int,
              reasoning_effort: str = "", *, prompt: str = PELICAN_PROMPT,
              prompt_mode: str = "fixed") -> dict:
        record = {"id": uuid.uuid4().hex, "model": model, "provider": provider,
                  "protocol": protocol, "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                  "status": "running", "elapsed": 0, "error": "", "max_tokens": max_tokens,
                  "reasoning_effort": reasoning_effort, "characters": 0, "validation_version": 2,
                  "prompt": prompt, "prompt_mode": prompt_mode}
        with self.lock:
            self._write(record)
            folder = self.directory / record["id"]
            (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
            self.records[record["id"]] = record
        return dict(record)

    def finish(self, run_id: str, raw: str, elapsed: float, error: str = "", *, metrics: dict | None = None) -> dict:
        with self.lock:
            record = dict(self.records[run_id])
            folder = self.directory / run_id
            (folder / "response.txt").write_text(raw, encoding="utf-8")
            if not error:
                try:
                    document = extract_artwork(raw)
                except ValueError as exc:
                    error = str(exc)
                else:
                    (folder / "artwork.html").write_text(document, encoding="utf-8")
            record.update(status="failed" if error else "complete", error=error, elapsed=elapsed,
                          characters=len(raw))
            if metrics is not None:
                record.update({key: metrics.get(key) for key in ("output_tokens", "tps", "ttfb")})
            self._write(record)
            self.records[run_id] = record
            return dict(record)

    def list_runs(self):
        with self.lock:
            return [dict(r) for r in sorted(self.records.values(),
                                           key=lambda r: (r["created"], r["id"]), reverse=True)
                    if not r.get("removed")]

    def progress(self, run_id: str, characters: int):
        with self.lock:
            self.records[run_id]["characters"] = characters

    def remove(self, run_id: str):
        """Remove from history durably, including running jobs, without breaking the worker."""
        with self.lock:
            if run_id not in self.records:
                raise FileNotFoundError(run_id)
            self.records[run_id]["removed"] = True
            self._write(self.records[run_id])

    def read(self, run_id: str, filename: str) -> bytes:
        with self.lock:
            if (run_id not in self.records or self.records[run_id].get("removed")
                    or filename not in ("artwork.html", "response.txt")
                    or (filename == "artwork.html" and self.records[run_id].get("status") != "complete")):
                raise FileNotFoundError(run_id)
            return (self.directory / run_id / filename).read_bytes()

    def location(self, run_id: str) -> Path:
        """Resolve only a known, visible result to a fixed local filename."""
        with self.lock:
            record = self.records.get(run_id)
            if not record or record.get("removed"):
                raise FileNotFoundError("记录不存在或已移除")
            folder = (self.directory / run_id).resolve()
            if not folder.is_relative_to(self.directory.resolve()):
                raise ValueError("记录路径无效")
            name = "artwork.html" if record["status"] == "complete" else "run.json"
            path = (folder / name).resolve()
            if not path.is_relative_to(folder) or not path.is_file():
                raise FileNotFoundError("记录文件不存在")
            return path


def run_pelican_test(client, store: PelicanStore, record: dict, on_delta=None) -> dict:
    """Send only the run's prompt, without other artwork, history, tools or images."""
    import time
    start = time.perf_counter()
    chunks = []
    characters = 0

    def receive(piece):
        nonlocal characters
        chunks.append(piece)
        characters += len(piece)
        store.progress(record["id"], characters)
        if on_delta:
            on_delta(piece)

    try:
        result = client.test_chat(record["model"], record.get("prompt", PELICAN_PROMPT), stream=True,
                                  max_tokens=record["max_tokens"], temperature=0.0,
                                  on_delta=receive, with_image=False,
                                  reasoning_effort=record.get("reasoning_effort", ""))
        return store.finish(record["id"], result.text, result.elapsed,
                            "" if result.ok else (result.error or "模型请求失败"), metrics=result.speed_metrics())
    except Exception as exc:
        return store.finish(record["id"], "".join(chunks), time.perf_counter() - start,
                            f"{type(exc).__name__}: {exc}")


PREVIEW_PATH = Path(__file__).with_name("preview.html")
RENDERER_PATH = Path(__file__).with_name("preview.py")


def launch_embedded_preview(url: str, parent_handle: int):
    """Launch one renderer embedded in the supplied Tk tab host."""
    from .runtime import ensure_runtime
    python = ensure_runtime()
    helper = RENDERER_PATH
    DIAGNOSTICS_PATH.mkdir(parents=True, exist_ok=True)
    log_path = DIAGNOSTICS_PATH / "preview-errors.log"
    ready_path = Path(tempfile.gettempdir()) / ("api-tester-preview-" + uuid.uuid4().hex + ".json")
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n--- 鹈鹕预览窗口启动 " + datetime.now().astimezone().isoformat(timespec="seconds") + " ---\n")
        log.flush()
        process = subprocess.Popen([str(python), str(helper), url, "--parent", str(parent_handle),
                                    "--ready", str(ready_path)], stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return process, ready_path


class PelicanPreview:
    def __init__(self, store: PelicanStore, on_generate=None, on_settings=None):
        self.store = store
        self.processes = []
        self.window_lock = threading.Lock()
        self.native_handle = None
        self.context_lock = threading.RLock()
        self.sources = {}
        self.context = {"providers": [], "selected_provider": "", "selected_models": [],
                        "pelican_reasoning": "默认", "pelican_tokens": str(PELICAN_DEFAULT_MAX_TOKENS),
                        "pelican_timeout": "180"}
        self.active = True
        self.on_generate = on_generate
        self.on_settings = on_settings
        owner = self
        prefix = "/" + uuid.uuid4().hex + "/"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, status, body, content_type, csp=""):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if csp:
                    self.send_header("Content-Security-Policy", csp)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlsplit(self.path).path
                host = f"127.0.0.1:{self.server.server_port}"
                if self.headers.get("Host") != host or not path.startswith(prefix):
                    self.send(404, b"Not found", "text/plain")
                    return
                route = path[len(prefix):]
                if not route:
                    self.send(200, PREVIEW_PATH.read_bytes(), "text/html; charset=utf-8",
                              "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                              "connect-src 'self'; frame-src 'self'; base-uri 'none'; frame-ancestors 'none'")
                    return
                if route == "api/context":
                    with owner.context_lock:
                        content = json.dumps({**owner.context, "active": owner.active}, ensure_ascii=False).encode("utf-8")
                    self.send(200, content, "application/json; charset=utf-8")
                    return
                if route == "api/runs":
                    self.send(200, json.dumps(store.list_runs(), ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")
                    return
                match = re.fullmatch(r"(artwork|thumbnail|raw)/([0-9a-f]{32})", route)
                if match:
                    kind, run_id = match.groups()
                    try:
                        content = store.read(run_id, "response.txt" if kind == "raw" else "artwork.html")
                        if kind == "thumbnail":
                            content = still_artwork(content)
                    except OSError:
                        self.send(404, b"Not found", "text/plain")
                        return
                    self.send(200, content, ("text/plain" if kind == "raw" else "text/html")
                              + "; charset=utf-8", ARTWORK_CSP)
                    return
                self.send(404, b"Not found", "text/plain")

            def mutation_route(self):
                origin = f"http://127.0.0.1:{self.server.server_port}"
                path = urlsplit(self.path).path
                if (self.headers.get("Host") != origin.removeprefix("http://")
                        or self.headers.get("Origin", origin) != origin or not path.startswith(prefix)):
                    self.send(403, b"Forbidden", "text/plain")
                    return None
                return path[len(prefix):]

            def json_reply(self, status, payload):
                self.send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          "application/json; charset=utf-8")

            def do_POST(self):
                route = self.mutation_route()
                if route is None:
                    return
                match = re.fullmatch(r"api/runs/([0-9a-f]{32})/open", route)
                if route not in ("api/generate", "api/settings") and not match:
                    self.json_reply(404, {"error": "未知操作"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536 or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError("无效的生成请求")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("无效的设置请求")
                    if match:
                        owner.open_result(match.group(1), payload.get("action"))
                        self.json_reply(200, {"opened": True})
                        return
                    if route == "api/settings":
                        if owner.on_settings is None:
                            raise ValueError("请从主界面打开动画测试")
                        self.json_reply(200, owner.on_settings(payload))
                        return
                    if owner.on_generate is None:
                        raise ValueError("请从主界面打开动画测试")
                    options = {key: payload[key] for key in ("max_tokens", "reasoning", "timeout", "prompt_mode", "custom_prompt")
                               if key in payload}
                    if options and owner.on_settings is not None:
                        owner.on_settings(options)
                    self.json_reply(200, owner.on_generate(payload))
                except (ValueError, KeyError) as exc:
                    self.json_reply(400, {"error": str(exc)})
                except FileNotFoundError as exc:
                    self.json_reply(404, {"error": str(exc)})
                except Exception as exc:
                    self.json_reply(500, {"error": f"操作失败: {exc}"})

            def do_DELETE(self):
                route = self.mutation_route()
                if route is None:
                    return
                match = re.fullmatch(r"api/runs/([0-9a-f]{32})", route)
                if not match:
                    self.json_reply(404, {"error": "记录不存在"})
                    return
                try:
                    store.remove(match.group(1))
                    self.json_reply(200, {"removed": True})
                except FileNotFoundError:
                    self.json_reply(404, {"error": "记录不存在"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}{prefix}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def open_result(self, run_id: str, action: str):
        """Open a known record; never accept a client-supplied path or URL."""
        if action == "browser":
            # Check readiness and keep the same CSP/sandbox used by embedding.
            self.store.read(run_id, "artwork.html")
            if not webbrowser.open(self.url + "artwork/" + run_id, new=2):
                raise RuntimeError("无法启动默认浏览器")
        elif action == "explorer":
            path = self.store.location(run_id)
            if sys.platform != "win32":
                raise RuntimeError("文件资源管理器定位当前仅支持 Windows")
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        else:
            raise ValueError("未知的打开方式")

    def set_sources(self, sources: dict, selected_provider: str, selected_models: list,
                    reasoning_label: str = "默认", max_tokens: str = str(PELICAN_DEFAULT_MAX_TOKENS),
                    timeout: str = "180", prompt_mode: str = "fixed", custom_prompt: str = "",
                    templates: dict | None = None):
        with self.context_lock:
            self.sources = sources
            self.context = {
                "providers": [{"id": key, "name": cfg["name"], "models": list(cfg.get("models", []))}
                              for key, cfg in sources.items()],
                "selected_provider": selected_provider, "selected_models": list(selected_models),
                "pelican_reasoning": reasoning_label, "pelican_tokens": str(max_tokens),
                "pelican_timeout": str(timeout),
                "prompt_mode": prompt_mode, "custom_prompt": custom_prompt,
                "templates": dict(templates or {}),
            }

    def get_source(self, source_id: str):
        with self.context_lock:
            if source_id not in self.sources:
                raise ValueError("供应商不存在，请重新选择")
            return dict(self.sources[source_id])

    def set_active(self, active: bool):
        with self.context_lock:
            self.active = bool(active)

    def embed(self, parent_handle: int):
        with self.window_lock:
            self.processes = [p for p in self.processes if p.poll() is None]
            if self.processes:
                return self.processes[0]
            if sys.platform != "win32":
                raise RuntimeError("鹈鹕预览当前需要 Windows WebView2")
            process, ready_path = launch_embedded_preview(self.url, parent_handle)
            self.processes.append(process)
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("内嵌预览启动失败，详情见 pelican/diagnostics/preview-errors.log")
                    if ready_path.is_file():
                        try:
                            self.native_handle = json.loads(ready_path.read_text(encoding="utf-8"))["handle"]
                        except (OSError, ValueError, KeyError):
                            pass
                        else:
                            return process
                    time.sleep(0.1)
                process.terminate()
                raise RuntimeError("内嵌预览加载超时，详情见 pelican/diagnostics/preview-errors.log")
            finally:
                ready_path.unlink(missing_ok=True)

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.server.shutdown()
        self.server.server_close()





