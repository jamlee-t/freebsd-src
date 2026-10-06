"""QEMU 进程管理：启动（-s -S gdbstub）、串口日志、清理。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path


class QemuRunner:
    """启动一台挂 gdbstub 的 QEMU 虚拟机，跑完负责清理。"""

    def __init__(
        self,
        iso: str | Path,
        *,
        smp: int = 1,
        mem: str = "1G",
        gdb_port: int = 1234,
        serial_log: str | Path | None = None,
    ) -> None:
        self.iso = str(iso)
        self.smp = smp
        self.mem = mem
        self.gdb_port = gdb_port
        self.serial_log = str(serial_log) if serial_log else "/tmp/kerninspect-serial.log"
        self.proc: subprocess.Popen | None = None

    def start(self, wait: float = 3.0) -> None:
        cmd = [
            "qemu-system-x86_64",
            "-m", self.mem,
            "-smp", str(self.smp),
            "-nographic",
            "-serial", f"file:{self.serial_log}",
            "-drive", f"file={self.iso},media=cdrom,format=raw",
            "-s",            # gdbstub on tcp::1234
            "-S",            # 起步即停，等 gdb attach
            "-no-reboot",
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(wait)

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        finally:
            self.proc = None

    def serial_text(self) -> str:
        try:
            return Path(self.serial_log).read_text(errors="replace")
        except Exception:
            return ""

    def __enter__(self) -> "QemuRunner":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
