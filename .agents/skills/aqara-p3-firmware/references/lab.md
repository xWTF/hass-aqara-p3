# 构建与离线测试

## 环境

脚本只在 PC 操作私有目录，不接受设备地址。构建及 chroot 测试用 Linux/WSL root，工作目录放原生 Linux 文件系统；输出可放被忽略的项目 `analysis/`。Windows 导出树会丢失 Unix 权限、链接和设备节点，不可作为构建输入。

已经验证的工具：SquashFS tools 4.5.1、QEMU 7.2（Debian 包版本 `7.2+dfsg-7+deb12u18+b3`）、Unicorn 2.1.4。安装 `squashfs-tools`、`qemu-user-static`，运行环境需要 `dash`、`libc-bin`（ldd）、coreutils；逆向时可另装 `binutils-mipsel-linux-gnu`。`scripts/requirements.txt` 固定 Unicorn 版本。HA Python 依赖不是离线工具的依赖。

WSL1 实测 QEMU 需要 `-B 0x100000000 -R 2147483648` 才能分配客体地址空间，脚本已携带。`qemu-system-mips` 没有这里需要的 RTL8197F 整板模型，Malta 等机型不能证明原厂内核可启动。

## 私有输入

准备同一设备、同一时间状态下的文件及对应 `.sha256` 文本（首字段为 SHA-256）：

```text
mtd0-bootloader.bin   mtd0-bootloader.sha256
mtd1-boot_info.bin    mtd1-boot_info.sha256
mtd4-linux_1.bin      mtd4-linux_1.sha256
mtd5-rootfs_1.bin     mtd5-rootfs_1.sha256
mtd6-linux_2.bin      mtd6-linux_2.sha256
mtd7-rootfs_2.bin     mtd7-rootfs_2.sha256
```

固件构建输入为完整原版 mtd7，不是 OTA 容器。实时操作前还应保存其他恢复分区并离线核对，但 factory 等私有分区不是模拟测试输入。已有历史备份可用于复现，不能用来代替下一次刷写的实时 boot_info 检查。

## 命令顺序

以下在 Linux shell 执行；将目录参数换成实际目录，输出目录必须尚不存在。`SKILL` 指向项目 `.agents/skills/aqara-p3-firmware`。脚本依赖断言，禁止 `python -O` 或 `PYTHONOPTIMIZE`；脚本会主动拒绝优化模式。

```sh
python3 -m venv /var/tmp/p3-firmware-venv
/var/tmp/p3-firmware-venv/bin/pip install -r "$SKILL/scripts/requirements.txt"
/var/tmp/p3-firmware-venv/bin/python "$SKILL/scripts/build.py" \
  "$BACKUP/mtd7-rootfs_2.bin" --work-root "$LINUX_WORK" --output "$OUTPUT"
/var/tmp/p3-firmware-venv/bin/python "$SKILL/scripts/test_runtime.py" "$OUTPUT" "$BACKUP"
/var/tmp/p3-firmware-venv/bin/python "$SKILL/scripts/test_boot_policy.py" "$OUTPUT" "$BACKUP"
/var/tmp/p3-firmware-venv/bin/python "$SKILL/scripts/prepare_install.py" "$OUTPUT" "$BACKUP"
```

`build.py` 和 `prepare_install.py` 拒绝覆盖输出；失败产物保留供检查，新一轮用新目录。不要把这些命令指向旧的成功构建目录重跑。

## 验收点

### 构建

`build-report.json`、两份 manifest、候选与原 rcS：

- 未修改重建基线的所有内容和 Unix 元数据相同。
- 候选唯一变化为 rcS 内容/大小；权限、UID/GID、mtime、链接、设备主次编号、xattrs 保持。
- 候选重复构建字节相同；保留 XZ、128 KiB 块、fragment、export 和原创建时间。
- 容器含原厂尾校验，分区留出余量；本次留量不代表所有设备坏块都可容纳。
- 原厂 MIPS BusyBox 能运行，并通过 rcS、用户脚本语法检查。

### 升级程序和启动脚本

`runtime-report.json`：11 个 updater 用例，6 个用户脚本用例。正常写入目标、回读、boot_info 内容必须符合预期；失败用例的 PASS 表示观察到预期行为（可能是原厂缺陷），不是失败自动恢复。

隔离 chroot 的 `/dev/mtd*` 都是普通文件，无宿主 `/dev`、`/proc`、`/sys` 挂载；QEMU 跑原厂 fw_update，Flash 命令为替身。rcS 使用原厂 MIPS BusyBox shell，硬件初始化与应用命令为替身。测试结束杀掉自身进程组。

### 引导器

`boot-policy-report.json`：12 个选槽情形、6 个原机器码校验向量。Unicorn hook 了 Flash、输出、保存和最终跳转，停止在内核之前。换引导器二进制后需要重新分析地址。

### 可选 Telnet 监听

```sh
/var/tmp/p3-firmware-venv/bin/python "$SKILL/scripts/test_telnetd.py" "$OUTPUT"
```

只绑定环回随机端口，登录程序替身为 `/bin/false`。原 WSL1 环境可监听但报告 `can't find free pty`，因此结果为 `LIMITED_BY_HOST_PTY`，不是登录成功。此项不能代替实机 TTY 驱动、认证和网络恢复验证。

## 安装包

`prepare_install.py` 核对三个报告、关键备份和用户脚本，生成 `install/`：`firmware.bin`、`post_init.sh`、`apply.sh`、`boot_info.expected.bin`、`plan.json`。准备过程中不连接设备，不执行 apply。

安装脚本范围固定为当前 root/kernel bank 1，目标 root bank 0；它包含严格哈希与路径检查、独占备份、用户脚本安装、原厂升级及独立回读。使用前阅读 [安装流程](install.md)，准备完成不是授权证明。修改源码后重新运行本节验证，不复制旧报告来通过检查。

## 公共内核/rootfs 配套包

上节为历史单分区模板。仓库 `firmware/` 的双分区安装流程使用另一套生成器，公开白名单、程序指纹、安装与备份方法见[公共指南](../../../../firmware/README.md)。内核容器由已验证 mtd6 的前 2101252 字节有效负载加 `cr6c` 头构成；未使用的两个地址字段为零，原厂 updater 依据类型与长度写入备用内核槽。

在仓库根目录复现配套流程；`PAIR_WORK` 和 `LEGACY_WORK` 为尚不存在的 Linux 工作目录，报告输出仍放私有目录：

```sh
python "$SKILL/scripts/test_public_bundle.py" --backup "$BACKUP" --work "$PAIR_WORK" --output "$PAIR_REPORT"
python "$SKILL/scripts/test_public_bundle.py" --backup "$BACKUP" --legacy-rootfs "$BACKUP/mtd5-rootfs_1.bin" --work "$LEGACY_WORK" --output "$LEGACY_REPORT"
python "$SKILL/scripts/test_boot_policy.py" "$OUTPUT" "$BACKUP" --paired-bundle firmware/4.0.4-user-init --output "$PAIR_BOOT_REPORT"
```

第二条命令针对已验证旧槽位的 4.0.1 / 系统 3.4.0 程序；其他源固件不因此自动获支持。两组测试共 18 项安装情形，原机器码额外检查四种 kernel/root 目标槽组合。修改兼容白名单前，应增加对应原厂程序的测试，不能仅修改版本字符串或哈希。
