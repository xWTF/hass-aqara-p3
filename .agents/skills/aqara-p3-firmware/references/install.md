# 安装与恢复检查

本文描述历史单 rootfs 安装模板。公开内核/rootfs 配套安装使用[公共安装指南](../../../../firmware/README.md)及 `firmware/p3_firmware.py`；不能混用两种流程的暂存目录、计划和 expected 文件。

## 范围与准备

本 skill 提供离线安装包生成器，没有自动连接设备的刷机命令。之前的网络执行脚本绑定历史目录与设备状态，未作为通用工具复制。实际操作者可复用项目 `protocol/control.py` 的 `CommandSession`，或使用已建立的 Telnet shell；地址、密码来自当前用户配置，不写进 skill。

已有这次候选的明确刷写授权时，按授权继续；否则先完成可审查的候选、测试报告、哈希及安装计划，再让用户确认实际写入与重启。`--confirmed` 只是脚本防误执行参数，不是用户确认的证据。不得沿用历史 `USER_CONFIRMED_IN_THREAD`。

只读核对：

```sh
getprop persist.sys.model
cat /proc/mtd
cat /proc/cmdline
boot_ctrl show
cat /proc/sys/kernel/random/boot_id
df -k /data
sha256sum /etc/init.d/rcS /bin/fw_update
ls -ld /data/scripts /data/scripts/post_init.sh /data/aqara-p3-firmware-backup
```

记录当前内核/rootfs 全分区哈希、完整 boot_info 与其哈希。核对源固件、备份和计划一致；上次安装后的设备可能已经在 bank 0，此时附带模板应拒绝执行。不要修改模板常量绕过它，应根据新槽位重新设计和模拟。

用户分区空间应覆盖完整镜像、用户脚本、小型备份、日志和余量。历史约 20 MiB 空闲足以放约 14.88 MiB 镜像，不能把这个数当成当前值。不要为了腾空间删除设备未知数据。

## 上传与只执行一次

附带模板固定暂存目录 `/data/aqara-p3-install-v3`，备份目录 `/data/aqara-p3-firmware-backup`：

1. 上传前独占 `mkdir` 暂存目录，拒绝已有目录、符号链接或非预期父路径。若已存在，先调查来源和进行状态；不删除后重试。
2. 完整上传 `firmware.bin`、`post_init.sh`、`apply.sh`，核对每个文件长度及 SHA-256。记录安装脚本自身哈希；先用原厂 BusyBox `sh -n` 验证脚本。
3. Telnet base64 上传时，单独发送 `busybox stty -echo`，等其提示符后才发数据；stty 会丢弃排队输入，不能与 heredoc 连发。历史可用块大小 128 KiB、base64 每行 768 字符。关闭会话前恢复回显，连接中断时先检查文件完整性。
4. 独占创建暂存目录下的 `execution` 子目录作为执行预约。用有回执的 `nohup /bin/sh` 后台运行已经核对的 `apply.sh --confirmed`，stdout/stderr、退出码写在暂存目录。派发一次；超时后读日志、state、进程，不重复发送。

`apply.sh` 自动完成：型号/槽位/路径/哈希检查 → 独占备份原 rcS、boot_info 与原脚本缺失标记 → 验证备份 → 设文件 0400、目录 0500 → 独占安装用户脚本 → 写入 → 独立回读。若已有用户脚本则停止，不能覆盖；需要保留或合并脚本时，先修改计划、备份策略并验证。

脚本不重启。它只能生成 `VERIFIED_READY_TO_REBOOT` 状态；出现断线或不确定结果先检查。刷写前失败也可能已经建立备份或装好用户脚本，不能假定失败毫无副作用。

## 独立验证后重启

逐项按 `plan.json` 核对，不能只看 Success 或返回码：

- 目标 rootfs 开头 **payload 长度**的 SHA-256；长度从本次计划取得，不硬编码旧长度。
- 写入后完整 boot_info 分区与 QEMU 生成的 expected 字节一致。
- 被保留的内核/rootfs 完整哈希不变。
- 用户脚本及备份文件哈希正确；备份未被覆盖；state 已到验证完成。

所有结果符合后，在已授权的范围内发送一次有回执的延迟重启。例如设备 shell 中：

```sh
busybox nohup /bin/sh -c 'sleep 3; /bin/busybox reboot' \
  > /data/aqara-p3-install-v3/reboot.log 2>&1 < /dev/null &
echo REBOOT_QUEUED
```

收到回执再关闭连接。历史直接发 reboot 后立即关闭 Telnet，命令未实际执行。重启后验证 boot_id 改变、uptime 重置、当前 rootfs 到目标槽、rcS 和用户脚本哈希、`/tmp/post_init.log`、telnetd 自动启动，以及 HA 正常读取功率/电量、本地模式恢复。

**重启会更新 boot_info 的当前槽字段；重启后不能继续要求整个分区匹配重启前 expected 哈希。** 应检查新字段与选槽结果是否一致，内核槽是否保持。

结束后只删除已核对路径与哈希的暂存大镜像以回收空间，保留备份和日志。项目内清理同样先检查绝对路径在预期目录内；Windows 用原生 `-LiteralPath`，不拼接跨 shell 递归删除命令。

## 异常处理

- 发现校验不符、缺失备份或日志不完整：先只读诊断，不自动重刷、换槽或重启。
- 所谓“双槽”只是保留了另一个镜像，不能保证自恢复；具体限制见 [引导器行为](firmware.md)。本方案没有添加自动回滚。
- 用户确认要恢复时，依据实时槽位和原始备份重新制定回退计划，不直接把历史 boot_info 覆写回去。
- 恢复出厂清掉 `/data/scripts` 和备份目录，密码通常因 `/data/musics` 保留而留下。需要保留的备份必须另存在 PC。

不把实机断电、IR 发射、继电器操作或恢复出厂加入普通连通性验证；它们不是刷写核对的必要步骤。
