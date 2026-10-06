#!/bin/bash
# FreeBSD 交叉编译 + 打包 ISO + QEMU 启动 一条龙（WSL /root/freebsd-src）
# 用法:
#   ./build.sh kernel      # 只编内核（KERNCONF=MINIMAL）
#   ./build.sh world       # 只编用户态 world（约 30-60 分钟）
#   ./build.sh both        # world + kernel（默认）
#   ./build.sh iso         # kernel + 重打 ISO
#   ./build.sh run         # kernel + 重打 ISO + QEMU 启动到 login（全链路）
#   ./build.sh rescue      # 恢复模式：单用户 ISO 启动 + 挂上现有盘，进 shell 自己修盘
#   ./build.sh qemu        # 只启动当前 ISO（不编译不打包）
#   ./build.sh rebuild     # 清理(rm obj)后全量重编 world+kernel（默认只用一半 CPU）
#   ./build.sh rebuild-world  # 只 world：cleanworld + buildworld（默认半 CPU）
#   ./build.sh rebuild-kernel # 只 kernel：cleankernel + buildkernel（默认半 CPU）
# 可覆盖环境变量:
#   KERNCONF=GENERIC  JOBS=8  MAKEOBJDIRPREFIX=$SRC/00_jam/tmp/obj
#   CROSS_BINDIR=/usr/lib/llvm-21/bin  SRC=/root/freebsd-src
#   （rebuild* 未显式设 JOBS 时默认 nproc/2 只用一半 CPU，防机器卡死；JOBS=N 显式传则覆盖）
#   NO_BEAR=1 关闭 bear（默认所有编译都带 bear --append 生成 compile_commands.json）
#   BMAKE_SKIP_TESTS=0 强制跑 bmake unit-tests（默认 1=跳过：交叉宿主 Linux 上
#   deptgt-interrupt/export/var-op-shell 依赖 FreeBSD sh 语义会误报失败）
#   临时产物（obj/isobuild/日志）默认放 $SRC/00_jam/tmp/，WSL 重启不丢
#   （原 /tmp、/var/tmp 重启清空，构建缓存全废）
#   ISO_SRC=（源头 ISO，默认 $SRC/00_jam/tmp/freebsd-minimal.iso，WSL 内自洽）
#   KERNEL=（内核产物，默认按 MAKEOBJDIRPREFIX 推导）
#   GDB_WAIT=（1 = run-sd/dev 暂停等 gdb 连接；默认直接启动）  LOG=（启动日志路径）
set -euo pipefail

SRC="${SRC:-/root/freebsd-src}"
cd "$SRC"

# 临时产物目录（obj / isobuild / 日志），WSL 重启保留
mkdir -p "$SRC/00_jam/tmp"

# ---------- 编译参数 ----------
USER_JOBS="${JOBS:-}"          # 用户显式传入的 JOBS（未设则为空；rebuild* 用它判断是否覆盖半 CPU）
JOBS="${JOBS:-$(nproc)}"
KERNCONF="${KERNCONF:-MINIMAL}"
TARGET="${TARGET:-amd64}"
TARGET_ARCH="${TARGET_ARCH:-amd64}"
MAKEOBJDIRPREFIX="${MAKEOBJDIRPREFIX:-$SRC/00_jam/tmp/obj}"
CROSS_BINDIR="${CROSS_BINDIR:-/usr/lib/llvm-21/bin}"
export MAKEOBJDIRPREFIX
# bmake 自举（obj 清空后首次 buildworld/buildkernel 触发）在非 FreeBSD 宿主跳过 unit-tests
export BMAKE_SKIP_TESTS="${BMAKE_SKIP_TESTS:-1}"
MAKE=(tools/build/make.py -j "$JOBS" TARGET="$TARGET" TARGET_ARCH="$TARGET_ARCH" --cross-bindir="$CROSS_BINDIR" MK_DEPEND_CLEANUP=no)
CC_JSON="$SRC/00_jam/tmp/compile_commands.json"   # bear 输出统一放工作区（源码树根不留大文件）
KERNEL_OUT="$MAKEOBJDIRPREFIX/root/freebsd-src/$TARGET.$TARGET_ARCH/sys/$KERNCONF/kernel"

