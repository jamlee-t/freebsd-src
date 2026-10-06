"""运行在 gdb 进程内的 Python 脚本（gdb -x 加载）。

直接用 gdb 内置 Python API：
  - gdb.Command / gdb.parse_and_eval / gdb.selected_inferior().read_memory
  - gdb.events.stop 自动识别 btext / cpu_startup 断点并 dump

用法（由编排层生成 boot.gdb 调用）：
  hbreak btext
  continue
  kern-dump loader
  hbreak cpu_startup
  continue
  kern-dump kernel

dump 结果写入 /tmp/kerninspect-dump-<tag>.txt，供编排层生成报告。

扩展新内存结构：加一个解析函数 + 一条 gdb.Command（参考 kern-page），
通用原语只有两个：read_phys（按物理地址读）与 walk（按虚拟地址四级翻译）。
"""

import gdb

DMAP = 0xFFFFF80000000000          # FreeBSD amd64 LA48 的 direct map 起点
WALK_VA = [
    0x0000000000000000,             # 低地址（identity）
    0xFFFF800000000000,             # PML4[256] 递归页表
    0xFFFFF80000000000,             # PML4[496] DMAP
    0xFFFFFE0000000000,             # PML4[508] kernel map（VM_MIN_KERNEL_ADDRESS）
    0xFFFFFFFF80000000,             # PML4[511] KERNBASE
]
PHYS_MASK = 0x000FFFFFFFFFF000      # 4KB/2MB/1GB 页物理地址掩码（低 12 位对齐）
PHYS_MASK_1G = 0x000FFFFFFFC00000   # 1GB 大页物理掩码（PDP PS=1）
PHYS_MASK_2M = 0x000FFFFFFFE00000   # 2MB 大页物理掩码（PD PS=1）

DUMP_DIR = "/tmp"


# ---------------------------------------------------------------- 读物理内存

def read_phys(addr: int, size: int) -> bytes | None:
    """读物理地址 addr。

    gdb 的 read_memory 是虚拟地址读（经当前页表翻译）：
    - loader 临时表阶段：页表页在低 2MB identity 内，直读即可；
    - 正式表阶段：页表页（0x1438000 一带）不在 identity 内，直读失败，
      改用 DMAP 虚拟地址 0xfffff80000000000+phys 重读。
    """
    for va in (addr, DMAP + addr):
        try:
            return bytes(gdb.selected_inferior().read_memory(va, size))
        except Exception:
            continue
    return None


def read_u64(addr: int) -> int:
    raw = read_phys(addr, 8)
    if raw is None:
        raise RuntimeError("read fail: phys=%#x" % addr)
    return int.from_bytes(raw, "little")


# ---------------------------------------------------------------- 表项解析

def parse_entry(e: int, lvl: int) -> dict:
    p = e & 1
    attrs = {
        "entry": e,
        "p": p,
        "rw": (e >> 1) & 1,
        "us": (e >> 2) & 1,
        "ps": (e >> 7) & 1,
        "nx": (e >> 63) & 1,
        "phys": 0,
        "kind": "空",
    }
    if not p:
        return attrs
    if attrs["ps"]:
        # 大页：1GB 只在 PDP 级（lvl==1），2MB 在 PD 级（lvl==2）
        mask = PHYS_MASK_1G if lvl == 1 else PHYS_MASK_2M
        attrs["phys"] = e & mask
        attrs["kind"] = "大页"
    else:
        attrs["phys"] = e & PHYS_MASK
        attrs["kind"] = "表页"
    return attrs


def fmt_entry(a: dict, lvl: int) -> str:
    if not a["p"]:
        return "  空（未映射）"
    bits = []
    bits.append("可读写" if a["rw"] else "只读")
    bits.append("用户可访问" if a["us"] else "仅内核")
    if a["ps"]:
        bits.append("大页")
    if a["nx"]:
        bits.append("禁执行")
    return "表项值=%#018x → %s %#x  %s" % (
        a["entry"], a["kind"], a["phys"], "·".join(bits),
    )


# ---------------------------------------------------------------- 四级 walk

