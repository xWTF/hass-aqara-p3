# Aqara P3 用户分区自启动固件

此目录提供 **4.0.4_0016 的配套内核、修改后的 rootfs 和用户启动脚本**。rootfs 唯一修改是在原厂应用启动后运行 `/data/scripts/post_init.sh`；附带脚本负责拉起原厂 Telnet。HA 本地模式仍由集成管理。

## 哪些设备可以直接安装

**通过工具全部兼容检查的设备，可以直接安装这组镜像，无需先通过米家升级到 4.0.4。** 检查依据是硬件、磁盘布局、程序指纹和当前启动状态，而非界面显示的版本号。

- 型号 `lumi.aircondition.acn05` / KTBL12LM，RTL8197F、MIPS 24Kc、MIPS32r2 小端。
- 分区表须完整匹配：128 KiB 擦除块，两个 3 MiB 内核槽、两个 16 MiB rootfs 槽，以及其余分区的名称、顺序和容量。
- 引导器须匹配已验证指纹；`boot_info` 格式、校验、当前槽和首选槽须一致，活动镜像须与其记录匹配。
- 升级工具须匹配 `manifest.json` 中的一整组指纹。目前验证了 **4.0.4** 及 **米家 4.0.1 / 系统 3.4.0** 的原厂程序。版本名称仅帮助辨认，最终以指纹为准。
- Telnet 可登录，`/data` 空间充足，用户启动脚本和本工具备份目录尚不存在。

未知版本会明确拒绝生成安装计划。遇到拒绝，应根据新的固件补充分析和测试；直接更改白名单无法建立兼容性。

### 验证程度

配套内核的有效负载与历史实机运行的内核相同；这份修改后的 rootfs 已在 P3 上刷入、重启并验证 Telnet 自启和 HA 功能。

本次“两分区安装流程”已使用新旧两版原厂 MIPS 升级程序进行 QEMU 验证，覆盖四种起始槽位组合；引导器机器码测试确认目标内核和 rootfs 的选择。**旧版直接升级的完整流程尚未做实机测试**。QEMU 使用普通文件代替 NAND，验证范围见 [VALIDATION.md](4.0.4-user-init/VALIDATION.md)；它不能模拟真实坏块、掉电和整板 Linux 启动。

## 文件含义

| 文件 | 内容 |
| --- | --- |
| `4.0.4-user-init/kernel.bin` | 原厂内核有效负载及 `fw_update` 容器头 |
| `4.0.4-user-init/rootfs.bin` | 已实机验证的自启动 rootfs 及容器头 |
| `4.0.4-user-init/post_init.sh` | 安装到用户分区的 Telnet 启动脚本 |
| `4.0.4-user-init/manifest.json` | 镜像、程序和引导器指纹 |
| `4.0.4-user-init/SHA256SUMS` | 分发文件校验值 |
| `p3_firmware.py` | 采集、离线检查、生成计划、上传暂存文件 |
| `apply.sh.in` | 设备专属安装脚本模板 |

两份 `.bin` 专供此安装流程调用原厂 `fw_update`，**不是整片 Flash 镜像**。rootfs 文件与历史已安装镜像字节一致。内核只取启动记录声明的有效长度，排除了分区尾部的历史数据；内核容器两个未使用的地址字段为零，由已验证的 `fw_update` 按镜像类型选择目标分区。勿交给其他刷机工具解释地址。

镜像的 `/data` 为空；不包含设备的 factory、boot_info、Wi-Fi 配置、配对数据或当前密码。每台设备的备份和安装脚本均在使用时单独生成，应保留在仓库忽略的 `analysis/` 中。

## 安装步骤

电脑需要 Python 3.11 及以上、可访问设备的 LAN，以及已启用的 Telnet。安装期间应暂停原厂 OTA，并在路由器上隔离设备外网、保留 LAN，避免两个升级流程同时修改 Flash。下面的 `DEVICE_IP` 替换为设备地址；密码通过终端提示输入。

### 1. 安装通信依赖并采集备份

在仓库根目录执行：

```sh
python -m pip install telnetlib3==5.0.0
python firmware/p3_firmware.py collect --host DEVICE_IP --directory analysis/p3-backup
```