# ---------- 编译执行器：默认带 bear（--append）生成 compile_commands.json（NO_BEAR=1 关闭） ----------
use_bear() {   # 返回 0=用 bear；1=不用（NO_BEAR=1 或 bear 未安装）
  if [ "${NO_BEAR:-0}" = "1" ]; then
    return 1
  fi
  command -v bear >/dev/null 2>&1
}

BEAR_OUT_OPT=""   # 探测结果缓存：bear 3.x 只认 --output，2.x 用 -o
BEAR_PRELOAD=""   # 探测结果缓存：bear 3.x 有 --force-preload（见下说明）
# bear 3.x 默认用 wrapper 模式：往 PATH 前面塞一个目录，里面有 ar/as/cc/clang/ld/nm/ranlib/
# strip/yacc... 等 31 个同名假工具。FreeBSD 交叉构建正好要用这些名字（且它们并不在宿主 PATH 里，
# 而是 WORLDTMP/legacy 或交叉工具链里的），wrapper 找不到真身就报
#   "wrapper: failed with: No such file or directory"
# 于是 _cleanobj_fast_depend_hack 等目标直接失败。
# --force-preload 改用 LD_PRELOAD 拦截 exec*，完全不动 PATH，对构建系统透明。
bear_probe() {
  if [ -n "$BEAR_OUT_OPT" ]; then
    return 0
  fi
  local help
  help="$(bear --help 2>&1 || true)"
  case "$help" in
    *--output*) BEAR_OUT_OPT="--output" ;;
    *)          BEAR_OUT_OPT="-o" ;;
  esac
  case "$help" in
    *--force-preload*) BEAR_PRELOAD="--force-preload" ;;
    *)                 BEAR_PRELOAD="" ;;
  esac
}

run_cmd() {  # 用法: run_cmd <完整命令...>；用 bear 包裹（NO_BEAR=1 或无 bear 时直接执行）
  if use_bear; then
    bear_probe
    local opts=(--append)
    if [ -n "$BEAR_PRELOAD" ]; then
      opts+=("$BEAR_PRELOAD")
    fi
    opts+=("$BEAR_OUT_OPT" "$CC_JSON")
    bear "${opts[@]}" -- "$@"
  else
    if [ "${NO_BEAR:-0}" != "1" ]; then
      echo "警告: 未找到 bear（apt-get install -y bear），跳过 compile_commands.json" >&2
    fi
    "$@"
  fi
}

run_build() {  # 用法: run_build <make 目标参数...>（用全局 MAKE 数组）
  run_cmd "${MAKE[@]}" "$@"
}

# ---------- ISO 打包参数 ----------
ISOBUILD="$SRC/00_jam/tmp/isobuild"
ISO_SRC="${ISO_SRC:-$SRC/00_jam/tmp/freebsd-minimal.iso}"   # 源头 ISO（WSL 内 00_jam，重启不丢；不再依赖 Windows 盘）
KERNEL="${KERNEL:-$KERNEL_OUT}"
LOADERCONF="${LOADERCONF:-$SRC/00_jam/out/loader.conf.iso}"
OUT="$ISOBUILD/freebsd-minimal-bios.iso"

# ---------- QEMU 参数 ----------
LOG="${LOG:-$SRC/00_jam/tmp/boot-bios.log}"

do_kernel() {
  echo "==> [kernel] buildkernel KERNCONF=$KERNCONF (MAKEOBJDIRPREFIX=$MAKEOBJDIRPREFIX, bear=$([ "${NO_BEAR:-0}" = "1" ] && echo off || echo on))"
  run_build KERNCONF="$KERNCONF" buildkernel
  echo "==> [kernel] 产物: $KERNEL_OUT"
}

do_world() {
  echo "==> [world] buildworld（lib + bin/sbin/usr.bin/usr.sbin + rescue，约 30-60 分钟，bear=$([ "${NO_BEAR:-0}" = "1" ] && echo off || echo on))"
  run_build buildworld
  echo "==> [world] 完成。产物在 obj 树，rootfs 需另跑: make installworld DESTDIR=/xxx"
}

