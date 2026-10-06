#!/bin/bash
# 第一次做盘：QEMU 启动 ISO(单用户) + 空盘，newfs/mount/cp 根到 /dev/ada0
ISOBUILD=/root/freebsd-src/00_jam/tmp/isobuild
ISO=$ISOBUILD/freebsd-minimal-bios.iso
ROOTFS=/root/freebsd-src/00_jam/tmp/rootfs.img
LOG=/root/freebsd-src/00_jam/tmp/mkrootfs.log

rm -f "$ROOTFS"; dd if=/dev/zero of=$ROOTFS bs=1M count=4096 status=none

# 等单用户 shell(约55s)，随后逐条喂命令
(
sleep 55
echo
sleep 5
echo "newfs /dev/ada0"
sleep 10
echo "mount -t ufs /dev/ada0 /mnt"
sleep 5
echo "cd / && /usr/bin/tar cf - --exclude=dev --exclude=proc --exclude=mnt --exclude=tmp --exclude=usr/tests . | /usr/bin/tar xf - -C /mnt"
sleep 120
echo "mkdir -p /mnt/dev /mnt/proc /mnt/mnt /mnt/tmp"
sleep 4
echo "echo '/dev/ada0 / ufs rw 0 0' > /mnt/etc/fstab"
sleep 3
echo "echo 'devfs /dev devfs rw 0 0' >> /mnt/etc/fstab"
sleep 3
echo "sync"
sleep 5
echo "halt -p"
sleep 15
) | qemu-system-x86_64 -m 2G \
  -drive file=$ISO,format=raw,media=cdrom,readonly=on,if=none,id=cdrom0 \
  -device ich9-ahci,id=ahci \
  -device ide-cd,bus=ahci.0,drive=cdrom0 \
  -drive file=$ROOTFS,format=raw,if=none,id=disk0 \
  -device ide-hd,bus=ahci.1,drive=disk0 \
  -boot order=d -nographic 2>&1 | tee $LOG
echo "QEMU_DONE=$?"
tail -8 $LOG