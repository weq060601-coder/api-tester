"""Native, passive view of measured token throughput; never sends requests."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class SpeedView:
    def __init__(self, parent, *, colors, font):
        self.parent, self.rows = parent, {}
        self.colors, self.font = colors, font
        self.sequence = 0
        header = ttk.Frame(parent, style="Card.TFrame", padding=10)
        header.pack(fill="x")
        ttk.Label(header, text="输出速度 · t/s", style="Sec.TLabel").pack(anchor="w")
        ttk.Label(header, text="平均速度 = 输出 token ÷ 总耗时（包含等待）。测试完成后显示；接口没报告 token 就不计算。",
                  style="Muted.TLabel", wraplength=580).pack(anchor="w", pady=(4, 8))
        actions = ttk.Frame(header, style="Card.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="复制所选记录", command=self.copy).pack(side="left")
        ttk.Button(actions, text="清空速度记录", command=self.clear).pack(side="left", padx=6)
        ttk.Label(actions, text="查看不会发起测试", style="Muted.TLabel").pack(side="right")
        table = ttk.Frame(parent, style="Card.TFrame")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.columns = ("model", "tps", "tokens", "seconds", "first", "kind", "provider", "key", "status")
        self.tree = ttk.Treeview(table, columns=self.columns, show="headings", selectmode="extended", height=6)
        for column, title, width in zip(self.columns,
                ("模型", "平均 t/s", "输出 token", "总耗时(秒)", "首字(秒)", "测试", "供应商", "Key 名称", "状态"),
                (155, 85, 105, 100, 90, 105, 150, 100, 95)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=65, stretch=column in ("model", "provider"))
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.note = tk.StringVar(value="还没有速度记录。运行连通性、批量测速或鹈鹕测试后，在这里查看。")
        ttk.Label(parent, textvariable=self.note, style="Muted.TLabel", padding=10, wraplength=580).pack(side="bottom", fill="x")
        table.pack(fill="both", expand=True, padx=8)
        self.tree.bind("<<TreeviewSelect>>", self.describe)
        self.tree.bind("<Control-c>", lambda _event: self.copy())
        self.tree.bind("<Button-3>", self.menu)

    @staticmethod
    def values(record):
        metrics = record["metrics"]
        return (record["model"], f"{metrics['tps']:.2f}" if metrics.get("tps") is not None else "未报告",
                metrics.get("output_tokens") if metrics.get("output_tokens") is not None else "未报告",
                f"{metrics['elapsed']:.3f}", f"{metrics['ttfb']:.3f}" if metrics.get("ttfb") is not None else "—",
                record["kind"], record["provider"], record["key_name"], "成功" if record["ok"] else "失败/未完成")

    def add(self, record):
        self.sequence += 1
        iid = str(self.sequence)
        self.rows[iid] = record
        self.tree.insert("", 0, iid=iid, values=self.values(record))
        # This view needs no polling and cannot grow without bound.
        while len(self.rows) > 500:
            oldest = next(iter(self.rows))
            del self.rows[oldest]
            self.tree.delete(oldest)
        self.note.set(f"已记录 {len(self.rows)} 次测试，最新一条在最上方。接口报告的输出 token 可能包含推理 token。")

    def describe(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        record = self.rows[selected[0]]
        metrics = record["metrics"]
        text = f"{record['model']} · {record['provider']} · {record['key_name']} · {record['kind']}。"
        if metrics.get("output_tokens") is None:
            text += "接口未报告输出 token，无法计算 t/s。"
        elif metrics.get("tps") is None:
            text += "没有有效耗时，无法计算 t/s。"
        else:
            text += f"{metrics['output_tokens']:,} token ÷ {metrics['elapsed']:.3f} 秒 = {metrics['tps']:.2f} t/s（包含等待）。"
        if not record["ok"]:
            text += "本次失败或未完成，数值仅代表已收到的接口统计。"
        if record.get("error"):
            text += " " + record["error"]
        self.note.set(text)

    def copy(self):
        selected = self.tree.selection()
        if selected:
            text = "\t".join(self.tree.heading(c, "text") for c in self.columns) + "\n"
            text += "\n".join("\t".join(str(v) for v in self.values(self.rows[iid])) for iid in selected)
            self.parent.clipboard_clear()
            self.parent.clipboard_append(text)
        return "break"

    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.note.set("速度记录已清空；模型列表和作品不受影响。")

    def menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        menu = tk.Menu(self.parent, tearoff=0, bg=self.colors["panel"], fg=self.colors["fg"],
                       activebackground=self.colors["sel"], activeforeground=self.colors["acc_hi"],
                       bd=0, relief="flat", font=self.font)
        menu.add_command(label="复制速度记录", command=self.copy)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