# ---------- 清理后重编（默认半 CPU；JOBS= 覆盖） ----------
#   rebuild        全量：rm obj + buildworld + buildkernel
#   rebuild-world  只 world：cleanworld + buildworld（保留 kernel obj）
#   rebuild-kernel 只 kernel：cleankernel + buildkernel（保留 world obj）
do_rebuild() {
  local RB_JOBS="${USER_JOBS:-$(( $(nproc) / 2 ))}"
  local RB_MAKE=(tools/build/make.py -j "$RB_JOBS" TARGET="$TARGET" TARGET_ARCH="$TARGET_ARCH" --cross-bindir="$CROSS_BINDIR" MK_DEPEND_CLEANUP=no)
  echo "==> [rebuild] 清理后全量重编 world + kernel（JOBS=$RB_JOBS，bear=$([ "${NO_BEAR:-0}" = "1" ] && echo off || echo on)）"
  echo "==> [rebuild 1/3] 清理旧产物: rm -rf $MAKEOBJDIRPREFIX"
  [ "$MAKEOBJDIRPREFIX" = "/" ] && { echo "错误: MAKEOBJDIRPREFIX 不能是 /"; exit 1; }
  rm -rf "$MAKEOBJDIRPREFIX"
  mkdir -p "$MAKEOBJDIRPREFIX"   # make.py 要求 MAKEOBJDIRPREFIX 目录存在，删掉后必须重建
  echo "==> [rebuild 2/3] buildworld（完整用户态，约 30-60 分钟）"
  run_cmd "${RB_MAKE[@]}" buildworld
  echo "==> [rebuild 3/3] buildkernel（KERNCONF=$KERNCONF，完整内核）"
  run_cmd "${RB_MAKE[@]}" KERNCONF="$KERNCONF" buildkernel
  echo "==> [rebuild] 完成。内核产物: $KERNEL_OUT"
}

do_rebuild_world() {
  local RB_JOBS="${USER_JOBS:-$(( $(nproc) / 2 ))}"
  local RB_MAKE=(tools/build/make.py -j "$RB_JOBS" TARGET="$TARGET" TARGET_ARCH="$TARGET_ARCH" --cross-bindir="$CROSS_BINDIR" MK_DEPEND_CLEANUP=no)
  echo "==> [rebuild-world] cleanworld + buildworld（JOBS=$RB_JOBS，只重编 world，kernel obj 保留）"
  run_cmd "${RB_MAKE[@]}" cleanworld
  run_cmd "${RB_MAKE[@]}" buildworld
  echo "==> [rebuild-world] 完成"
}

do_rebuild_kernel() {
  local RB_JOBS="${USER_JOBS:-$(( $(nproc) / 2 ))}"
  local RB_MAKE=(tools/build/make.py -j "$RB_JOBS" TARGET="$TARGET" TARGET_ARCH="$TARGET_ARCH" --cross-bindir="$CROSS_BINDIR" MK_DEPEND_CLEANUP=no)
  echo "==> [rebuild-kernel] cleankernel + buildkernel（JOBS=$RB_JOBS，只重编内核，world obj 保留）"
  run_cmd "${RB_MAKE[@]}" KERNCONF="$KERNCONF" cleankernel
  run_cmd "${RB_MAKE[@]}" KERNCONF="$KERNCONF" buildkernel
  echo "==> [rebuild-kernel] 完成。内核产物: $KERNEL_OUT"
}

