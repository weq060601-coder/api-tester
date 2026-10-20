"""Opt-in WebView2 resource check using temporary mock records, never saved credentials.

Requires psutil and websocket-client in the test Python environment. The app
itself retains its existing dependencies. CDP is enabled only for this test.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request
from unittest.mock import patch

import psutil
import websocket

import api_tester
import pelican.benchmark as benchmark
from test_api_tester import GuiPersistenceTests, mock_server


def check(baseline=False, count=40):
    case = GuiPersistenceTests()
    measurements = {}
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        settings = directory / "settings.json"
        settings.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1",
                                        "api_key": "sk-test"}), encoding="utf-8")
        store = benchmark.PelicanStore(directory / "results")
        for index in range(count):
            record = store.begin(f"mock-{index}", "MOCK", "openai", 32768)
            store.finish(record["id"], mock_server.MOCK_PELICAN.format(
                model=f"mock-{index}", color="#ef4444"), 0.1)
        page = benchmark.PREVIEW_PATH
        renderer = benchmark.RENDERER_PATH
        if baseline:
            page = directory / "baseline.html"
            page.write_bytes(subprocess.check_output(
                ["git", "show", "4a10502:pelican/preview.html"]))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if baseline:
            renderer = directory / "baseline_renderer.py"
            source = subprocess.check_output(
                ["git", "show", "4a10502:pelican/preview.py"]).decode("utf-8")
            source = source.replace("    window = webview.create_window(",
                f'    webview.settings["REMOTE_DEBUGGING_PORT"] = {port}\n\n'
                "    window = webview.create_window(", 1)
            renderer.write_text(source, encoding="utf-8")
        previews = []

        def factory(*args, **kwargs):
            preview = benchmark.PelicanPreview(*args, **kwargs)
            previews.append(preview)
            return preview

        def driver(root):
            root.geometry("1280x900")

            def pump(seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)

            case.press(root, "查看作品")
            connection = None
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                pump(0.1)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as response:
                        targets = json.load(response)
                    target = next(t for t in targets if t.get("type") == "page"
                                  and previews[0].url in t.get("url", ""))
                    connection = websocket.create_connection(target["webSocketDebuggerUrl"],
                                                             timeout=5, suppress_origin=True)
                    break
                except (OSError, StopIteration, IndexError):
                    pass
            assert connection, "Test-only CDP endpoint did not start"
            command = 0

            def evaluate(expression):
                nonlocal command
                command += 1
                connection.send(json.dumps({"id": command, "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True}}))
                while True:
                    reply = json.loads(connection.recv())
                    if reply.get("id") == command:
                        assert "exceptionDetails" not in reply.get("result", {}), reply
                        return reply.get("result", {}).get("result", {}).get("value")

            def resources():
                parent = psutil.Process(previews[0].processes[0].pid)
                before = {}
                for process in [parent, *parent.children(recursive=True)]:
                    try:
                        before[process.pid] = sum(process.cpu_times()[:2])
                    except psutil.Error:
                        pass
                started = time.monotonic()
                pump(3)
                cpu = 0
                memory = 0
                private = 0
                details = []
                processes = [parent, *parent.children(recursive=True)]
                for process in processes:
                    try:
                        memory += process.memory_info().rss
                        private += process.memory_full_info().uss
                        details.append({"name": process.name(),
                            "working_set_mb": round(process.memory_info().rss / 1024**2, 1),
                            "private_working_set_mb": round(process.memory_full_info().uss / 1024**2, 1)})
                        if process.pid in before:
                            cpu += max(0, sum(process.cpu_times()[:2]) - before[process.pid])
                    except psutil.Error:
                        pass
                return {"cpu_percent_of_machine": round(100 * cpu / (time.monotonic() - started)
                                                        / psutil.cpu_count(), 2),
                        "working_set_mb": round(memory / 1024**2, 1),
                        "private_working_set_mb": round(private / 1024**2, 1),
                        "processes": len(processes),
                        "details": details,
                        "loaded_artwork_frames": evaluate("[...document.querySelectorAll('iframe')].filter(f=>f.getAttribute('src')&&!f.src.endsWith('about:blank')).length"),
                        "thumbnail_frames": evaluate("document.querySelectorAll('.thumb iframe').length")}

            try:
                pump(2)
                history_height = evaluate("document.getElementById('records').scrollHeight")
                # Scroll through the history: lazy frames must be released,
                # rather than accumulate after a user has browsed the list.
                for index in range(12):
                    evaluate(f"document.getElementById('records').scrollTop={index}*500")
                    pump(0.15)
                    if not baseline:
                        assert evaluate("document.querySelectorAll('.thumb iframe').length") <= 4
                        assert evaluate("document.getElementById('records').scrollHeight") == history_height, "Thumbnails changed history layout"
                measurements["visible"] = resources()
                if not baseline:
                    evaluate("document.getElementById('source').click()")
                    pump(1)
                    assert evaluate("document.getElementById('art').getAttribute('src')") == "about:blank"
                    measurements["raw"] = resources()
                    evaluate("document.getElementById('animation').click()")
                notebook = next(w for w in case.widgets(root)
                                if w.winfo_class() == "TNotebook")
                notebook.select(0)
                pump(2)
                measurements["hidden"] = resources()
                if not baseline:
                    assert measurements["visible"]["loaded_artwork_frames"] <= 5, measurements
                    assert measurements["hidden"]["loaded_artwork_frames"] == 0, measurements
                    notebook.select(2)
                    pump(3)
                    assert evaluate("document.getElementById('art').src.includes('/artwork/')"), "Preview did not resume"
                    assert evaluate("document.querySelectorAll('.thumb iframe').length") <= 4
                    root.iconify()
                    pump(2)
                    assert evaluate("[...document.querySelectorAll('iframe')].filter(f=>f.getAttribute('src')&&!f.src.endsWith('about:blank')).length") == 0
                    root.deiconify()
                    pump(3)
                    assert evaluate("document.getElementById('art').src.includes('/artwork/')"), "Minimized preview did not resume"
            finally:
                connection.close()

        with patch.dict(os.environ, {"API_TESTER_PREVIEW_DEBUG_PORT": str(port)}), \
                patch.object(api_tester, "PelicanStore", return_value=store), \
                patch.object(api_tester, "PelicanPreview", side_effect=factory), \
                patch.object(benchmark, "PREVIEW_PATH", page), \
                patch.object(benchmark, "RENDERER_PATH", renderer):
            case.run_gui(settings, driver)
    return measurements


if __name__ == "__main__":
    results = {"records": 40, "baseline": check(True), "updated": check(False)}
    print(json.dumps(results, indent=2))
    destination = Path(__file__).parent / "pelican" / "diagnostics" / "performance-check.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(results, indent=2), encoding="utf-8")