def cr3() -> int:
    """取 CR3（页表基址），去掉标志位低 12 位。"""
    try:
        return int(gdb.parse_and_eval("$cr3")) & ~0xFFF
    except Exception:
        out = gdb.execute("info registers cr3", to_string=True)
        return int(out.split()[1], 16) & ~0xFFF


def walk(va: int) -> list[dict]:
    """LA48 四级 walk：返回 [L0..L3] 每级的索引与表项解析结果。"""
    idx = [
        (va >> 39) & 0x1FF,
        (va >> 30) & 0x1FF,
        (va >> 21) & 0x1FF,
        (va >> 12) & 0x1FF,
    ]
    steps = []
    phys = cr3()
    for lvl, i in enumerate(idx):
        e = read_u64(phys + i * 8)
        a = parse_entry(e, lvl)
        steps.append({"lvl": lvl, "idx": i, **a})
        if not a["p"] or a["ps"]:
            break
        phys = a["phys"]
    return steps


def walk_lines(va: int) -> list[str]:
    lines = ["--- walk %#018x" % va]
    for s in walk(va):
        if s["p"]:
            lines.append("    L%d idx=%-3d %s" % (s["lvl"], s["idx"], fmt_entry(s, s["lvl"])))
        else:
            lines.append("    L%d idx=%-3d 表项值=%#018x 空（未映射）" % (s["lvl"], s["idx"], s["entry"]))
    return lines


# ---------------------------------------------------------------- 阅读指南

READER_GUIDE = """【页表阅读指南·先读这里】
零、先看懂一行（拿 PML4[0] 为例）
     [0]  → PDP 表 0x73000   表项值=0x73023
     └┬─┘   └──┬────────┘   └──┬─────┘
     槽位号  这个数的翻译     槽里存的原始 64 位数字
   “表项值”就是这一格内存里原样存放的数字；它由两半组成：
     高位（0x73000）= 下一级表的物理地址，箭头后的内容就是它
     低位（0x023）  = 标志位：0x001 存在 · 0x002 可读写 · 0x020 已访问
   所以整行读作：PML4 表 [0] 号槽说“下一级是 PDP 表，它在物理 0x73000”。
   箭头连续读下去，就是 CPU 逐级找页的路径（walk）。
一、四级页表与虚拟地址切分（LA48，Intel 硬件 MMU 规则，非内核自定义）
   48 位虚拟地址被切成 5 段，每级表 512 项（9 位索引）：
     bit47-39 → PML4 索引（第 0 级表）
     bit38-30 → PDP 索引（第 1 级表）
     bit29-21 → PD  索引（第 2 级表）
     bit20-12 → PT  索引（第 3 级表）
     bit11-0  → 页内偏移
   walk 算法 = (va >> (12 + 9*级数)) & 0x1ff，逐级取表项、解出下一级表地址。
二、表项 64 位字段（bit 从低往高数）
   bit0   P   存在：1=映射有效；0=未映射（访问触发缺页 #PF）
   bit1   RW  读写：1=可读写；0=只读
   bit2   US  用户：1=用户态可访问；0=仅内核态（写 S）
   bit3   PWT 写直达缓存（忽略）
   bit4   PCD 禁用缓存（忽略）
   bit5   A   已访问：CPU 读过即置 1
   bit6   D   脏：CPU 写过即置 1
   bit7   PS  大页：1=本级直接命中（PDP 级=1GB / PD 级=2MB）；0=继续下一级
   bit11  G   全局页：CR4.PGE 时跨进程不刷 TLB
   bit51-12 PFN 物理页帧号（物理地址的高位，低 12 位页内偏移在访问时补齐）
   bit63  NX  禁执行：1=该区域不能取指
三、输出里权限组合怎么读（如 可读写·仅内核·禁执行）
   可读写 / 只读   仅内核 / 用户可访问   禁执行
   ·  可读写·仅内核·禁执行 = 数据区（可读写、仅内核、禁取指）
   ·  可读写·仅内核    = 代码/loader 区（可读写、仅内核、可取指）
   ·  只读·仅内核·禁执行 = 只读保护区
四、区间与粒度
   叶子行（大页/4KB 页）同时给出“虚拟 区间”与“物理 区间”：
     虚拟 = 由本级槽位号拼出：i0<<39 | i1<<30 | i2<<21 | i3<<12（bit47 符号扩展）
     例：PML4[511]→PDP[510]→PD[1] 大页 = 虚拟 0xffffffff80000000-0xffffffff80200000
   2MB 大页：PD 项 PS=1，一次映射 2MB（loader 表全用这个）
   1GB 大页：PDP 项 PS=1（本内核未使用）
   4KB 页：走满 4 级，PT 项（kernel 表细粒度区域用）
五、两套表怎么对照
   loader 表（btext 时刻）：扁平全映射，任何虚拟地址都落到物理前 4GB，只求能取指
   kernel 表（cpu_startup 时刻）：分区+权限+NX：DMAP / KERNBASE / kernel map / 递归页表各司其职"""