do_iso() {
  echo "==> [iso] 重打 ISO（KERNEL=$KERNEL）"
  [ -f "$ISO_SRC" ] || { echo "错误: 原 ISO 不存在: $ISO_SRC（用 ISO_SRC= 指定）"; exit 1; }
  [ -f "$KERNEL" ] || { echo "错误: 内核不存在: $KERNEL（先跑 build.sh kernel）"; exit 1; }

  echo "==> [iso 1/5] 解包原 ISO（tar 保留符号链接）"
  cd "$ISOBUILD"
  rm -rf isosrc && mkdir isosrc
  mkdir -p "$ISOBUILD/mnt"; mount -o loop,ro "$ISO_SRC" "$ISOBUILD/mnt"
  cd "$ISOBUILD/mnt" && tar cf - . | tar xf - -C "$ISOBUILD/isosrc"
  cd / && umount "$ISOBUILD/mnt"

  echo "==> [iso 2/5] 合并 /rescue 硬链接（crunch 组同内容，md5 分组）"
  cd "$ISOBUILD/isosrc/rescue"
  declare -A seen
  for f in *; do
    [ -f "$f" ] || continue
    h=$(md5sum "$f" | awk '{print $1}')
    if [ -n "${seen[$h]:-}" ]; then
      rm -f "$f"; ln "${seen[$h]}" "$f"
    else
      seen[$h]="$f"
    fi
  done
  cd "$ISOBUILD"

  echo "==> [iso 3/5] 还原 cdboot（防 boot-info-table 污染，md5 应为 7e4fc6f6...）"
  isoinfo -R -x /boot/cdboot -i "$ISO_SRC" 2>/dev/null > "$ISOBUILD/isosrc/boot/cdboot"
  md5sum "$ISOBUILD/isosrc/boot/cdboot"

  echo "==> [iso 3.5/5] 清理 Rock Ridge 迁移目录（重打包同名冲突）"
  rm -rf "$ISOBUILD/isosrc/rr_moved" "$ISOBUILD/isosrc/.rr_moved"

  echo "==> [iso 4/5] 注入新内核 + loader.conf"
  cp "$KERNEL" "$ISOBUILD/isosrc/boot/kernel/kernel"
  cp "$LOADERCONF" "$ISOBUILD/isosrc/boot/loader.conf"

  echo "==> [iso 5/5] genisoimage 打包（单 BIOS El Torito，无 -boot-info-table）"
  genisoimage -J -R -V FREEBSD_MINIMAL \
    -b boot/cdboot -no-emul-boot -boot-load-size 4 \
    -o "$OUT" "$ISOBUILD/isosrc/" 2>&1 | tail -2
  ls -la "$OUT"
  isoinfo -d -i "$OUT" | grep -iE 'eltorito|boot catalog' | head -3
}

# ---------- release：Linux 从编译产物做 ISO（官方等价；不依赖老 ISO 骨架） ----------
#   buildworld → installworld DESTDIR=新根树 → installkernel → distribution
#   → 补 fstab/loader.conf → genisoimage 打包（BIOS El Torito）
#   全部 Linux 侧完成；产物可 QEMU 单飞启动到 login（已实测）。
do_release() {
  local ROOT="${ROOT:-$SRC/00_jam/tmp/root}"                       # installworld DESTDIR 新根树
  local WORLD_ISO="${WORLD_ISO:-$ISOBUILD/freebsd-from-world.iso}" # 产物
  echo "==> [release] Linux 从编译产物做 ISO（ROOT=$ROOT）"
  echo "==> [release 1/5] installworld DESTDIR=$ROOT（造全新根树：bin/boot/etc/lib/rescue/... + METALOG）"
  "${MAKE[@]}" installworld DESTDIR="$ROOT"
  echo "==> [release 2/5] installkernel DESTDIR=$ROOT KERNCONF=$KERNCONF（装 /boot/kernel/kernel）"
  "${MAKE[@]}" KERNCONF="$KERNCONF" installkernel DESTDIR="$ROOT"
  echo "==> [release 3/5] distribution DESTDIR=$ROOT（生成 /etc/rc、passwd、master.passwd、group、ttys 等运行时文件）"
  "${MAKE[@]}" distribution DESTDIR="$ROOT"
  echo "==> [release 4/5] 补 fstab + loader.conf（单飞 cd9660 根，QEMU 串口）"
  printf '/dev/iso9660/FREEBSD_MINIMAL / cd9660 ro 0 0\n' > "$ROOT/etc/fstab"
  printf 'vfs.root.mountfrom="cd9660:/dev/cd0"\nconsole="comconsole"\nautoboot_delay=1\n' > "$ROOT/boot/loader.conf"
  echo "==> [release 5/5] genisoimage 打包（BIOS El Torito cdboot）"
  genisoimage -J -R -V FREEBSD_MINIMAL \
    -b boot/cdboot -no-emul-boot -boot-load-size 4 \
    -o "$WORLD_ISO" "$ROOT/" 2>&1 | tail -2
  ls -la "$WORLD_ISO"
  echo "==> [release] 完成: $WORLD_ISO（启动: build.sh qemu ISO=$WORLD_ISO）"
}

