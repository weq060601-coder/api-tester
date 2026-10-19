"""Regression checks: real mock HTTP, request isolation, preview sandbox and GUI persistence."""
import importlib
import gc
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import api_tester
from pelican import (ARTWORK_CSP, PELICAN_DEFAULT_MAX_TOKENS, PELICAN_PROMPT,
                     PELICAN_SETTINGS_VERSION, PelicanPreview, PelicanStore,
                     extract_artwork, migrate_legacy_files, run_pelican_test)

with patch("sys.argv", ["mock_server.py", "--quiet"]):
    mock_server = importlib.import_module("mock_server")


class PelicanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), mock_server.H)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_protocols_and_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PelicanStore(Path(directory))
            (Path(directory) / "other-model.html").write_text("SECRET_OTHER_MODEL_WORK", encoding="utf-8")
            for profile in ("default", "claude-code", "codex"):
                cli = api_tester.ApiClient(self.base, "sk-test", timeout=10, profile=profile)
                with patch.object(cli, "_build", wraps=cli._build) as build:
                    record = store.begin("mock-small", "Test", cli.protocol, PELICAN_DEFAULT_MAX_TOKENS)
                    outcome = run_pelican_test(cli, store, record)
                    self.assertEqual(outcome["status"], "complete", outcome)
                    path, body = build.call_args.args[:2]
                    limit_key = "max_output_tokens" if cli.protocol == "responses" else "max_tokens"
                    self.assertEqual(body[limit_key], 32768)
                    self.assertNotIn("images", path)
                    self.assertTrue(body["stream"])
                    self.assertNotIn("tools", body)
                    messages = body.get("messages", body.get("input"))
                    self.assertEqual(len(messages), 1)
                    self.assertEqual(messages[0]["role"], "user")
                    content = messages[0]["content"]
                    self.assertEqual(content if isinstance(content, str) else content[0]["text"], PELICAN_PROMPT)
                    self.assertNotIn("SECRET_OTHER_MODEL_WORK", json.dumps(body))
                    self.assertNotIn("image", json.dumps(body))
                # Existing vision and image generation remain independent and functional.
                vision = cli.test_chat("mock-vision", with_image=True)
                self.assertIn("看到图片", api_tester.extract_content(vision.data))
                self.assertTrue(cli.test_image("mock-small").ok)
            restored = PelicanStore(Path(directory))
            self.assertEqual(len(restored.list_runs()), 3)
            self.assertTrue(all(r["status"] == "complete" for r in restored.list_runs()))

    def test_errors_are_saved_without_fake_artwork(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PelicanStore(Path(directory))
            cli = api_tester.ApiClient(self.base, "invalid", timeout=3)
            record = store.begin("mock-small", "Test", "openai", 8192)
            result = run_pelican_test(cli, store, record)
            self.assertEqual(result["status"], "failed")
            self.assertIn("401", result["error"])
            self.assertFalse((Path(directory) / record["id"] / "artwork.html").exists())
            record = store.begin("mock-small", "Test", "openai", 8192)
            result = store.finish(record["id"], "<!doctype html><html><svg></svg>", 1)
            self.assertEqual(result["status"], "failed")
            self.assertIn("截断", result["error"])

    def test_collect_legacy_results_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "pelican_results" / "existing"
            new = root / "pelican" / "results" / "existing"
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            (old / "response.txt").write_text("original", encoding="utf-8")
            (new / "response.txt").write_text("keep", encoding="utf-8")
            (old / "prompt.txt").write_text("prompt", encoding="utf-8")
            example = root / "pelican.html"
            example.write_text("example", encoding="utf-8")
            migrate_legacy_files(root)
            migrate_legacy_files(root)
            self.assertEqual((new / "response.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual((old / "response.txt").read_text(encoding="utf-8"), "original")
            self.assertEqual((new / "prompt.txt").read_text(encoding="utf-8"), "prompt")
            self.assertEqual((root / "pelican/examples/pelican.html").read_text(encoding="utf-8"), "example")

    def test_extract_fences_svg_and_refusal(self):
        text = '```html\n<!doctype html><html><body><svg><circle r="5"/></svg></body></html>\n```'
        self.assertTrue(extract_artwork(text).startswith("<!doctype html>"))
        self.assertIn("<body><svg", extract_artwork('```svg\n<svg><rect width="20" height="20"/></svg>\n```'))
        for text in ("不能绘制", "<html></html>", "<html><svg>"):
            with self.assertRaises(ValueError):
                extract_artwork(text)

    def test_preview_routes_and_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PelicanStore(Path(directory))
            record = store.begin("mock-small", "Test", "openai", 8192)
            store.finish(record["id"], '<svg><circle r="5"/></svg>', 1)
            preview = PelicanPreview(store)
            try:
                with urllib.request.urlopen(preview.url) as response:
                    page = response.read().decode()
                    self.assertIn('sandbox title=', page)
                with urllib.request.urlopen(preview.url + "artwork/" + record["id"]) as response:
                    self.assertEqual(response.headers["Content-Security-Policy"], ARTWORK_CSP)
                    self.assertIn("sandbox", ARTWORK_CSP)
                    self.assertNotIn("allow-scripts", ARTWORK_CSP)
                for route in ("../settings.json", "settings.json", "raw/../../settings.json"):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(preview.url + route)
                    self.assertEqual(caught.exception.code, 404)
            finally:
                preview.close()

    def test_embedded_renderer_reuse_and_record_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PelicanStore(Path(directory))
            record = store.begin("mock-small", "Test", "openai", 8192)
            store.progress(record["id"], 4000)
            preview = PelicanPreview(store)
            try:
                with patch("pelican.benchmark.launch_embedded_preview") as launch:
                    from unittest.mock import Mock
                    process = Mock()
                    process.poll.return_value = None
                    ready_path = Path(directory) / "ready.json"
                    ready_path.write_text('{"handle": 123}', encoding="utf-8")
                    launch.return_value = process, ready_path
                    self.assertIs(preview.embed(456), process)
                    self.assertIs(preview.embed(456), process)
                    self.assertIs(preview.embed(456), process)
                    self.assertEqual(launch.call_count, 1)
                self.assertEqual(store.list_runs()[0]["characters"], 4000)
                request = urllib.request.Request(preview.url + "api/runs/" + record["id"], method="DELETE")
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.load(response)["removed"])
                store.finish(record["id"], '<svg><circle r="5"/></svg>', 1)
                self.assertEqual(store.list_runs(), [])
                self.assertEqual(PelicanStore(Path(directory)).list_runs(), [])
            finally:
                preview.close()

    def test_record_menu_open_actions_and_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PelicanStore(Path(directory))
            record = store.begin("mock-small", "Test", "openai", 32768)
            store.finish(record["id"], '<svg><circle r="5"/></svg>', 1)
            running = store.begin("mock-running", "Test", "openai", 32768)
            preview = PelicanPreview(store)
            def action(run_id, name, expected=200):
                request = urllib.request.Request(preview.url + "api/runs/" + run_id + "/open",
                    data=json.dumps({"action": name}).encode(),
                    headers={"Content-Type": "application/json"})
                if expected != 200:
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request)
                    self.assertEqual(caught.exception.code, expected)
                else:
                    with urllib.request.urlopen(request) as response:
                        self.assertTrue(json.load(response)["opened"])
            try:
                with patch("pelican.benchmark.webbrowser.open", return_value=True) as browser, \
                        patch("pelican.benchmark.subprocess.Popen") as explorer:
                    action(record["id"], "browser")
                    url = preview.url + "artwork/" + record["id"]
                    browser.assert_called_once_with(url, new=2)
                    with urllib.request.urlopen(url) as response:
                        self.assertEqual(response.headers["Content-Security-Policy"], ARTWORK_CSP)
                    action(record["id"], "explorer")
                    explorer.assert_called_with(["explorer.exe", "/select,",
                        str(Path(directory).resolve() / record["id"] / "artwork.html")])
                    action(running["id"], "explorer")
                    explorer.assert_called_with(["explorer.exe", "/select,",
                        str(Path(directory).resolve() / running["id"] / "run.json")])
                    browser.reset_mock()
                    explorer.reset_mock()
                    action(running["id"], "browser", 404)
                    action("0" * 32, "explorer", 404)
                    action(record["id"], "file:///settings.json", 400)
                    store.remove(record["id"])
                    action(record["id"], "explorer", 404)
                    action(record["id"], "browser", 404)
                    browser.assert_not_called()
                    explorer.assert_not_called()
            finally:
                preview.close()

    def test_reject_reasoning_draft_and_revalidate_old_blank(self):
        draft = '<svg><defs><linearGradient id="sky"><stop offset="0"/></linearGradient></defs>...</svg>'
        with self.assertRaisesRegex(ValueError, "没有实际图形"):
            extract_artwork("这是构思：```svg\n" + draft + "\n```后面还没有开始最终作答")
        with tempfile.TemporaryDirectory() as directory:
            store = PelicanStore(Path(directory))
            record = store.begin("model", "supplier", "responses", 8192)
            folder = Path(directory) / record["id"]
            record.update(status="complete", validation_version=1)
            (folder / "response.txt").write_text(draft, encoding="utf-8")
            (folder / "run.json").write_text(json.dumps(record), encoding="utf-8")
            restored = PelicanStore(Path(directory)).list_runs()[0]
            self.assertEqual(restored["status"], "failed")
            self.assertIn("没有实际图形", restored["error"])
            self.assertEqual((folder / "response.txt").read_text(encoding="utf-8"), draft)

    def test_stream_incomplete_reasons_and_analysis_channels(self):
        for event in (
            {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
        ):
            _pieces, done, error = api_tester.parse_stream_event(event)
            self.assertTrue(done)
            self.assertTrue(error)
        import io
        class Response(io.BytesIO):
            status = 200
        events = [
            {"type": "response.output_item.added", "item": {"id": "analysis", "type": "message", "channel": "analysis"}},
            {"type": "response.output_text.delta", "item_id": "analysis", "delta": "private draft"},
            {"type": "response.output_text.delta", "item_id": "final", "delta": "actual answer"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ]
        cli = api_tester.ApiClient("http://127.0.0.1:9/v1", protocol="responses")
        data = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
        with patch.object(cli.opener, "open", return_value=Response(data)):
            result = cli.request_stream("/responses", {})
            self.assertTrue(result.ok)
            self.assertEqual(result.text, "actual answer")
        with patch.object(cli.opener, "open", return_value=Response(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')):
            result = cli.request_stream("/responses", {})
            self.assertFalse(result.ok)
            self.assertEqual(result.text, "partial")
            self.assertIn("未收到结束事件", result.error)


class GuiPersistenceTests(unittest.TestCase):
    def run_gui(self, settings, driver):
        def loop(root, *_args):
            root.update()
            def raise_callback(_type, value, _traceback):
                raise value
            root.report_callback_exception = raise_callback
            try:
                driver(root)
            finally:
                root.tk.call(root.protocol("WM_DELETE_WINDOW"))
        with patch.object(api_tester, "SETTINGS_PATH", settings), patch.object(tk.Tk, "mainloop", loop):
            try:
                api_tester.build_gui()
            finally:
                # Collect destroyed Tk variables here, before another HTTP worker
                # can become the thread that triggers cyclic garbage collection.
                gc.collect()

    @staticmethod
    def widgets(root):
        result = []
        def visit(widget):
            result.append(widget)
            for child in widget.winfo_children():
                visit(child)
        visit(root)
        return result

    def row_widgets(self, root, label):
        item = next(w for w in self.widgets(root) if isinstance(w, ttk.Label) and w.cget("text") == label)
        grid_row = item.grid_info()["row"]
        frame = next(w for w in item.master.winfo_children()
                     if isinstance(w, ttk.Frame) and w.grid_info().get("row") == grid_row)
        return frame.winfo_children()

    def entry(self, root, label):
        return next(w for w in self.row_widgets(root, label) if isinstance(w, ttk.Entry))

    def combo(self, root, label):
        return next(w for w in self.row_widgets(root, label) if isinstance(w, ttk.Combobox))

    @staticmethod
    def set_entry(entry, value):
        entry.delete(0, "end")
        entry.insert(0, value)

    def press(self, root, text, answer=None):
        button = next(w for w in self.widgets(root) if isinstance(w, ttk.Button) and w.cget("text") == text)
        with patch("tkinter.simpledialog.askstring", return_value=answer):
            button.invoke()
        root.update()

    def select(self, root, label, value):
        combo = self.combo(root, label)
        combo.set(value)
        combo.event_generate("<<ComboboxSelected>>")
        root.update()

    def model_tree(self, root):
        return next(w for w in self.widgets(root) if isinstance(w, ttk.Treeview))

    def model_ids(self, root):
        tree = self.model_tree(root)
        return [tree.set(iid, "model") for iid in tree.get_children()]

    def add_model_ids(self, root, value):
        button = next(w for w in self.widgets(root) if isinstance(w, ttk.Button)
                      and w.cget("text") == "添加模型")
        entry = next(w for w in button.master.winfo_children() if type(w) is ttk.Entry)
        self.set_entry(entry, value)
        self.press(root, "添加模型")

    def model_save_toggle(self, root):
        return next(w for w in self.widgets(root) if isinstance(w, ttk.Checkbutton)
                    and w.cget("text") == "保存模型列表")

    def test_shared_model_management_optional_save_and_supplier_restart(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(api_tester.ApiClient, "list_models") as fetch, \
                patch.object(api_tester.ApiClient, "test_chat") as chat:
            settings = Path(directory) / "settings.json"
            def saved():
                return json.loads(settings.read_text(encoding="utf-8"))
            def create(root):
                self.set_entry(self.entry(root, "Base URL"), "http://supplier-a.test/v1")
                self.add_model_ids(root, "a-one，a-two;a-one\n a-three")
                self.assertEqual(self.model_ids(root), ["a-one", "a-two", "a-three"])
                self.add_model_ids(root, "a-two,a-three")
                tree = self.model_tree(root)
                self.assertEqual([tree.set(i, "model") for i in tree.selection()], ["a-two", "a-three"])
                self.assertEqual(len(self.model_ids(root)), 3)
                self.press(root, "保存供应商", "A")
                self.set_entry(self.entry(root, "Base URL"), "http://supplier-b.test/v1")
                self.assertEqual(self.model_ids(root), [])
                self.add_model_ids(root, "b-one,b-two")
                self.press(root, "保存供应商", "B")
                self.model_save_toggle(root).invoke()
                root.update()
                self.assertEqual(self.model_ids(root), ["b-one", "b-two"])
                self.assertFalse(saved()["save_models"])
                self.assertEqual(saved()["models"], [])
                self.assertEqual(saved()["providers"]["B"]["models"], [])
                self.assertEqual(saved()["providers"]["B"]["results"], {})
                self.select(root, "供应商档案", "A")
                self.assertTrue(self.model_save_toggle(root).instate(["selected"]))
                self.assertEqual(self.model_ids(root), ["a-one", "a-two", "a-three"])
                tree.selection_set(tree.get_children()[1:])
                self.press(root, "删除选中模型")
                self.assertEqual(self.model_ids(root), ["a-one"])
                self.assertEqual(saved()["providers"]["A"]["models"], ["a-one"])
                # Unsaved B models remain usable across switches in this session.
                self.select(root, "供应商档案", "B")
                self.assertEqual(self.model_ids(root), ["b-one", "b-two"])
                self.assertFalse(self.model_save_toggle(root).instate(["selected"]))
                self.select(root, "供应商档案", "A")
            self.run_gui(settings, create)
            def restart(root):
                self.assertEqual(self.model_ids(root), ["a-one"])
                self.select(root, "供应商档案", "B")
                self.assertEqual(self.model_ids(root), [])
                self.assertFalse(self.model_save_toggle(root).instate(["selected"]))
                self.add_model_ids(root, "b-new")
                self.assertEqual(saved()["models"], [])
                self.model_save_toggle(root).invoke()
                self.assertEqual(saved()["models"], ["b-new"])
            self.run_gui(settings, restart)
            def saved_again(root):
                self.assertEqual(self.model_ids(root), ["b-new"])
                self.select(root, "供应商档案", "A")
                self.assertEqual(self.model_ids(root), ["a-one"])
            self.run_gui(settings, saved_again)
            fetch.assert_not_called()
            chat.assert_not_called()

    def test_models_are_bound_to_named_key_within_one_supplier(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"

            def create(root):
                self.set_entry(self.entry(root, "Base URL"), "http://same-provider.test/v1")
                self.set_entry(self.entry(root, "API Key"), "key-a")
                self.press(root, "保存供应商", "同一供应商")
                self.press(root, "保存/新增 Key", "组 A")
                self.add_model_ids(root, "model-a1,model-a2")
                self.set_entry(self.entry(root, "API Key"), "key-b")
                self.assertEqual(self.model_ids(root), [],
                                 "手动改 Key 后不能继续显示旧 Key 的模型")
                self.press(root, "保存/新增 Key", "组 B")
                self.add_model_ids(root, "model-b1")
                self.select(root, "命名 Key", "组 A")
                self.assertEqual(self.model_ids(root), ["model-a1", "model-a2"])
                self.select(root, "命名 Key", "组 B")
                self.assertEqual(self.model_ids(root), ["model-b1"])
                saved = json.loads(settings.read_text(encoding="utf-8"))
                slots = saved["providers"]["同一供应商"]["key_models"]
                self.assertEqual(slots["组 A"]["models"], ["model-a1", "model-a2"])
                self.assertEqual(slots["组 B"]["models"], ["model-b1"])

            self.run_gui(settings, create)

            def restored(root):
                self.assertEqual(self.model_ids(root), ["model-b1"])
                self.select(root, "命名 Key", "组 A")
                self.assertEqual(self.model_ids(root), ["model-a1", "model-a2"])
                self.select(root, "命名 Key", "组 B")
                self.assertEqual(self.model_ids(root), ["model-b1"])

            self.run_gui(settings, restored)

    def test_fetch_merges_manual_ids_and_restores_without_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({"base_url": "http://mock.test/v1", "api_key": "sk-test"}),
                                encoding="utf-8")
            reply = api_tester.ApiResult(True, 200, data={"data": []})
            with patch.object(api_tester.ApiClient, "list_models", return_value=(["fetched", "manual"], reply)) as fetch:
                def driver(root):
                    self.add_model_ids(root, "manual")
                    self.press(root, "获取模型")
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        root.update()
                        if "fetched" in self.model_ids(root):
                            break
                        time.sleep(0.02)
                    self.assertEqual(self.model_ids(root), ["manual", "fetched"])
                    tree = self.model_tree(root)
                    self.assertEqual([tree.set(i, "model") for i in tree.selection()], ["manual"])
                self.run_gui(settings, driver)
                fetch.assert_called_once()
            with patch.object(api_tester.ApiClient, "list_models") as fetch:
                self.run_gui(settings, lambda root: self.assertEqual(self.model_ids(root), ["manual", "fetched"]))
                fetch.assert_not_called()

    def test_model_ids_can_be_copied_from_toolbar_and_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({"models": ["copy-one", "copy-two"]}), encoding="utf-8")
            def driver(root):
                tree = self.model_tree(root)
                tree.selection_set(tree.get_children())
                button = next(w for w in self.widgets(root) if isinstance(w, ttk.Button)
                              and w.cget("text") == "复制 ID")
                button.invoke()
                self.assertEqual(root.clipboard_get(), "copy-one\ncopy-two")
            self.run_gui(settings, driver)

    def test_opt_out_removes_existing_saved_results_and_delete_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            cfg = {"base_url": "http://mock.test/v1", "models": ["one", "two"],
                   "results": {"one": ["1 ms", "✔ 通"], "two": ["2 ms", "✔ 通"]}}
            settings.write_text(json.dumps({**cfg, "providers": {"A": cfg}, "active_provider": "A"}),
                                encoding="utf-8")
            def driver(root):
                self.model_save_toggle(root).invoke()
                data = json.loads(settings.read_text(encoding="utf-8"))
                self.assertEqual(data["models"], [])
                self.assertEqual(data["results"], {})
                self.assertEqual(data["providers"]["A"]["models"], [])
                self.assertEqual(data["providers"]["A"]["results"], {})
                self.assertEqual(self.model_ids(root), ["one", "two"])
                self.model_save_toggle(root).invoke()
                tree = self.model_tree(root)
                tree.selection_set(tree.get_children()[0])
                self.press(root, "删除选中模型")
                data = json.loads(settings.read_text(encoding="utf-8"))
                self.assertEqual(data["models"], ["two"])
                self.assertEqual(data["results"], {"two": ["2 ms", "✔ 通"]})
                self.assertEqual(data["providers"]["A"]["results"], data["results"])
            self.run_gui(settings, driver)
            self.run_gui(settings, lambda root: self.assertEqual(self.model_ids(root), ["two"]))

    def test_named_keys_supplier_switch_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            def create(root):
                self.set_entry(self.entry(root, "Base URL"), "http://supplier-a.test/v1")
                self.set_entry(self.entry(root, "API Key"), "test-a1")
                self.press(root, "保存供应商", "供应商 A")
                self.press(root, "保存/新增 Key", "主 Key")
                self.set_entry(self.entry(root, "API Key"), "test-a2")
                self.press(root, "保存/新增 Key", "备用 Key")
                self.press(root, "重命名", "备用改名")
                self.set_entry(self.entry(root, "Base URL"), "http://supplier-b.test/v1")
                self.assertEqual(self.entry(root, "API Key").get(), "")
                self.set_entry(self.entry(root, "API Key"), "test-b1")
                self.press(root, "保存供应商", "供应商 B")
                self.press(root, "保存/新增 Key", "主 Key")
                self.select(root, "供应商档案", "供应商 A")
                self.assertEqual(self.entry(root, "Base URL").get(), "http://supplier-a.test/v1")
                self.assertEqual(self.entry(root, "API Key").get(), "test-a2")
                self.assertEqual(set(self.combo(root, "命名 Key").cget("values")), {"主 Key", "备用改名"})
                self.select(root, "命名 Key", "主 Key")
                self.assertEqual(self.entry(root, "API Key").get(), "test-a1")
                self.select(root, "供应商档案", "供应商 B")
                self.assertEqual(self.entry(root, "API Key").get(), "test-b1")
                self.assertEqual(tuple(self.combo(root, "命名 Key").cget("values")), ("主 Key",))
            self.run_gui(settings, create)
            def restored(root):
                self.assertEqual(self.combo(root, "供应商档案").get(), "供应商 B")
                self.assertEqual(self.entry(root, "API Key").get(), "test-b1")
                self.select(root, "供应商档案", "供应商 A")
                self.assertEqual(self.entry(root, "API Key").get(), "test-a1")
            self.run_gui(settings, restored)
            saved = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(saved["providers"]["供应商 A"]["named_keys"]["备用改名"], "test-a2")

    def test_legacy_settings_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({"base_url": "http://legacy.test/v1", "api_key": "legacy-value",
                "providers": {"Legacy": {"base_url": "http://legacy.test/v1"}}, "active_provider": "Legacy",
                "named_keys": {"旧 Key": "legacy-value"}, "active_key": "旧 Key"}), encoding="utf-8")
            def check(root):
                self.assertEqual(self.entry(root, "API Key").get(), "legacy-value")
                self.assertEqual(self.combo(root, "命名 Key").get(), "旧 Key")
            self.run_gui(settings, check)
            saved = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(saved["providers"]["Legacy"]["named_keys"]["旧 Key"], "legacy-value")

    def test_pelican_saved_output_limit_is_preserved_on_restart(self):
        cases = [({}, "32768"),
                 ({"pelican_max_tokens": "8192"}, "8192"),
                 ({"pelican_max_tokens": "16384"}, "16384"),
                 ({"pelican_max_tokens": "8192", "pelican_settings_version": 1}, "8192")]
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            for data, expected in cases:
                with self.subTest(settings=data):
                    settings.write_text(json.dumps(data), encoding="utf-8")
                    def check(root):
                        self.assertEqual(self.entry(root, "鹈鹕测试").get(), expected)
                    self.run_gui(settings, check)
                    saved = json.loads(settings.read_text(encoding="utf-8"))
                    self.assertEqual(saved["pelican_max_tokens"], expected)
                    self.assertEqual(saved["pelican_settings_version"], PELICAN_SETTINGS_VERSION)
                    self.run_gui(settings, check)

    def test_all_five_manual_parameters_autosave_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            expected = {"timeout": "95", "max_tokens": "2048", "pelican_max_tokens": "65536",
                        "pelican_reasoning": "高", "pelican_timeout": "720"}
            def change(root):
                self.set_entry(self.entry(root, "超时 / 代理"), "95")
                prompt_entries = [w for w in self.row_widgets(root, "测试提示词") if isinstance(w, ttk.Entry)]
                self.set_entry(prompt_entries[-1], "2048")
                entries = [w for w in self.row_widgets(root, "鹈鹕测试") if type(w) is ttk.Entry]
                self.set_entry(entries[0], "65536")
                self.set_entry(entries[1], "720")
                self.select(root, "鹈鹕测试", "高")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    root.update()
                    if settings.exists():
                        saved = json.loads(settings.read_text(encoding="utf-8"))
                        if all(saved.get(k) == v for k, v in expected.items()):
                            return
                    time.sleep(0.02)
                self.fail("Manual parameters did not autosave before closing")
            self.run_gui(settings, change)
            def restored(root):
                self.assertEqual(self.entry(root, "超时 / 代理").get(), "95")
                entries = [w for w in self.row_widgets(root, "测试提示词") if isinstance(w, ttk.Entry)]
                self.assertEqual(entries[-1].get(), "2048")
                entries = [w for w in self.row_widgets(root, "鹈鹕测试") if type(w) is ttk.Entry]
                self.assertEqual([w.get() for w in entries], ["65536", "720"])
                self.assertEqual(self.combo(root, "鹈鹕测试").get(), "高")
            self.run_gui(settings, restored)

    def test_gui_multimodel_pelican_action(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), mock_server.H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                settings = Path(directory) / "settings.json"
                results = Path(directory) / "results"
                settings.write_text(json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "api_key": "sk-test",
                    "pelican_max_tokens": "8192"}), encoding="utf-8")
                opened = []
                requested_parameters = []
                original_chat = api_tester.ApiClient.test_chat

                def capture_chat(client, *args, **kwargs):
                    requested_parameters.append((client.timeout, kwargs.get("max_tokens"),
                                                 kwargs.get("reasoning_effort")))
                    return original_chat(client, *args, **kwargs)

                class Process:
                    def wait(self, timeout=None):
                        return 0
                    def poll(self):
                        return None
                    def terminate(self):
                        pass

                def open_window(preview, parent_handle):
                    opened.append(preview)
                    preview.native_handle = 1
                    process = Process()
                    preview.processes = [process]
                    return process

                def driver(root):
                    self.add_model_ids(root, "mock-small,mock-large")
                    tree = next(w for w in self.widgets(root) if isinstance(w, ttk.Treeview))
                    tree.selection_set(tree.get_children())
                    self.press(root, "鹈鹕测试")
                    opening_deadline = time.monotonic() + 3
                    while not opened and time.monotonic() < opening_deadline:
                        root.update()
                        time.sleep(0.01)
                    self.assertEqual(len(opened), 1)
                    preview = opened[0]
                    # Opening the page with models selected must not generate.
                    self.press(root, "鹈鹕测试")
                    self.press(root, "查看作品")
                    self.assertEqual(requested_parameters, [])
                    self.assertEqual(preview.store.list_runs(), [])
                    with urllib.request.urlopen(preview.url + "api/context") as response:
                        public = response.read().decode()
                        context = json.loads(public)
                        self.assertNotIn("sk-test", public)
                        self.assertEqual(context["selected_models"], ["mock-small", "mock-large"])
                        self.assertEqual(context["pelican_tokens"], "8192")
                    # The embedded page must persist edits without generating.
                    reply = {}
                    done = threading.Event()
                    def save_from_page():
                        try:
                            request = urllib.request.Request(preview.url + "api/settings",
                                data=json.dumps({"max_tokens": "49152", "reasoning": "高",
                                                 "timeout": "420"}).encode(),
                                headers={"Content-Type": "application/json"})
                            with urllib.request.urlopen(request, timeout=8) as response:
                                reply.update(json.load(response))
                        except Exception as exc:
                            reply["error"] = str(exc)
                        finally:
                            done.set()
                    threading.Thread(target=save_from_page, daemon=True).start()
                    deadline = time.monotonic() + 9
                    while not done.is_set() and time.monotonic() < deadline:
                        root.update()
                        time.sleep(0.02)
                    self.assertTrue(reply.get("saved"), reply)
                    self.assertEqual(preview.store.list_runs(), [])
                    self.assertEqual(self.entry(root, "鹈鹕测试").get(), "49152")
                    self.assertEqual(self.combo(root, "鹈鹕测试").get(), "高")
                    saved = json.loads(settings.read_text(encoding="utf-8"))
                    self.assertEqual(saved["pelican_max_tokens"], "49152")
                    self.assertEqual(saved["pelican_reasoning"], "高")
                    self.assertEqual(saved["pelican_timeout"], "420")
                    self.press(root, "鹈鹕测试")
                    self.assertEqual(preview.store.list_runs(), [])
                    self.assertEqual(requested_parameters, [])
                    self.assertEqual(len(opened), 1)
                    # Only the embedded Generate button's endpoint starts jobs.
                    generated, generation_done = {}, threading.Event()
                    def generate_from_page():
                        try:
                            request = urllib.request.Request(preview.url + "api/generate",
                                data=json.dumps({"provider": context["selected_provider"],
                                                 "models": context["selected_models"],
                                                 "max_tokens": "49152", "reasoning": "高",
                                                 "timeout": "420"}).encode(),
                                headers={"Content-Type": "application/json"})
                            with urllib.request.urlopen(request, timeout=8) as response:
                                generated.update(json.load(response))
                        except Exception as exc:
                            generated["error"] = str(exc)
                        finally:
                            generation_done.set()
                    threading.Thread(target=generate_from_page, daemon=True).start()
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        root.update()
                        statuses = [tree.item(row, "values")[2] for row in tree.get_children()]
                        if (generation_done.is_set() and statuses
                                and all("鹈鹕已生成" in status for status in statuses)):
                            break
                        time.sleep(0.03)
                    self.assertEqual(len(opened), 1)
                    self.assertEqual(len(generated.get("ids", [])), 2, generated)
                    self.assertTrue(all("鹈鹕已生成" in status for status in statuses), statuses)
                    stored = PelicanStore(results).list_runs()
                    self.assertEqual(len(stored), 2)
                    self.assertEqual({r["model"] for r in stored}, {"mock-small", "mock-large"})
                    self.assertTrue(all(r["max_tokens"] == 49152 for r in stored))
                    self.assertTrue(all(r["reasoning_effort"] == "high" for r in stored))

                with patch.object(api_tester, "PELICAN_RESULTS_PATH", results), \
                        patch.object(PelicanPreview, "embed", open_window), \
                        patch.object(api_tester.ApiClient, "test_chat", capture_chat):
                    self.run_gui(settings, driver)
                self.assertEqual(requested_parameters, [(420, 49152, "high")] * 2)
                def check_page_restart(root):
                    entries = [w for w in self.row_widgets(root, "鹈鹕测试") if type(w) is ttk.Entry]
                    self.assertEqual([w.get() for w in entries], ["49152", "420"])
                    self.assertEqual(self.combo(root, "鹈鹕测试").get(), "高")
                self.run_gui(settings, check_page_restart)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