SEPARATOR = "-" * 120


def _guide_block(title: str) -> str:
    return "== %s ==\n%s\n" % (title, READER_GUIDE)


def _sep_block() -> str:
    return "\n%s\n\n" % SEPARATOR


# ---------------------------------------------------------------- dump 输出

ZONE_TAGS = {
    256: "递归页表(自映射)",
    257: "large map", 258: "large map", 259: "large map", 260: "large map",
    261: "large map", 262: "large map", 263: "large map", 264: "large map",
    496: "DMAP(direct map)",
    508: "kernel map", 509: "kernel map", 510: "kernel map", 511: "kernel map/KERNBASE",
}


def zone_tag(l0: int) -> str:
    return ZONE_TAGS.get(l0, "扁平映射")


def _perm_str(a: dict) -> str:
    parts = ["可读写" if a["rw"] else "只读"]
    parts.append("用户可访问" if a["us"] else "仅内核")
    if a["nx"]:
        parts.append("禁执行")
    return "·".join(parts)


def _interval(phys: int, size: int) -> str:
    return "%#x-%#x" % (phys, phys + size)


def _va_base(i0: int, i1=None, i2=None, i3=None) -> int:
    """由各级槽位号拼 48 位虚拟地址：i0<<39 | i1<<30 | i2<<21 | i3<<12，bit47 符号扩展。"""
    va = (i0 & 0x1FF) << 39
    if i1 is not None:
        va |= (i1 & 0x1FF) << 30
    if i2 is not None:
        va |= (i2 & 0x1FF) << 21
    if i3 is not None:
        va |= (i3 & 0x1FF) << 12
    if va & (1 << 47):
        va |= ~((1 << 48) - 1)
    return va & ((1 << 64) - 1)


def _va_interval(i0: int, i1, i2, i3, size: int) -> str:
    base = _va_base(i0, i1, i2, i3)
    return "%#x-%#x" % (base, base + size)


def _page_entries(phys: int) -> list[int]:
    """整页批量读：一次 gdb read 拿 512 项，避免逐项 read_u64 的巨量往返。"""
    raw = read_phys(phys, 4096)
    if raw is None:
        raise RuntimeError("read fail: phys=%#x" % phys)
    return [int.from_bytes(raw[i*8:(i+1)*8], "little") for i in range(512)]


