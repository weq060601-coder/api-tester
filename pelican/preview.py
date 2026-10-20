"""WebView2 renderer embedded in the Tk pelican tab.

The renderer is a single child HWND owned by the preview host.  All geometry
changes are made with Win32 SetWindowPos in the renderer process; WinForms
Form.Location/Size are deliberately avoided because they use a different
coordinate system after reparenting.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import sys
import time
import webview


GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_CLIPSIBLINGS = 0x04000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_FRAMECHANGED = 0x0020
SW_SHOW = 5
SW_HIDE = 0
HWND_TOP = 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("内嵌预览当前需要 Windows WebView2")
    if not args.parent:
        raise SystemExit("缺少 Tk 预览宿主句柄")

    # Opt-in local diagnostic endpoint; never enabled by normal app launches.
    debug_port = os.environ.get("API_TESTER_PREVIEW_DEBUG_PORT")
    if debug_port:
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(debug_port)

    window = webview.create_window(
        "鹈鹕测试",
        args.url,
        width=640,
        height=480,
        min_size=(1, 1),
        hidden=True,
        frameless=True,
        shadow=False,
        background_color="#0d1117",
        text_select=True,
    )
    user32 = ctypes.windll.user32
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style = user32.GetWindowLongPtrW
    set_style = user32.SetWindowLongPtrW
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style.restype = ctypes.c_ssize_t
    set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_style.restype = ctypes.c_ssize_t

    def client_size() -> tuple[int, int]:
        rect = wintypes.RECT()
        if not user32.GetClientRect(args.parent, ctypes.byref(rect)):
            return 1, 1
        return max(1, rect.right - rect.left), max(1, rect.bottom - rect.top)

    def place(handle: int) -> None:
        width, height = client_size()
        user32.SetWindowPos(handle, HWND_TOP, 0, 0, width, height,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED)

    def attach_on_ui_thread() -> None:
        form = window.native
        handle = int(form.Handle.ToInt64())
        style = get_style(handle, GWL_STYLE)
        style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX |
                   WS_MAXIMIZEBOX | WS_SYSMENU)
        style |= WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS
        set_style(handle, GWL_STYLE, style)
        ex_style = get_style(handle, GWL_EXSTYLE)
        ex_style &= ~WS_EX_APPWINDOW
        ex_style |= WS_EX_TOOLWINDOW
        set_style(handle, GWL_EXSTYLE, ex_style)
        user32.SetParent(handle, args.parent)
        if user32.GetParent(handle) != args.parent:
            raise RuntimeError("预览渲染器父窗口校验失败")
        place(handle)
        user32.ShowWindow(handle, SW_SHOW)
        args.ready.write_text(json.dumps({"handle": handle}), encoding="utf-8")

        # The Tk frame can resize independently of the WebView process. Keep
        # only the child rectangle synchronized; no Tk widget is moved.
        from System import Action
        from System.Windows.Forms import Timer
        timer = Timer()
        timer.Interval = 80
        last = [0, 0]
        browser = form.browser.webview
        hidden_at = None
        suspend_task = None
        suspend_attempted = False

        def maintain(_sender, _event) -> None:
            nonlocal hidden_at, suspend_task, suspend_attempted
            if not user32.IsWindow(args.parent):
                timer.Stop()
                user32.ShowWindow(handle, SW_HIDE)
                form.Close()
                return
            visible = (user32.IsWindowVisible(args.parent)
                       and not user32.IsIconic(user32.GetAncestor(args.parent, 2)))
            if not visible:
                if suspend_task is not None and suspend_task.IsCompleted:
                    try:
                        if not suspend_task.Result:
                            logging.warning("WebView2 declined suspension; artwork remains unloaded")
                    except Exception:
                        logging.exception("WebView2 suspension failed; artwork remains unloaded")
                    suspend_task = None
                if hidden_at is None:
                    hidden_at = time.monotonic()
                    # Let visibilitychange unload artwork before suspending JS.
                    browser.Visible = False
                if not suspend_attempted and time.monotonic() - hidden_at >= 0.5:
                    suspend_attempted = True
                    try:
                        suspend_task = browser.CoreWebView2.TrySuspendAsync()
                    except Exception:
                        logging.exception("WebView2 suspend unavailable; artwork remains unloaded")
                return
            if hidden_at is not None:
                try:
                    # Resume also cancels a pending TrySuspendAsync operation.
                    browser.CoreWebView2.Resume()
                except Exception:
                    logging.exception("WebView2 resume failed")
                browser.Visible = True
                hidden_at = None
                suspend_task = None
                suspend_attempted = False
            width, height = client_size()
            if last != [width, height]:
                last[:] = [width, height]
                place(handle)

        timer.Tick += maintain
        timer.Start()
        window._embed_timer = timer

    def on_start() -> None:
        if not window.events.loaded.wait(30):
            raise RuntimeError("WebView2 页面加载超时")
        from System import Action
        window.native.Invoke(Action(attach_on_ui_thread))

    webview.start(on_start, gui="edgechromium", private_mode=True, debug=False)


if __name__ == "__main__":
    main()



