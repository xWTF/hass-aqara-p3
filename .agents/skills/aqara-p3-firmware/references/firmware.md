# 固件与已知行为

## 已验证配置

型号 `lumi.aircondition.acn05`；RTL8197F、MIPS 24Kc、MIPS32 小端。实机固件标识 4.0.4_0016，米家侧版本曾显示 4.0.4_0006。字符串相同不代表二进制相同，以哈希为准。

| 输入 | SHA-256 |
| --- | --- |
| 原 rootfs bank 1，完整 16 MiB mtd7 逻辑读取 | `0361837803c25b19200264141dbaae2eaf6f30547a5f404c8cdf7a3558adcd8b` |
| 原 `/etc/init.d/rcS` | `405e831672151ea110051b4ed42eabd271a203ea9770d90dd59f37a319e9d721` |
| `/bin/fw_update` | `97efaf9b4d2a7b623ba8469ad192c1e0ef65ff036869a4911299a6c7059c0dc7` |
| 内核 bank 1，完整 mtd6 | `d3ce6d04ee741de577657303e289ca416035cb3edc3708625c645d68a26f169d` |
| 引导器，完整 mtd0 | `1d1426dbdfc98537cf5d435ec3870e9c3ede288bf273605e01d02f0ac7f24f10` |

这些是程序版本指纹。boot_info、factory、用户数据等设备相关内容及其安装记录不随 skill 分发。

| 分区 | 用途 | 容量 |
| --- | --- | --- |
| mtd0 | bootloader | 640 KiB |
| mtd1 | boot_info | 256 KiB |
| mtd2 | factory（设备身份等私有信息） | 256 KiB |
| mtd3 | bbt | 896 KiB |
| mtd4 / mtd6 | kernel bank 0 / 1 | 各 3 MiB |
| mtd5 / mtd7 | rootfs bank 0 / 1 | 各 16 MiB |
| mtd8 | 用户数据 | 约 75 MiB |

擦除块 128 KiB。以 `/proc/mtd`、`/proc/cmdline`、`boot_ctrl show` 实时读取为准。`/dev/mtdblock*` 备份是逻辑数据，不包含 NAND OOB/ECC；不能描述为完整物理 NAND 备份。

## 自启动设计

原 rcS 末尾声明 `/data/scripts/post_init.sh`，但判断分支被注释，只执行 `fw_manager.sh -r`。不能简单去掉注释：原 if/else 会让用户脚本替代原厂应用启动。

已验证修改仅为 rcS 尾部：

```sh
CUSTOM_POST_INIT=/data/scripts/post_init.sh
fw_manager.sh -r
if [ -x "${CUSTOM_POST_INIT}" ]; then
    "${CUSTOM_POST_INIT}" > /tmp/post_init.log 2>&1 &
fi
```

`assets/post_init.sh` 单独放在 `/data/scripts/post_init.sh`，0755：开启 `/sys/class/tty/tty/enable`，无 telnetd 进程时调用原厂 BusyBox telnetd。脚本不进 SquashFS，以后改用户服务无需再刷机。日志在 RAM，每次启动覆盖。云服务停用继续由 HA 集成管理，不在此固件里额外修改。

恢复出厂的 `fw_manager.sh` 清理 `/data` 时保留 `musics`，会清掉 `scripts` 及安装备份目录。`/etc/passwd`、`/etc/shadow` 指向 `/data/musics/`，rcS 只在文件缺失时复制默认值，因此正常恢复出厂保留已有密码；默认 shadow 中 admin 密码为空。恢复出厂后需要重新启用 Telnet、重新安装用户脚本。

## 容器与 boot_info

rootfs 容器头为 `struct.pack('>4sIII', b'r6cr', 0x2d0000, 0xe00000, payload_length)`，16 字节。地址字段来自原厂格式；已分析 updater 按签名与长度处理。payload 是 SquashFS 加 4 字节尾部：BE16 零与 BE16 补值，使全部 BE16 字的截断加和为零。

boot_info 前 55 字节：magic/version `7c 91 00 00`，4..5 是 6..54 的折叠反码校验；6/7 是当前 kernel/root，8/9 是首选 kernel/root；root 条目位于 24、31，格式 `>IHB`（长度、校验、失败次数），38 为 root 校验开关。**尾部校验与 boot_info 折叠校验不是同一算法。** 准备阶段保留完整分区字节，只修改已知字段并与 QEMU 正常用例输出逐字节比较。安装时由原厂 updater 更新 boot_info，不能直接刷 PC 生成的 expected 文件。

## 原厂缺陷与模拟边界

- 已测失败路径仍可退出 0；boot_info 保存失败仍可输出 Success。
- 擦除命令失败会被忽略。文件替身没有真实 NAND 的只能 1→0 编程约束，擦除失败用例“继续成功”只能用来证明缺陷。
- 输入损坏但回读一致，updater 仍可能接受；截断输入在被发现前已擦除目标槽。
- boot_info 缺失或校验错误时使用默认槽，不能依赖 updater 从当前内核参数推断槽位。
- root 校验开关关闭时跳过校验和相应失败处理。开启后镜像校验失败可换槽，但引导器进入内核前清零失败次数，Linux 死机不保证回退。boot_info 保存失败也可能继续跳内核。

引导器 gzip 从 mtd0 偏移 `0xa230` 提取；已验证机器码入口：选槽 `0xa0003650`、校验 `0xa0002654`。具体 Flash、日志、保存和内核跳转 hook 在 `test_boot_policy.py` 中，全部由引导器哈希锁定；换版本需重新定位，不能沿用地址。

## 历史验证结果

2026-09-16 曾成功从原 bank 1 刷入用户脚本版 bank 0，保留 bank 1 内核与原 rootfs；实际重启后 Telnet 自动恢复，HA 功率、电量和本地模式检查通过。完整目录比对为 2,737 个路径，唯一变化为 rcS 内容与大小；未执行额外断电冷启动测试。

这是方案的实机证据，不是当前设备状态或当前候选的测试报告。仓库本地若仍有 `analysis/firmware-lab/`，其早期 README 混有 v1/v2 旧方案，应优先查看 `output-v3-user-init/README.md` 的完成记录；不要执行旧的 `*_confirmed.py` 等一次性脚本。
