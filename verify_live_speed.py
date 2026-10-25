"""Opt-in real embedded-page check: slow localhost SSE only, never saved keys.

Test dependencies: websocket-client and the existing preview runtime.
"""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import websocket

import api_tester
from pelican import PELICAN_PROMPT, PelicanPreview, PelicanStore
from test_api_tester import GuiPersistenceTests, mock_server


def main():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            assert self.headers["Authorization"] == "Bearer sk-test"
            assert body["messages"] == [{"role": "user", "content": PELICAN_PROMPT}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            def event(payload):
                self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
                self.wfile.flush()
            time.sleep(2)
            event({"choices": [{"delta": {"reasoning_content": "MOCK thinking " * 40}}]})
            time.sleep(2)
            artwork = mock_server.MOCK_PELICAN.format(model="mock-small", color="#2dd4bf")
            middle = len(artwork) // 2
            for piece in (artwork[:middle], artwork[middle:]):
                event({"choices": [{"delta": {"content": piece}}]})
                time.sleep(2)
            event({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            event({"choices": [], "usage": {"completion_tokens": 120}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    try:
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            settings = directory / "settings.json"
            settings.write_text(json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                            "api_key": "sk-test", "models": ["mock-small"]}), encoding="utf-8")
            store = PelicanStore(directory / "results")
            previews = []
            def factory(*args, **kwargs):
                preview = PelicanPreview(*args, **kwargs)
                previews.append(preview)
                return preview
            case = GuiPersistenceTests()

            def driver(root):
                root.geometry("1280x940")
                tree = next(w for w in case.widgets(root) if getattr(w, "_name", "")
                            and w.winfo_class() == "Treeview" and "model" in w["columns"])
                tree.selection_set(tree.get_children()[0])
                case.press(root, "鹈鹕测试")
                def pump(seconds):
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        root.update()
                        time.sleep(0.02)
                connection = None
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    pump(0.1)
                    try:
                        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1))
                        target = next(t for t in targets if previews[0].url == t.get("url"))
                        connection = websocket.create_connection(target["webSocketDebuggerUrl"],
                                                                 suppress_origin=True, timeout=5)
                        break
                    except (OSError, StopIteration):
                        pass
                assert connection, "Embedded renderer did not start"
                sequence = 0
                def evaluate(expression):
                    nonlocal sequence
                    sequence += 1
                    connection.send(json.dumps({"id": sequence, "method": "Runtime.evaluate",
                        "params": {"expression": expression, "returnByValue": True}}))
                    while True:
                        reply = json.loads(connection.recv())
                        if reply.get("id") == sequence:
                            assert "exceptionDetails" not in reply.get("result", {}), reply
                            return reply.get("result", {}).get("result", {}).get("value")
                try:
                    pump(1)
                    assert not calls, "Opening the page must never start testing"
                    assert not evaluate("document.getElementById('generate').disabled")
                    evaluate("document.getElementById('generate').click()")
                    phases = set()
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        pump(0.25)
                        state = evaluate("({speed:document.getElementById('detail-speed').textContent,"
                            "card:document.querySelector('.record .speed')?.textContent,"
                            "phase:document.getElementById('detail-tokens').textContent,"
                            "elapsed:document.getElementById('detail-elapsed').textContent,"
                            "art:document.getElementById('art').getAttribute('src')})")
                        if not state["card"]:
                            continue
                        assert state["speed"] == state["card"], state
                        if "等待首字" in state["speed"]:
                            phases.add("waiting")
                        if "估算" in state["speed"] and "思考中" in state["phase"]:
                            phases.add("reasoning")
                            root.lift()
                            root.attributes("-topmost", True)
                            pump(0.1)
                            from PIL import ImageGrab
                            folder = Path(__file__).parent / "pelican" / "diagnostics"
                            folder.mkdir(parents=True, exist_ok=True)
                            ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(),
                                root.winfo_rootx()+root.winfo_width(), root.winfo_rooty()+root.winfo_height()
                                )).save(folder / "live-speed-check.png")
                            root.attributes("-topmost", False)
                        if "估算" in state["speed"] and "生成中" in state["phase"]:
                            phases.add("output")
                        if state["art"] and state["art"].startswith("artwork/"):
                            assert "估算" not in state["speed"] and "t/s" in state["speed"], state
                            assert state["phase"] == "输出 120 token", state
                            phases.add("complete")
                            break
                    assert phases == {"waiting", "reasoning", "output", "complete"}, phases
                    assert len(calls) == 1
                    print("PASS: one explicit local request; card + detail show waiting, live reasoning/output speed, final reported t/s and artwork")
                finally:
                    connection.close()

            with patch.object(api_tester, "PelicanStore", return_value=store), \
                    patch.object(api_tester, "PelicanPreview", side_effect=factory), \
                    patch.dict(os.environ, {"API_TESTER_PREVIEW_DEBUG_PORT": str(port)}):
                case.run_gui(settings, driver)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