# ---------- 单用户引导 ISO：从 root 树临时打包（boot_single=YES，做盘用；不动正式 ISO） ----------
make_single_iso() {
  ROOT="${ROOT:-$SRC/00_jam/tmp/root}"
  SINGLE_ISO="$ISOBUILD/single-boot.iso"
  [ -d "$ROOT/boot" ] || { echo "错误: root 树不存在: $ROOT（先跑 build.sh release）"; exit 1; }
  echo "==> [single-iso] 从 root 树造单用户引导 ISO（boot_single=YES，约 2-3 分钟）"
  cp "$ROOT/boot/loader.conf" "$ISOBUILD/loader.conf.bak"
  printf 'vfs.root.mountfrom="cd9660:/dev/cd0"\nconsole="comconsole"\nautoboot_delay=1\nboot_single="YES"\n' > "$ROOT/boot/loader.conf"
  genisoimage -J -R -V FREEBSD_MINIMAL \
    -b boot/cdboot -no-emul-boot -boot-load-size 4 \
    -o "$SINGLE_ISO" "$ROOT/" 2>&1 | tail -2
  cp "$ISOBUILD/loader.conf.bak" "$ROOT/boot/loader.conf"   # 恢复正式 loader.conf
  ls -la "$SINGLE_ISO"
}
do_qemu() {
  echo "==> [qemu] 启动 $ISO（NO_PAUSE=${NO_PAUSE:-0}；设 NO_PAUSE=1 不暂停等 gdb 1234）"
  [ -f "$ISO" ] || { echo "错误: ISO 不存在: $ISO（先跑 build.sh iso）"; exit 1; }
  local kvm=()
  [ -e /dev/kvm ] && kvm+=(-enable-kvm)
  local gdbopts=()
  [ -n "${GDB_WAIT:-}" ] && gdbopts=(-s -S)
  exec qemu-system-x86_64 "${gdbopts[@]}" -m 2G "${kvm[@]}" \
    -nic none \
    -drive file="$ISO",format=raw,media=cdrom,readonly=on,if=none,id=cdrom0 \
    -device ich9-ahci,id=ahci \
    -device ide-cd,bus=ahci.0,drive=cdrom0 \
    -boot order=d \
    -nographic "$@" 2>&1 | tee "$LOG"
}

# ---------- 单盘启动：只挂 singledisk.img（GPT 引导根盘） ----------
do_qemu_singledisk() {
  local SD="${SINGLEDISK:-$SRC/00_jam/tmp/singledisk.img}"
  echo "==> [run-sd] 单盘启动 $SD（无 ISO；设 SINGLEDISK= 可指定盘）"
  [ -f "$SD" ] || { echo "错误: 单盘不存在: $SD（先跑 build.sh singledisk）"; exit 1; }
  local kvm=()
  [ -e /dev/kvm ] && kvm+=(-enable-kvm)
  local gdbopts=()
  [ -n "${GDB_WAIT:-}" ] && gdbopts=(-s -S)
  exec qemu-system-x86_64 "${gdbopts[@]}" -m 2G "${kvm[@]}" \
    -nic none \
    -drive file="$SD",format=raw,if=none,id=disk0 \
    -device ich9-ahci,id=ahci -device ide-hd,bus=ahci.0,drive=disk0 \
    -boot order=c \
    -nographic "$@" 2>&1 | tee "$SRC/00_jam/tmp/singledisk-run.log"
}
# ---------- 做盘：QEMU 内 newfs/mount/tar，生成可写 UFS 根盘 ----------
do_rootfs() {
  local ROOTFS="${ROOTFS:-$SRC/00_jam/tmp/rootfs.img}"
  local LC_SINGLE="$SRC/00_jam/out/loader.conf.single"
  echo "==> [rootfs] 生成 4G 可写 UFS 根盘: $ROOTFS"
  echo "==> [rootfs 1/4] 造单用户引导 ISO（root 树 + boot_single）"
  make_single_iso
  echo "==> [rootfs 2/4] 造 4G 空盘"
  rm -f "$ROOTFS"
  dd if=/dev/zero of="$ROOTFS" bs=1M count=4096 status=none
  echo "==> [rootfs 3/4] QEMU 单用户做盘（等 shell 后逐条喂命令）"
  (
    sleep 100; for i in 1 2 3 4 5; do echo; sleep 3; done; sleep 6
    echo "newfs /dev/ada0"; sleep 10
    echo "mount -t ufs /dev/ada0 /mnt"; sleep 5
    echo "cd / && /usr/bin/tar cf - --exclude=dev --exclude=proc --exclude=mnt --exclude=tmp --exclude=usr/tests . | /usr/bin/tar xf - -C /mnt"; sleep 300
    echo "mkdir -p /mnt/dev /mnt/proc /mnt/mnt /mnt/tmp"; sleep 4
    echo "echo '/dev/ada0 / ufs rw 0 0' > /mnt/etc/fstab"; sleep 3
    echo "echo 'devfs /dev devfs rw 0 0' >> /mnt/etc/fstab"; sleep 3
    echo "sync"; sleep 5
    echo "halt -p"; sleep 600
  ) | qemu-system-x86_64 -m 2G -nographic -no-reboot \
    -drive file="$SINGLE_ISO",format=raw,media=cdrom,readonly=on,if=none,id=cdrom0 \
    -device ich9-ahci,id=ahci -device ide-cd,bus=ahci.0,drive=cdrom0 \
    -drive file="$ROOTFS",format=raw,if=none,id=disk0 -device ide-hd,bus=ahci.1,drive=disk0 \
    -boot order=d 2>&1 | tee "$SRC/00_jam/tmp/mkrootfs.log"
  echo "==> [rootfs 4/4] 完成（正式 loader.conf 已由 make_single_iso 自动恢复）"
  echo "==> [rootfs] 完成: $ROOTFS（可写 UFS 根，配 ISO 双盘启动）"
}