def tree_lines() -> tuple[list[str], dict]:
    """完整遍历页表树：PML4 → PDP → PD → PT，只收存在=1 项；连续相同表项合并。
    每张表（PML4/PDP/PD/PT）出现时先打印它自己的物理地址，项行全部中文解读。
    每张表页只发起一次 gdb 内存读（整页批量），否则全树会慢到不可用。"""
    lines = []
    stats = {"pml4": 0, "pdp": 0, "pd": 0, "pt": 0, "big1g": 0, "big2m": 0, "leaf4k": 0}
    root = cr3()
    lines.append("页表树（根 = PML4 表，物理地址 %#x）：PML4→PDP→PD→PT，只列“存在=1”的表项，同值连续槽已合并" % root)

    l0 = _page_entries(root)
    lines.append("L0 PML4 表 @%#x" % root)
    i0 = 0
    while i0 < 512:
        e0 = l0[i0]
        a0 = parse_entry(e0, 0)
        if not a0["p"]:
            i0 += 1
            continue
        j0 = i0 + 1
        while j0 < 512 and l0[j0] == e0:
            j0 += 1
        rng0 = "[%d]" % i0 if j0 == i0 + 1 else "[%d-%d]" % (i0, j0 - 1)
        stats["pml4"] += 1
        p1 = a0["phys"]
        lines.append("  %-9s → %-8s %#x   表项值=%#018x   %s" % (rng0, "PDP 表", p1, e0, zone_tag(i0)))

        l1 = _page_entries(p1)
        lines.append("  L1 PDP 表 @%#x" % p1)
        i1 = 0
        while i1 < 512:
            e1 = l1[i1]
            a1 = parse_entry(e1, 1)
            if not a1["p"]:
                i1 += 1
                continue
            j1 = i1 + 1
            while j1 < 512 and l1[j1] == e1:
                j1 += 1
            rng1 = "[%d]" % i1 if j1 == i1 + 1 else "[%d-%d]" % (i1, j1 - 1)
            if a1["ps"]:
                stats["big1g"] += (j1 - i1)
                lines.append("    %-9s → %-8s 虚拟 %-38s 物理 %-32s 表项值=%#018x  %-11s" % (
                    rng1, "1GB 大页",
                    _va_interval(i0, i1, None, None, 0x40000000),
                    "%#x-%#x" % (a1["phys"], a1["phys"] + 0x40000000),
                    e1, _perm_str(a1)))
                i1 = j1
                continue
            stats["pdp"] += 1
            p2 = a1["phys"]
            lines.append("    %-9s → %-8s %#x   表项值=%#018x" % (rng1, "PD 表", p2, e1))

            l2 = _page_entries(p2)
            lines.append("    L2 PD 表 @%#x" % p2)
            i2 = 0
            while i2 < 512:
                e2 = l2[i2]
                a2 = parse_entry(e2, 2)
                if not a2["p"]:
                    i2 += 1
                    continue
                j2 = i2 + 1
                while j2 < 512 and l2[j2] == e2:
                    j2 += 1
                rng2 = "[%d]" % i2 if j2 == i2 + 1 else "[%d-%d]" % (i2, j2 - 1)
                if a2["ps"]:
                    stats["big2m"] += (j2 - i2)
                    lines.append("      %-9s → %-8s 虚拟 %-38s 物理 %-32s 表项值=%#018x  %-11s" % (
                        rng2, "2MB 大页",
                        _va_interval(i0, i1, i2, None, 0x200000),
                        "%#x-%#x" % (a2["phys"], a2["phys"] + 0x200000),
                        e2, _perm_str(a2)))
                    i2 = j2
                    continue
                stats["pd"] += 1
                p3 = a2["phys"]
                lines.append("      %-9s → %-8s %#x   表项值=%#018x" % (rng2, "PT 表", p3, e2))

                l3 = _page_entries(p3)
                lines.append("      L3 PT 表 @%#x" % p3)
                i3 = 0
                while i3 < 512:
                    e3 = l3[i3]
                    a3 = parse_entry(e3, 3)
                    if not a3["p"]:
                        i3 += 1
                        continue
                    j3 = i3 + 1
                    while j3 < 512 and l3[j3] == e3:
                        j3 += 1
                    rng3 = "[%d]" % i3 if j3 == i3 + 1 else "[%d-%d]" % (i3, j3 - 1)
                    stats["leaf4k"] += (j3 - i3)
                    lines.append("        %-9s → %-8s 虚拟 %-38s 物理 %-32s 表项值=%#018x  %-11s" % (
                        rng3, "4KB 页",
                        _va_interval(i0, i1, i2, i3, 0x1000),
                        "%#x-%#x" % (a3["phys"], a3["phys"] + 0x1000),
                        e3, _perm_str(a3)))
                    i3 = j3
                i2 = j2
            i1 = j1
        i0 = j0

    lines.append("-- 统计：PML4 有效段 %d · PDP 表项 %d（=PD 表张数） · PD 表项 %d（=PT 表张数） · 1GB 大页 %d · 2MB 大页 %d · 4KB 叶 %d" % (
        stats["pml4"], stats["pdp"], stats["pd"],
        stats["big1g"], stats["big2m"], stats["leaf4k"]))
    return lines, stats


