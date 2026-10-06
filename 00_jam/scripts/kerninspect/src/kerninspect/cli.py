"""编排层 CLI：ISO 启动 → 取符号 → gdb 断点 dump → 生成报告（kerninspect）。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from .qemu import QemuRunner
from .report import write_report

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GDB_SCRIPT = PROJECT_ROOT / "gdb" / "kerninspect_gdb.py"


def elf_symbol_value(path: str | Path, name: str) -> int:
    """readelf -s 解析符号值（kernel.debug 无 program headers，不能靠 file 加载）。"""
    out = subprocess.run(
        ["readelf", "-s", str(path)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[7] == name:
            return int(fields[1], 16)
    raise RuntimeError("symbol %r not found in %s" % (name, path))


def build_boot_gdb(
    kernel: str, btext_addr: int, port: int, gdb_py: Path
) -> str:
    return """set pagination off
set confirm off
target remote :{port}
add-symbol-file {kernel} {btext:#x}
source {gdb_py}
hbreak btext
continue
kern-dump loader
kern-tree loader
hbreak cpu_startup
continue
kern-dump kernel
kern-tree kernel
detach
quit
""".format(port=port, kernel=kernel, btext=btext_addr, gdb_py=gdb_py)


def run_gdb(boot_gdb: Path, gdb_bin: str = "/usr/bin/gdb") -> tuple[int, str]:
    proc = subprocess.run(
        [gdb_bin, "-q", "-batch", "-x", str(boot_gdb)],
        capture_output=True,
        text=True,
        timeout=240,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="kerninspect",
        description="QEMU + gdb(Python API) 检视 FreeBSD 内核内存",
    )
    ap.add_argument("--iso", required=True, help="FreeBSD 启动 ISO 路径")
    ap.add_argument("--kernel", required=True, help="kernel.debug（含符号）路径")
    ap.add_argument("--smp", type=int, default=1, help="CPU 数（默认 1）")
    ap.add_argument("--mem", default="1G", help="内存（默认 1G）")
    ap.add_argument("--gdb-port", type=int, default=1234, help="gdbstub 端口")
    ap.add_argument("--out-dir", default="/tmp/kerninspect-report", help="报告输出目录")
    ap.add_argument("--gdb-bin", default="/usr/bin/gdb")
    args = ap.parse_args(argv)

    kernel = Path(args.kernel)
    if not kernel.exists():
        print("kernel.debug 不存在: %s" % kernel, file=sys.stderr)
        return 1
    if not Path(args.iso).exists():
        print("ISO 不存在: %s" % args.iso, file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[1/4] 解析符号地址 ...")
    btext_addr = elf_symbol_value(kernel, "btext")
    print("      btext = %#x" % btext_addr)

    print("[2/4] 启动 QEMU（gdbstub :%d）..." % args.gdb_port)
    serial_log = out_dir / "serial.log"
    with QemuRunner(
        args.iso, smp=args.smp, mem=args.mem,
        gdb_port=args.gdb_port, serial_log=serial_log,
    ) as qemu:
        boot_gdb = out_dir / "boot.gdb"
        boot_gdb.write_text(
            build_boot_gdb(str(kernel), btext_addr, args.gdb_port, GDB_SCRIPT)
        )

        print("[3/4] gdb 断点 dump（btext → cpu_startup）...")
        rc, gdb_out = run_gdb(boot_gdb, args.gdb_bin)
        (out_dir / "gdb.log").write_text(gdb_out)
        print("      gdb exit=%d（详见 %s）" % (rc, out_dir / "gdb.log"))

        if rc != 0:
            print("      !!! gdb 异常退出，dump 可能不完整", file=sys.stderr)

        dump_loader = Path("/tmp/kerninspect-dump-loader.txt")
        dump_kernel = Path("/tmp/kerninspect-dump-kernel.txt")
        tree_loader = Path("/tmp/kerninspect-tree-loader.txt")
        tree_kernel = Path("/tmp/kerninspect-tree-kernel.txt")
        for f in (dump_loader, dump_kernel, tree_loader, tree_kernel):
            if f.exists():
                (out_dir / f.name).write_text(f.read_text())

        print("[4/4] 生成报告 ...")
        report_md, report_json = write_report(
            out_dir,
            loader=Path(out_dir / "kerninspect-dump-loader.txt"),
            kernel=Path(out_dir / "kerninspect-dump-kernel.txt"),
            tree_loader=Path(out_dir / "kerninspect-tree-loader.txt"),
            tree_kernel=Path(out_dir / "kerninspect-tree-kernel.txt"),
            serial=serial_log,
            gdb_log=out_dir / "gdb.log",
        )

    print("完成。产物：")
    print("  Markdown  : %s" % report_md)
    print("  JSON      : %s" % report_json)
    print("  serial    : %s" % serial_log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