# ---------- 恢复模式：单用户 ISO 启动 + 挂上现有盘，进 shell 自己修 ----------
#   盘只挂上去，不做任何自动拷贝/修改；修复命令全部由你自己在 shell 里敲
#   RESCUE_DISK= 目标盘（默认 singledisk.img）
#   RESCUE_PART= 盘内根分区（默认 ada0p2；裸 UFS 的 rootfs.img 要填 ada0）
#   RESCUE_ISO=  引导 ISO（默认用 root 树临时造的单用户 ISO；也可指向现成 ISO）
do_rescue() {
  local DISK="${RESCUE_DISK:-${SINGLEDISK:-$SRC/00_jam/tmp/singledisk.img}}"
  local PART="${RESCUE_PART:-ada0p2}"
  local RISO="${RESCUE_ISO:-}"
  [ -f "$DISK" ] || { echo "错误: 目标盘不存在: $DISK（用 RESCUE_DISK= 指定，或先跑 build.sh rootfs / singledisk）"; exit 1; }

  if [ -z "$RISO" ]; then
    make_single_iso          # boot_single=YES → 直接进 root shell，不用登录
    RISO="$SINGLE_ISO"
  fi
  [ -f "$RISO" ] || { echo "错误: 引导 ISO 不存在: $RISO（默认 ISO 需先跑 build.sh release 造 root 树）"; exit 1; }

  echo "==> [rescue] 恢复模式：ISO 启动 + 挂上 $DISK（不自动改盘）"
  echo "==> [rescue] 引导 ISO : $RISO"
  echo "==> [rescue] 目标盘   : $DISK"
  echo "==> [rescue] 进 shell 后自己来，常用命令："
  echo "      mount -t ufs /dev/$PART /mnt    # 挂载待修的盘"
  echo "      ls /mnt                         # 确认挂对了"
  echo "      ... 你的修复操作 ..."
  echo "      sync; umount /mnt; halt -p      # 收尾并关机"
  echo "==> [rescue] 退出 QEMU: Ctrl-A 然后按 x"

  local kvm=()
  [ -e /dev/kvm ] && kvm+=(-enable-kvm)
  exec qemu-system-x86_64 -m 2G "${kvm[@]}" \
    -nic none \
    -drive file="$RISO",format=raw,media=cdrom,readonly=on,if=none,id=cdrom0 \
    -device ich9-ahci,id=ahci -device ide-cd,bus=ahci.0,drive=cdrom0 \
    -drive file="$DISK",format=raw,if=none,id=disk0 -device ide-hd,bus=ahci.1,drive=disk0 \
    -boot order=d -nographic 2>&1 | tee "$SRC/00_jam/tmp/rescue.log"
}