def dump_text(tag: str, sym_name: str) -> str:
    lines = []
    lines.append("== %s @ %s 命中 ==" % (tag, sym_name))
    lines.append(_guide_block("阅读指南"))
    lines.append(_sep_block().rstrip("\n"))
    lines.append("  CR3 = %#x" % cr3())
    lines.append("  PML4 槽位（按连续相同值压缩）:")
    pml4_phys = cr3()
    raw = read_phys(pml4_phys, 4096)
    entries = [int.from_bytes(raw[i*8:(i+1)*8], "little") for i in range(512)]
    i = 0
    while i < 512:
        e = entries[i]
        a = parse_entry(e, 0)
        j = i + 1
        while j < 512 and entries[j] == e:
            j += 1
        rng = "[%3d]" % i if j == i + 1 else "[%3d-%-3d]" % (i, j - 1)
        lines.append("      %s %s" % (rng, fmt_entry(a, 0)))
        i = j
    for va in WALK_VA:
        lines.extend(walk_lines(va))
    return "\n".join(lines) + "\n"


def _write_dump(tag: str, sym_name: str) -> None:
    path = "%s/kerninspect-dump-%s.txt" % (DUMP_DIR, tag)
    with open(path, "w") as f:
        f.write(dump_text(tag, sym_name))
    gdb.write("kern-dump: wrote %s\n" % path)


def _write_tree(tag: str, sym_name: str) -> None:
    path = "%s/kerninspect-tree-%s.txt" % (DUMP_DIR, tag)
    try:
        lines, stats = tree_lines()
    except Exception:
        import traceback
        try:
            with open(path + ".err", "w") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        try:
            gdb.write("kern-tree: FAILED %s（详情见 %s.err）\n" % (tag, path))
        except Exception:
            pass
        return
    try:
        with open(path, "w") as f:
            f.write("== %s @ %s 页表树 ==\n" % (tag, sym_name))
            f.write(_guide_block("阅读指南"))
            f.write(_sep_block())
            f.write("\n".join(lines) + "\n")
        gdb.write("kern-tree: wrote %s（%d 行；4KB 叶 %d，2MB 大页 %d）\n" % (
            path, len(lines), stats["leaf4k"], stats["big2m"]))
    except Exception:
        import traceback
        try:
            with open(path + ".err", "w") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass


# ---------------------------------------------------------------- gdb 命令

