"""Opt-in smoke check of the real Tk app with an embedded WebView2 preview, using mock files only."""
import ctypes
from ctypes import wintypes
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

import api_tester
from pelican import PELICAN_DEFAULT_MAX_TOKENS, PelicanPreview, PelicanStore
from test_api_tester import GuiPersistenceTests, mock_server


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = PelicanStore(Path(directory) / "results")
        requested = set()
        original_read = store.read

        def read(run_id, filename):
            data = original_read(run_id, filename)
            if filename == "artwork.html":
                requested.add(run_id)
            return data

        store.read = read
        for model in ("mock-small", "mock-large"):
            record = store.begin(model, "本地 MOCK", "openai", PELICAN_DEFAULT_MAX_TOKENS)
            store.finish(record["id"], mock_server.MOCK_PELICAN.format(
                model=model, color="#ef4444" if model == "mock-small" else "#3b82f6"), 0.2)
        settings = Path(directory) / "settings.json"
        import json
        settings.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-test",
                                       "models": ["mock-small", "mock-large"],
                                       "pelican_max_tokens": "8192"}), encoding="utf-8")
        previews = []

        def factory(*args, **kwargs):
            preview = PelicanPreview(*args, **kwargs)
            previews.append(preview)
            return preview

        case = GuiPersistenceTests()
        def driver(root):
            assert case.entry(root, "鹈鹕测试").get() == "8192", "Saved manual output limit must be preserved"
            case.press(root, "查看作品")
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                root.update()
                if previews and previews[0].native_handle and len(requested) == 2:
                    break
                time.sleep(0.05)
            assert previews and previews[0].native_handle, "Renderer never attached; see pelican/diagnostics/preview-errors.log"
            preview = previews[0]
            assert len(requested) == 2, "Both model thumbnails must render"
            case.press(root, "查看作品")
            assert len(preview.processes) == 1, "Repeated opens must reuse the embedded renderer"
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            parent = user32.GetParent(preview.native_handle)
            widget_handles = {w.winfo_id() for w in case.widgets(root)}
            assert parent in widget_handles, "Renderer must be a child of the actual app notebook"
            visible_windows = []
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            @callback_type
            def collect(hwnd, _param):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == preview.processes[0].pid and user32.IsWindowVisible(hwnd):
                    visible_windows.append(hwnd)
                return True

            user32.EnumWindows(collect, 0)
            assert not visible_windows, "Renderer must not create a visible top-level window"
            case.press(root, "展开设置")
            root.geometry("1280x1000")
            for _ in range(8):
                root.update()
                time.sleep(0.05)
            try:
                from PIL import ImageGrab
                # The desktop may currently show another application. Briefly
                # raise our test window so the evidence captures this app.
                root.attributes("-topmost", True)
                root.lift()
                for _ in range(6):
                    root.update()
                    time.sleep(0.05)
                try:
                    ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(),
                                        root.winfo_rootx() + root.winfo_width(),
                                        root.winfo_rooty() + root.winfo_height())).save(
                                            str(Path(__file__).parent / "pelican" / "diagnostics" / "native-preview-check.png"))
                finally:
                    root.attributes("-topmost", False)
            except ImportError:
                pass
            print("PASS: saved settings are preserved; two mock artworks render inside the app; no extra top-level window")

        with patch.object(api_tester, "PelicanStore", return_value=store), \
                patch.object(api_tester, "PelicanPreview", side_effect=factory):
            case.run_gui(settings, driver)


if __name__ == "__main__":
    main()