# ---------- 单盘：GPT 引导盘（分区+bootcode+newfs+复制根，可直接单盘启动） ----------
do_singledisk() {
  local SINGLEDISK="${SINGLEDISK:-$SRC/00_jam/tmp/singledisk.img}"
  local DISKMB="${DISKMB:-8192}"   # 盘大小 MB（默认 8G，防满）
  local LC_SINGLE="$SRC/00_jam/out/loader.conf.single"
  echo "==> [singledisk] 生成 ${DISKMB}M GPT 可引导根盘: $SINGLEDISK（官方布局: boot 分区 + ufs 分区）"
  echo "==> [singledisk 1/5] 造单用户引导 ISO（root 树 + boot_single）"
  make_single_iso
  echo "==> [singledisk 2/5] 造 ${DISKMB}M 空盘"
  rm -f "$SINGLEDISK"
  dd if=/dev/zero of="$SINGLEDISK" bs=1M count="$DISKMB" status=none
  echo "==> [singledisk 3/5] QEMU 单用户做盘（freebsd-boot + freebsd-ufs 双分区 + bootcode + newfs + tar）"
  (
    sleep 100; for i in 1 2 3 4 5; do echo; sleep 3; done; sleep 6
    echo "gpart destroy -F ada0"; sleep 3
    echo "gpart create -s gpt ada0"; sleep 3
    echo "gpart add -t freebsd-boot -s 512K -i 1 ada0"; sleep 3
    echo "gpart add -t freebsd-ufs -i 2 ada0"; sleep 3
    echo "gpart bootcode -b /boot/pmbr ada0"; sleep 3
    echo "gpart bootcode -p /boot/gptboot -i 1 ada0"; sleep 3
    echo "newfs /dev/ada0p2"; sleep 10
    echo "mount -t ufs /dev/ada0p2 /mnt"; sleep 5
    echo "cd / && /usr/bin/tar cf - --exclude=dev --exclude=proc --exclude=mnt --exclude=tmp --exclude=usr/tests . | /usr/bin/tar xf - -C /mnt"; sleep 300
    echo "mkdir -p /mnt/dev /mnt/proc /mnt/mnt /mnt/tmp"; sleep 4
    echo "echo '/dev/ada0p2 / ufs rw 0 0' > /mnt/etc/fstab"; sleep 3
    echo "echo 'devfs /dev devfs rw 0 0' >> /mnt/etc/fstab"; sleep 3
    echo "echo 'vfs.root.mountfrom=\"ufs:/dev/ada0p2\"' > /mnt/boot/loader.conf"; sleep 3
    echo "echo 'console=\"comconsole\"' >> /mnt/boot/loader.conf"; sleep 3
    echo "echo 'autoboot_delay=1' >> /mnt/boot/loader.conf"; sleep 3
    echo "sync"; sleep 5
    echo "halt -p"; sleep 600
  ) | qemu-system-x86_64 -m 2G -nographic -no-reboot \
    -drive file="$SINGLE_ISO",format=raw,media=cdrom,readonly=on,if=none,id=cdrom0 \
    -device ich9-ahci,id=ahci -device ide-cd,bus=ahci.0,drive=cdrom0 \
    -drive file="$SINGLEDISK",format=raw,if=none,id=disk0 -device ide-hd,bus=ahci.1,drive=disk0 \
    -boot order=d 2>&1 | tee "$SRC/00_jam/tmp/singledisk-make.log"
  echo "==> [singledisk 4/5] 完成（正式 loader.conf 已由 make_single_iso 自动恢复）"
  echo "==> [singledisk 5/5] 单盘启动验证（无 ISO，boot order=c）"
  timeout 180 qemu-system-x86_64 -m 2G -nographic -no-reboot \
    -drive file="$SINGLEDISK",format=raw,if=none,id=disk0 \
    -device ich9-ahci,id=ahci -device ide-hd,bus=ahci.0,drive=disk0 \
    -boot order=c 2>&1 | tee "$SRC/00_jam/tmp/singledisk-boot.log"
  if grep -q "login:" "$SRC/00_jam/tmp/singledisk-boot.log"; then
    echo "==> [singledisk] 成功：单盘启动到 login"
  else
    echo "==> [singledisk] 警告：未检测到 login，看 singledisk-boot.log"
  fi
}
# ---------- 产物清单：构建类命令跑完后统一打印地址 ----------
#   注意: run / qemu / run-sd / rescue 用 exec 拉起 QEMU，进程被替换不会走到这里
#   产物分三处: obj 树=编译中间产物(kernel/world 默认落点)，root 树=installworld DESTDIR(需跑 release)
#   isobuild=各类 ISO，tmp 根下=盘镜像与日志
print_artifacts() {
  local label path mark
  echo ""
  echo "==> [产物清单] 统一落在 $SRC/00_jam/tmp/ 下（工作区内，WSL 重启不丢）"
  echo "    obj 树 = 编译中间产物（kernel/world 的默认落点）；root 树 = installworld DESTDIR（跑过 release 才有）"
  while IFS='|' read -r label path; do
    [ -n "$path" ] || continue
    if [ -e "$path" ]; then mark="有"; else mark="无"; fi
    printf '    [%s] %s: %s\n' "$mark" "$label" "$path"
  done <<ARTIFACTS
obj 树（buildworld/buildkernel 中间产物）|$MAKEOBJDIRPREFIX
内核（buildkernel 产物）|$KERNEL_OUT
根树（installworld DESTDIR）|${ROOT:-$SRC/00_jam/tmp/root}
compile_commands.json|$CC_JSON
重打 ISO（build.sh iso）|$OUT
release ISO（build.sh release）|$ISOBUILD/freebsd-from-world.iso
单用户 ISO（make_single_iso）|$ISOBUILD/single-boot.iso
rootfs 盘（build.sh rootfs）|${ROOTFS:-$SRC/00_jam/tmp/rootfs.img}
单盘（build.sh singledisk）|${SINGLEDISK:-$SRC/00_jam/tmp/singledisk.img}
ARTIFACTS
}

