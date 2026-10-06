"""报告生成：解析 gdb dump 文本 → Markdown / JSON 双时刻对照。"""

from __future__ import annotations

import json
import re
from pathlib import Path

CR3_RE = re.compile(r"CR3 = (0x[0-9a-fA-F]+)")
PML4_RE = re.compile(r"\[\s*(\d+)(?:-\s*(\d+))?\]\s+(0x[0-9a-fA-F]+)\s+(.*)")
WALK_RE = re.compile(r"--- walk (0x[0-9a-fA-F]+)")
STEP_RE = re.compile(
    r"L(\d) idx=(\d+) entry=(0x[0-9a-fA-F]+)\s+P=(\d) RW=(\d) US=(\d) NX=(\d) phys=(0x[0-9a-fA-F]+) (\S+)"
)


def parse_dump(path: Path) -> dict | None:
    if not path.exists():
        return None
    text = path.read_text(errors="replace")
    data: dict = {"pml4": [], "walks": {}}
    m = CR3_RE.search(text)
    data["cr3"] = int(m.group(1), 16) if m else None
    for line in text.splitlines():
        m = PML4_RE.match(line.strip())
        if m:
            start, end, entry, rest = m.groups()
            data["pml4"].append({
                "start": int(start),
                "end": int(end) if end else int(start),
                "entry": entry,
                "attrs": rest.strip(),
            })
        m = WALK_RE.search(line.strip())
        if m:
            data["cur_walk"] = int(m.group(1), 16)
            data["walks"][data["cur_walk"]] = []
        m = STEP_RE.match(line.strip())
        if m and "cur_walk" in data:
            data["walks"][data["cur_walk"]].append({
                "lvl": int(m.group(1)),
                "idx": int(m.group(2)),
                "entry": m.group(3),
                "p": m.group(4), "rw": m.group(5), "us": m.group(6),
                "nx": m.group(7), "phys": m.group(8), "kind": m.group(9),
            })
    return data


def _pml4_md(pml4: list[dict]) -> str:
    if not pml4:
        return "（无数据）"
    rows = []
    for e in pml4:
        rng = str(e["start"]) if e["start"] == e["end"] else "%d-%d" % (e["start"], e["end"])
        rows.append("`[%s]` `%s` %s" % (rng, e["entry"], e["attrs"]))
    return "；".join(rows)


def _walks_md(walks: dict) -> str:
    if not walks:
        return "（无数据）"
    lines = []
    for va, steps in walks.items():
        seg = " → ".join(
            "L%d[%d]=%s %s" % (s["lvl"], s["idx"], s["entry"], s["kind"])
            for s in steps
        )
        lines.append("`%#x`：%s" % (va, seg))
    return "<br/>".join(lines)


def write_report(
    out_dir: Path,
    loader: Path,
    kernel: Path,
    tree_loader: Path,
    tree_kernel: Path,
    serial: Path,
    gdb_log: Path,
) -> tuple[Path, Path]:
    dl = parse_dump(loader)
    dk = parse_dump(kernel)

    serial_text = serial.read_text(errors="replace") if serial.exists() else ""
    gdb_text = gdb_log.read_text(errors="replace") if gdb_log.exists() else ""

    def tree_tail(path: Path, limit: int = 60) -> str:
        if not path.exists():
            return "（缺失）"
        lines = path.read_text(errors="replace").splitlines()
        if len(lines) <= limit:
            return "\n".join(lines)
        return "\n".join(lines[:limit]) + "\n…（共 %d 行，已截断，完整见 tree 文件）" % len(lines)

    md = []
    md.append("# 内核内存实测报告（kerninspect）")
    md.append("")
    md.append("由 `uv run kerninspect` 自动生成：QEMU 启动 ISO，gdb 硬件断点")
    md.append("停在 `btext`（loader 临时表）与 `cpu_startup`（正式表），")
    md.append("Python 按 LA48 四级 walk 逐项解析。")
    md.append("")
    md.append("## 双时刻对照")
    md.append("")
    md.append("| 项 | loader 临时表（btext） | 正式表（cpu_startup） |")
    md.append("|---|---|---|")
    md.append("| CR3 | `%s` | `%s` |" % (
        ("%#x" % dl["cr3"]) if dl and dl["cr3"] else "—",
        ("%#x" % dk["cr3"]) if dk and dk["cr3"] else "—",
    ))
    md.append("| PML4 | %s | %s |" % (
        _pml4_md(dl["pml4"]) if dl else "（无）",
        _pml4_md(dk["pml4"]) if dk else "（无）",
    ))
    md.append("| walk | %s | %s |" % (
        _walks_md(dl["walks"]) if dl else "（无）",
        _walks_md(dk["walks"]) if dk else "（无）",
    ))
    md.append("")
    md.append("## 页表树（完整遍历，自带解读）")
    md.append("")
    md.append("### loader（btext）")
    md.append("")
    md.append("```text")
    md.append(tree_tail(tree_loader))
    md.append("```")
    md.append("")
    md.append("### kernel（cpu_startup）")
    md.append("")
    md.append("```text")
    md.append(tree_tail(tree_kernel))
    md.append("```")
    md.append("")
    md.append("## 原始 dump")
    md.append("")
    md.append("### loader（btext）")
    md.append("")
    md.append("```text")
    md.append(loader.read_text(errors="replace") if loader.exists() else "（缺失）")
    md.append("```")
    md.append("")
    md.append("### kernel（cpu_startup）")
    md.append("")
    md.append("```text")
    md.append(kernel.read_text(errors="replace") if kernel.exists() else "（缺失）")
    md.append("```")
    md.append("")
    md.append("## 串口日志（前 40 行）")
    md.append("")
    md.append("```text")
    md.append("\n".join(serial_text.splitlines()[:40]))
    md.append("```")
    md.append("")
    md.append("## gdb 输出（尾部 30 行）")
    md.append("")
    md.append("```text")
    md.append("\n".join(gdb_text.splitlines()[-30:]))
    md.append("```")
    md.append("")

    report_md = out_dir / "verify-report.md"
    report_md.write_text("\n".join(md), encoding="utf-8")

    payload = {
        "loader": dl,
        "kernel": dk,
        "serial_tail": serial_text.splitlines()[-20:],
        "gdb_exit_note": "见 gdb.log",
    }
    report_json = out_dir / "verify-report.json"
    report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return report_md, report_json
