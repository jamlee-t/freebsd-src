# kerninspect

用 **QEMU + gdb（内置 Python API）** 检视 FreeBSD 内核内存的可复现工具链。
第一个用例是验证启动早期页表：启动 ISO → 硬件断点停在 `btext` / `cpu_startup`
→ Python 按 LA48 四级 walk 逐项解析 CR3 与 PML4/PDP/PD/PT → 输出双时刻对照报告。
**未来读任何内核内存结构（bootinfo、kenv、vm_page 数组、各种链表……）都加在这个
框架里：通用原语只有两个——按物理地址读、按虚拟地址 walk。**

设计说明（为什么这么拆）：

- `gdb/kerninspect_gdb.py` 运行在 **gdb 进程内**（`gdb -x` 加载），直接用 gdb 的
  Python API（`gdb.Command`、`gdb.selected_inferior().read_memory`、
  `gdb.parse_and_eval`）。断点停住时只有 gdb 能读目标内存，所以解析必须在这里做。
- `src/kerninspect/` 是 **uv 管理的编排层**（纯标准库）：起 QEMU、取符号地址、
  生成 gdb 批次脚本、跑 gdb、汇总报告。它不接触目标内存。
- 两侧通过文件握手：gdb 侧把 dump 写进 `/tmp/kerninspect-dump-<tag>.txt`，
  编排层读取后生成 Markdown/JSON 报告。

## 用法

```bash
cd /root/freebsd-src/.00_jam/scripts/kerninspect
uv sync

# 最小参数（其余取默认）
uv run kerninspect \
  --iso /tmp/freebsd-minimal.iso \
  --kernel /tmp/obj/root/freebsd-src/amd64.amd64/sys/MINIMAL/kernel.debug \
  --out-dir /tmp/kerninspect-report

# 完整参数
uv run kerninspect \
  --iso /tmp/freebsd-minimal.iso \
  --kernel /tmp/obj/root/freebsd-src/amd64.amd64/sys/MINIMAL/kernel.debug \
  --smp 1 --mem 1G --gdb-port 1234 \
  --out-dir /tmp/kerninspect-report
```

产物（在 `--out-dir`）：

| 文件 | 内容 |
|---|---|
| `verify-report.md` | 双时刻页表对照（loader 临时表 / 正式表），可直接贴进文档 |
| `verify-report.json` | 结构化数据（CR3、PML4 槽、walk 结果） |
| `kerninspect-dump-loader.txt` / `-kernel.txt` | gdb 侧原始 dump |
| `serial.log` | QEMU 串口日志（可与 dump 互相印证） |

## gdb 内命令（手动调试时可用）

| 命令 | 作用 |
|---|---|
| `kern-dump <tag> [<symbol>]` | dump 当前页表到 `/tmp/kerninspect-dump-<tag>.txt` |
| `kern-walk <va>` | 单虚拟地址 LA48 四级 walk，打印到 gdb |
| `kern-page <phys> <lvl> [<tag>]` | dump 一页（512 项） |
| （自动） | stop 事件命中 `btext`/`cpu_startup` 自动 dump |

## 断点与读取的坑（已踩平）

1. **add-symbol-file 的地址**：`kernel.debug` 无 program headers（EXEC 型），
   必须 `add-symbol-file kernel.debug <btext_addr>`；`btext` 地址由 CLI 用
   `readelf -s` 自动解析（本内核实测 `0xffffffff802c0000`）。
2. **硬件断点**：QEMU 的 gdbstub 只支持硬件断点（`hbreak`）；按 PC 比较天然
   绕开"物理取指 vs 虚拟链接地址"的换算问题。
3. **虚拟地址读失败**：gdb `read_memory` 走当前页表翻译；正式表的页表页
   （物理 0x1438000 一带）不在低 2MB identity 内，直接读会失败 ——
   `read_phys()` 先直读、失败后改用 **DMAP 虚拟地址** `0xfffff80000000000+phys`
   重读（这正是 DMAP 直映的早期价值）。
4. **不提前 `set architecture` / `file`**：先 `target remote` 再
   `add-symbol-file`，避免 target description 与符号表互相干扰。

## 扩展：新增一种内存结构的读法

通用原语已经就位，新结构只需两步：

1. 在 `gdb/kerninspect_gdb.py` 里加一个解析函数 + 一条 `gdb.Command`
   （参考 `kern-page` 的写法：`read_phys` → 解构 → 打印/写文件）；
2. 需要进报告的，在 `src/kerninspect/report.py` 加一段解析与 Markdown 输出。

页表、kenv、bootinfo、vm_page 数组都可以照此接入，互不影响。

## 与源码的对应关系

- 临时表：loader 为 btext 准备的平坦映射（任意虚拟地址 → 物理前 18MB+）。
- 正式表：`hammer_time → pmap_bootstrap → create_pagetables`（`sys/amd64/amd64/pmap.c`）
  铺设的 DMAP / KERNBASE / kernel map / 递归页表。
- 实测值：loader 表 CR3=`0x72000`；正式表 CR3=`0x1438000`/`0x1439000`
  （每次启动随机，布局固定）；btext 经 PML4[511]→PDP[510]→PD[1] 大页落在
  物理 `0x2c0000`。