cmd="${1:-both}"
case "$cmd" in
  kernel) do_kernel ;;
  world)  do_world ;;
  both)   do_world; do_kernel ;;
  rebuild) do_rebuild ;;
  rebuild-world) do_rebuild_world ;;
  rebuild-kernel) do_rebuild_kernel ;;
  iso)    do_kernel; do_iso ;;
  release) do_release ;;
  rootfs) do_kernel; do_rootfs ;;
  singledisk) do_kernel; do_singledisk ;;
  rescue) do_rescue ;;
  run)    do_kernel; do_iso; ISO="$OUT" NO_PAUSE=1 do_qemu ;;
  run-sd) do_qemu_singledisk ;;
  qemu)   ISO="${ISO:-$OUT}" do_qemu ;;
  *)
    echo "用法: $0 [kernel|world|both|iso|rootfs|rescue|run|qemu]"
    echo "  kernel  只编内核（KERNCONF=MINIMAL）"
    echo "  world   只编用户态"
    echo "  both    world + kernel（默认）"
    echo "  rebuild 清理(rm obj)后全量重编 world+kernel（默认只用一半 CPU，JOBS= 可覆盖）"
    echo "  rebuild-world  只 world：cleanworld + buildworld（保留 kernel obj，默认半 CPU）"
    echo "  rebuild-kernel 只 kernel：cleankernel + buildkernel（保留 world obj，默认半 CPU）"
    echo "  （所有编译默认带 bear --append 生成 compile_commands.json；NO_BEAR=1 关闭）"
    echo "  iso     kernel + 重打 ISO（正式 loader.conf）"
  echo "  release Linux 从编译产物做 ISO（installworld 新根树+genisoimage，不依赖老 ISO）"
    echo "  rootfs  kernel + 做 4G UFS 根盘（QEMU 内 newfs/tar；完成后 ISO 恢复正式）"
    echo "  singledisk kernel + 做 ${DISKMB:-8}G GPT 可引导根盘（分区+bootcode，单盘直接启动；DISKMB= 可改大小）"
    echo "  rescue  恢复模式：单用户 ISO 启动 + 挂上现有盘，进 shell 自己修（不自动改盘）"
    echo "          RESCUE_DISK=目标盘（默认 singledisk.img）  RESCUE_PART=分区（默认 ada0p2，裸 UFS 填 ada0）"
    echo "          RESCUE_ISO=引导 ISO（默认用 root 树造的单用户 ISO）"
    echo "  run     kernel + 重打 ISO + 启动（全链路，NO_PAUSE=1）"
    echo "  qemu    只启动当前 ISO（ISO=/path 可指定）"
  echo "  run-sd  只启动 singledisk.img（默认不暂停；GDB_WAIT=1 暂停等 gdb）"
    echo "双盘验证:  qemu-system ... -drive file=ISO -drive file=rootfs.img（loader.conf 指 ufs:/dev/ada0）"
    exit 1 ;;
esac
print_artifacts