采集阶段只读取设备，把 mtd0～mtd7 的逻辑分区备份流式保存到电脑；约 40 MiB，无需在设备上暂存这些备份。它们包含设备私有信息，勿上传到 Git 或分享。备份不含 NAND OOB/ECC，也不包含整个用户分区；有重要用户数据时另行保存。

目录须尚不存在。连接中断后保留残留目录供排查，新一轮采集使用新目录名。

### 2. 生成专属安装计划

```sh
python firmware/p3_firmware.py prepare --backup analysis/p3-backup --output analysis/p3-install
```

检查生成的 `plan.json`：设备标识、启动 ID、目标槽和保留槽应与这台设备一致。计划会选择当前内核和 rootfs 各自的另一个槽，允许两者当前槽号不同。

计划中的 `boot-info.after-*.bin` 仅用于离线比较，**不应直接刷入设备**。安装脚本让原厂升级程序维护 boot_info，并在每一步独立核对完整分区哈希。

### 3. 上传暂存文件

```sh
python firmware/p3_firmware.py upload --host DEVICE_IP --directory analysis/p3-install
```

此步在设备 `/data/aqara-p3-firmware-install` 中独占创建暂存目录，上传并核对四个文件。电脑工具的三个子命令均不会调用升级程序或重启设备。

设备在采集后重启、暂存目录已存在或文件校验失败时会停止。先调查已有状态，再决定下一步，勿删除备份来绕过检查。

### 4. 执行安装

核对计划后，在**设备 Telnet shell** 中执行一次：

```sh
busybox nohup /bin/sh /data/aqara-p3-firmware-install/apply.sh --confirmed > /data/aqara-p3-firmware-install/dispatch.log 2>&1 < /dev/null &
```

安装脚本重新校验设备身份、启动状态、程序、备份和镜像，独占建立执行目录。它先在 `/data/aqara-p3-firmware-backup` 保存原 boot_info、rcS 和用户脚本原本不存在的标记，再安装用户脚本、写入备用内核和 rootfs、逐项回读验证。已有备份不会被覆盖。

查看状态：

```sh
cat /data/aqara-p3-firmware-install/execution/state
```

只有 **`VERIFIED_READY_TO_REBOOT`** 表示所有检查完成。日志位于 `execution/apply.log`、`kernel.log` 和 `rootfs.log`；前置检查错误位于 `dispatch.log`。原厂程序的退出码或 `Success` 消息均不代表完整成功。

断线后先读状态与日志；有执行目录时不要再次派发。两次升级之间发生异常可能留下部分写入及首选槽变更，出现失败状态时保留现场，先分析，勿直接重启。

### 5. 重启并核对

状态确认完成后，在设备 shell 中执行一次延迟重启，看到 `REBOOT_QUEUED` 后再关闭连接：

```sh
busybox nohup /bin/sh -c 'sleep 3; /bin/busybox reboot' > /data/aqara-p3-firmware-install/reboot.log 2>&1 < /dev/null &
echo REBOOT_QUEUED
```

重新连接后核对 `boot_ctrl show` 的当前槽与计划目标一致、启动 ID 已改变，并检查：

```sh
sha256sum /etc/init.d/rcS /data/scripts/post_init.sh
cat /tmp/post_init.log
```

rcS SHA-256 应为 `b86120fb125203c887815faf4bde597ae8eabf4f363617c29e4c543af4216647`；用户脚本应与分发文件一致。随后确认 HA 的功率、电量和控制功能。

确认重启和功能正常后，可在设备 shell 删除两个暂存大文件，回收约 17 MiB，保留电脑备份、设备备份及执行日志：

```sh
rm /data/aqara-p3-firmware-install/kernel.bin /data/aqara-p3-firmware-install/rootfs.bin
```

## 后续使用

- 用户服务写在 `/data/scripts/post_init.sh`，脚本需可执行；以后修改它可直接生效于下次启动，无需再次刷机。
- 恢复出厂设置会删除用户启动脚本和设备侧安装备份；之后需重新开启 Telnet、恢复脚本。已有密码通常由原厂保留的 `/data/musics` 保存。
- 双槽保留旧内核和旧 rootfs，但原厂引导器并不保证 Linux 启动失败后自动回滚。本包未加入自动回滚。
- 自行编写的脚本、工具和修改按仓库 GPL-3.0-only 许可提供；原厂镜像中的组件保留各自原有版权和许可，仓库 LICENSE 不重新授权这些组件。