class TreeCmd(gdb.Command):
    """kern-tree <tag> [<symbol>]：完整遍历页表树（PML4→PDP→PD→PT，含解读）到 /tmp/kerninspect-tree-<tag>.txt"""

    def __init__(self):
        super(TreeCmd, self).__init__("kern-tree", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        parts = arg.strip().split()
        if not parts:
            gdb.write("usage: kern-tree <tag> [<symbol>]\n")
            return
        tag = parts[0]
        sym_name = parts[1] if len(parts) > 1 else "pc=%#x" % int(gdb.parse_and_eval("$pc"))
        _write_tree(tag, sym_name)


class DumpCmd(gdb.Command):
    """kern-dump <tag> [<symbol>]：dump 当前页表到 /tmp/kerninspect-dump-<tag>.txt"""

    def __init__(self):
        super(DumpCmd, self).__init__("kern-dump", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        parts = arg.strip().split()
        if not parts:
            gdb.write("usage: kern-dump <tag> [<symbol>]\n")
            return
        tag = parts[0]
        sym_name = parts[1] if len(parts) > 1 else "pc=%#x" % int(gdb.parse_and_eval("$pc"))
        _write_dump(tag, sym_name)


class WalkCmd(gdb.Command):
    """kern-walk <va>：单地址四级 walk 打印到 gdb 输出。"""

    def __init__(self):
        super(WalkCmd, self).__init__("kern-walk", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        va = int(arg.strip(), 16)
        gdb.write("\n".join(walk_lines(va)) + "\n")


class PageCmd(gdb.Command):
    """kern-page <phys> <lvl> <tag>：dump 一页（512 项）到 gdb 输出。"""

    def __init__(self):
        super(PageCmd, self).__init__("kern-page", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        parts = arg.strip().split()
        if len(parts) < 2:
            gdb.write("usage: kern-page <phys> <lvl> [<tag>]\n")
            return
        phys = int(parts[0], 16)
        lvl = int(parts[1])
        tag = " ".join(parts[2:]) or "页"
        raw = read_phys(phys, 4096)
        if raw is None:
            gdb.write("  !! read fail %#x\n" % phys)
            return
        gdb.write("  %s (phys=%#x, L%d):\n" % (tag, phys, lvl))
        for i in range(512):
            e = int.from_bytes(raw[i*8:(i+1)*8], "little")
            a = parse_entry(e, lvl)
            if a["p"]:
                gdb.write("      [%3d] %s\n" % (i, fmt_entry(a, lvl)))


class BitCmd(gdb.Command):
    """kern-bit <entry>：把任意页表项 64 位逐字段拆开。"""

    FIELD_SPECS = [
        (63, 63, "NX", "不可执行（Execute Disable），1=该页/块禁取指"),
        (62, 52, "保留", "软件可用/保留，硬件忽略"),
        (51, 12, "PFN", "物理页帧号：物理地址的高位（页内偏移在地址低 12 位）"),
        (11, 11, "G", "全局页（Global），CR4.PGE 时跨进程不刷新 TLB"),
        (10, 9, "保留", "必须为 0"),
        (8, 8, "AVL", "软件可用位"),
        (7, 7, "PS", "页大小：1=大页（PDP=1GB / PD=2MB），0=继续下一级"),
        (6, 6, "D", "脏位（Dirty），被写过则置 1"),
        (5, 5, "A", "已访问（Accessed），被读过则置 1"),
        (4, 4, "PCD", "禁用缓存（Page Cache Disable）"),
        (3, 3, "PWT", "写直达缓存（Page Write Through）"),
        (2, 2, "US", "用户/超级用户：1=用户态可访问，0=仅内核"),
        (1, 1, "RW", "读写：1=可读写，0=只读"),
        (0, 0, "P", "存在（Present）：0=未映射（触发缺页）"),
    ]

    def __init__(self):
        super(BitCmd, self).__init__("kern-bit", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        e = int(arg.strip(), 16)
        gdb.write("entry = %#018x\n" % e)
        gdb.write("  bit63-52 | bit51-12 PFN | bit11-0 标志\n")
        for hi, lo, name, desc in self.FIELD_SPECS:
            if hi == 63 and lo == 63:
                val = (e >> 63) & 1
            else:
                width = hi - lo + 1
                mask = (1 << width) - 1
                val = (e >> lo) & mask
            gdb.write("  bit%2d-%-2d  %-4s = %#x   %s\n" % (hi, lo, name, val, desc))
        a = parse_entry(e, 0)
        gdb.write("  => %s\n" % fmt_entry(a, 0))


# ---------------------------------------------------------------- 自动触发

def _sym_pc(name: str) -> int | None:
    try:
        sym = gdb.lookup_symbol(name)[0]
        if sym is None or not sym.value().address:
            return None
        return int(sym.value()) & 0xFFFFFFFFFFFF
    except Exception:
        return None


def _on_stop(event):
    """断点命中时自动 dump + tree：PC == btext → loader 表；PC == cpu_startup → 正式表。"""
    if not isinstance(event, gdb.StopEvent):
        return
    try:
        pc = int(gdb.parse_and_eval("$pc")) & 0xFFFFFFFFFFFF
    except Exception:
        return
    for name, tag in (("btext", "loader"), ("cpu_startup", "kernel")):
        target = _sym_pc(name)
        if target is not None and pc == target:
            _write_dump(tag, name)
            _write_tree(tag, name)
            return


def _install_auto():
    try:
        gdb.events.stop.connect(_on_stop)
    except Exception:
        pass


DumpCmd()
WalkCmd()
PageCmd()
BitCmd()
TreeCmd()
_install_auto()
gdb.write("kerninspect_gdb: loaded (kern-dump / kern-walk / kern-page / kern-bit / kern-tree, auto dump+tree on btext/cpu_startup)\n")
