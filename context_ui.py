"""Native Tk context benchmark panel, with sequential, explicitly started requests."""
from __future__ import annotations

import csv
import json
import secrets
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from context_test import parse_lengths


class ContextPanel:
    def __init__(self, parent, *, colors, font, client_factory, models, source,
                 emit, busy, changed):
        self.parent, self.colors = parent, colors
        self.client_factory, self.models, self.source = client_factory, models, source
        self.emit, self.busy = emit, busy
        self.running = False
        self.stop = threading.Event()
        self.results = []
        self.vars = {key: tk.StringVar(value=value) for key, value in {
            "lengths": "8192,32768,131072", "mode": "接收长度 + 检索准确率",
            "max_tokens": "4096", "timeout": "180", "reasoning": "默认",
        }.items()}
        self.continue_var = tk.BooleanVar(value=False)
        controls = ttk.Frame(parent, style="Card.TFrame", padding=10)
        controls.pack(fill="x")
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="输入字符数", style="Sec.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.length_entry = ttk.Entry(controls, textvariable=self.vars["lengths"], width=28)
        self.length_entry.grid(row=0, column=1, sticky="ew")
        self.mode_box = ttk.Combobox(controls, textvariable=self.vars["mode"], state="readonly", width=24,
                                    values=("接收长度 + 检索准确率", "仅接收长度"))
        self.mode_box.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(controls, text="逗号分隔，按从小到大逐档测试；字符数不是 token，实际 token 以接口 usage 为准。",
                  style="Muted.TLabel", wraplength=600).grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 8))
        parameters = ttk.Frame(controls, style="Card.TFrame")
        parameters.grid(row=2, column=0, columnspan=3, sticky="w")
        self.inputs = [self.length_entry, self.mode_box]
        for key, label in (("max_tokens", "输出上限"), ("timeout", "超时(秒)"), ("reasoning", "推理强度")):
            ttk.Label(parameters, text=label, style="Muted.TLabel").pack(side="left", padx=(0, 5))
            if key == "reasoning":
                widget = ttk.Combobox(parameters, textvariable=self.vars[key], values=("默认", "低", "中", "高"),
                                      state="readonly", width=6)
            else:
                widget = ttk.Entry(parameters, textvariable=self.vars[key], width=8)
            widget.pack(side="left", padx=(0, 10))
            self.inputs.append(widget)
        self.continue_box = ttk.Checkbutton(controls, text="检索未全对也继续加长（接口失败则停止该模型）",
                                           variable=self.continue_var, command=changed)
        self.continue_box.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.inputs.append(self.continue_box)
        actions = ttk.Frame(controls, style="Card.TFrame")
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.start_button = ttk.Button(actions, text="开始上下文测试", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止后续请求", command=self.stop_requests, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(actions, text="导出上下文结果", command=self.export).pack(side="left")
        self.status = tk.StringVar(value="使用左侧选中模型与当前供应商；打开本页不会发送请求。")
        ttk.Label(controls, textvariable=self.status, style="Muted.TLabel", wraplength=600).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.summary = tk.StringVar(value="接收上界与检索准确率分别统计；未经测试的长度不作推断。")
        ttk.Label(parent, textvariable=self.summary, style="Muted.TLabel", wraplength=650,
                  padding=(10, 4)).pack(fill="x")
        body = ttk.Frame(parent, style="Card.TFrame")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=2)
        body.rowconfigure(1, weight=1)
        table = ttk.Frame(body, style="Card.TFrame")
        table.grid(row=0, column=0, sticky="nsew")
        columns = ("model", "chars", "tokens", "hits", "status", "seconds")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=7, selectmode="browse")
        for key, label, width in zip(columns, ("模型", "输入字符", "输入 token", "前/中/后", "结果", "耗时(秒)"),
                                     (155, 95, 95, 90, 145, 80)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=55, stretch=key in ("model", "status"))
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        detail_frame = ttk.Frame(body, style="Card.TFrame")
        detail_frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.detail = tk.Text(detail_frame, height=6, wrap="word", bg=colors["input"], fg=colors["fg"],
                              insertbackground=colors["acc"], selectbackground=colors["sel"],
                              font=font, relief="flat", padx=8, pady=8)
        detail_scroll = ttk.Scrollbar(detail_frame, command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.pack(side="right", fill="y")
        self.detail.pack(fill="both", expand=True)
        self.detail.insert("1.0", "选中结果可查看预期答案、模型回复、请求参数和错误。\n仅使用合成资料，不读取本地作品。")
        self.detail.configure(state="disabled")
        self.tree.bind("<<TreeviewSelect>>", self.show_detail)
        for var in self.vars.values():
            var.trace_add("write", changed)

    def options(self):
        return {**{key: var.get() for key, var in self.vars.items()}, "continue_on_miss": self.continue_var.get()}

    def load_options(self, options):
        if not isinstance(options, dict):
            return
        for key, var in self.vars.items():
            if isinstance(options.get(key), str):
                var.set(options[key])
        self.continue_var.set(options.get("continue_on_miss") is True)

    def set_external_busy(self, busy):
        self.start_button.configure(state="disabled" if busy or self.running else "normal")

    def start(self):
        if self.running:
            return
        try:
            options = self.options()
            lengths = parse_lengths(options["lengths"])
            output, timeout = int(options["max_tokens"]), int(options["timeout"])
            if output < 16 or timeout < 1:
                raise ValueError("输出上限须 ≥16，超时须为正整数")
            if options["reasoning"] not in ("默认", "低", "中", "高") or options["mode"] not in (
                    "接收长度 + 检索准确率", "仅接收长度"):
                raise ValueError("请选择有效的模式和推理强度")
            selected = list(self.models())
            if not 1 <= len(selected) <= 40:
                raise ValueError("请在左侧选择 1 至 40 个模型")
            client = self.client_factory()
            if client is None:
                return
            client.timeout = timeout
            reasoning = {"默认": "", "低": "low", "中": "medium", "高": "high"}[options["reasoning"]]
            # Validate protocol-specific thinking budgets before submitting.
            client.chat_body(selected[0], "validate", False, output, 0.0, False, reasoning)
            source = self.source()
        except (ValueError, TypeError) as exc:
            self.status.set(str(exc))
            return
        self.running = True
        self.last_error = ""
        self.stop.clear()
        self.stop_button.configure(state="normal")
        for widget in self.inputs:
            widget.configure(state="disabled")
        self.busy(True, "上下文测试中…")
        self.start_button.configure(state="disabled")
        retrieval = options["mode"] != "仅接收长度"
        seed = secrets.randbits(64)
        self.status.set(f"将依次测试 {len(selected)} 个模型、最多 {len(selected) * len(lengths)} 次请求；不自动重试。")

        def work():
            try:
                for model in selected:
                    for length in lengths:
                        if self.stop.is_set():
                            return
                        self.emit(("context_progress", f"{model} · {length:,} 字符 · 正在请求"))
                        result = client.test_context(model, length, retrieval=retrieval,
                                                     max_tokens=output, reasoning_effort=reasoning,
                                                     seed=seed + length, cancel_event=self.stop)
                        result.update(source)
                        result["created"] = datetime.now().astimezone().isoformat(timespec="seconds")
                        self.emit(("context_result", result))
                        if not result["accepted"]:
                            break
                        if retrieval and not options["continue_on_miss"] and (
                                not result["score"] or result["score"]["correct"] < result["score"]["total"]):
                            break
            except InterruptedError:
                pass
            except Exception as exc:
                self.emit(("context_error", f"测试异常：{type(exc).__name__}: {exc}"))
            finally:
                self.emit(("context_done", self.stop.is_set()))
                self.busy(False)
        threading.Thread(target=work, daemon=True, name="context-test").start()

    def stop_requests(self):
        self.stop.set()
        self.status.set("已停止排队；当前已发请求仍会等待返回或超时，不再发送下一次。")
        self.stop_button.configure(state="disabled")

    def handle(self, kind, value):
        if kind in ("context_progress", "context_error"):
            self.status.set(value)
            if kind == "context_error":
                self.last_error = value
        elif kind == "context_done":
            self.running = False
            self.stop_button.configure(state="disabled")
            self.start_button.configure(state="normal")
            for widget in self.inputs:
                widget.configure(state="readonly" if isinstance(widget, ttk.Combobox) else "normal")
            self.status.set(getattr(self, "last_error", "") or (
                ("已停止。" if value else "本次测试结束。") + " 可选中结果查看详情；测到的最大档位不代表模型理论上限。"))
        else:
            score = value["score"]
            hits = "/".join("✓" if hit else "×" for hit in score["hits"].values()) if score else "—"
            status = ("长度拒绝" if value["limit_rejected"] else "请求失败") if not value["accepted"] else (
                "已接收·输出异常" if value["error"] else "回复格式不符" if score and not score["valid_json"] else
                "检索全对" if score and score["correct"] == score["total"] else
                f"检索 {score['correct']}/{score['total']}" if score else "已接收（未测检索）")
            self.results.append(value)
            iid = str(len(self.results) - 1)
            self.tree.insert("", "end", iid=iid, values=(value["model"], value["characters"],
                value["input_tokens"] if value["input_tokens"] is not None else "未报告", hits, status, f"{value['elapsed']:.2f}"))
            self.tree.selection_set(iid)
            self.tree.see(iid)
            same = [r for r in self.results if (r["provider"], r["key_name"], r["model"]) ==
                    (value["provider"], value["key_name"], value["model"])]
            accepted = max((r["characters"] for r in same if r["accepted"]), default=0)
            passed = max((r["characters"] for r in same if r["score"] and r["score"]["correct"] == r["score"]["total"]), default=0)
            rejected = min((r["characters"] for r in same if r["limit_rejected"]), default=0)
            retrieval_summary = f" · 检索全对最大已测 {passed:,} 字符" if any(r["mode"] == "both" for r in same) else " · 尚未测试检索"
            self.summary.set(f"{value['model']} · 最大已接收 {accepted:,} 字符" + retrieval_summary
                             + (f" · 最小长度拒绝档位 {rejected:,} 字符。" if rejected else " · 尚未测得接收上限。"))

    def show_detail(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        record = self.results[int(selected[0])]
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", json.dumps(record, ensure_ascii=False, indent=2))
        self.detail.configure(state="disabled")

    def export(self):
        if not self.results:
            self.status.set("还没有可导出的上下文结果。")
            return
        filename = filedialog.asksaveasfilename(parent=self.parent, title="导出上下文测试结果",
            defaultextension=".json", initialfile="context-test-results.json",
            filetypes=(("JSON", "*.json"), ("CSV", "*.csv")))
        if not filename:
            return
        try:
            if filename.lower().endswith(".csv"):
                keys = ("provider", "key_name", "model", "mode", "characters", "input_tokens", "accepted", "score", "elapsed", "error")
                with Path(filename).open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=keys, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows({**r, "score": json.dumps(r["score"], ensure_ascii=False)} for r in self.results)
            else:
                Path(filename).write_text(json.dumps(self.results, ensure_ascii=False, indent=2), encoding="utf-8")
            self.status.set("上下文结果已导出。")
        except OSError as exc:
            self.status.set(f"导出失败：{exc}")
