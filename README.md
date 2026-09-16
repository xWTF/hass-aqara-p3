# Aqara P3 Home Assistant 集成

通过局域网将 **Aqara P3 空调伴侣** 接入 Home Assistant 的自定义集成。提供电力数据、大金 E-Max 7 的完整红外控制协议和内置音效播放功能，使用设备原有服务完成控制。

## AI Slop Warning

This integration is 99.9% coded by GPT 6 Astra, which I believe is better than most human developers.

If you feel uncomfortable with AI-generated code, please consider not using this integration.

## 适用范围

- 设备：Aqara P3，型号 `KTBL12LM` / `lumi.aircondition.acn05`。
- 固件：控制接口已核对 `4.0.4`，米家模式。
- Home Assistant：`2026.9.0` 及以上；自动化测试使用 Core `2026.9.2`。
- 连接：设备已启用 Telnet，Home Assistant 可以通过 LAN 访问设备。
- 空调协议：**大金 E-Max 7**。其他遥控器可使用抓取工具协助适配。

运行依赖为 `telnetlib3`，由 Home Assistant 自动安装。集成可在设备隔离互联网时独立工作，不依赖米家服务器。

## 安装

### HACS 自定义仓库

[![添加到 HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=xWTF&repository=hass-aqara-p3&category=integration)

点击上方 HACS 按钮，或在 HACS 的“自定义存储库”中添加 `xWTF/hass-aqara-p3`，类别选择“集成”，然后下载。

[![添加到 Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=aqara_p3)

先通过 HACS 下载本集成并重启 Home Assistant，再点击第二个按钮添加设备，填写设备 IP 和 Telnet 密码；登录用户为 `admin`。

### 手动安装

1. 将本仓库的 `custom_components/aqara_p3` 整个文件夹复制到 `<HA 配置目录>/custom_components/`。
2. 确认 `custom_components/aqara_p3/manifest.json` 存在，然后重启 Home Assistant。
3. 在“设置 → 设备与服务 → 添加集成”中选择 **Aqara P3**。
4. 填写设备 IP 和 Telnet 密码；登录用户为 `admin`。

升级时覆盖 `aqara_p3` 文件夹并重启 Home Assistant，已有配置保留。

## 功能

### 电力与空调

- 功率、累计电量和插座供电状态。
- 开关、制冷、制热、除湿、送风、自动模式。
- 制冷 18～32°C、制热 10～30°C、自动 18～30°C，步进 0.5°C。
- 除湿温度微调 −2～+2，步进 0.5；风量自动、静音及 1～5 档。
- 上下、左右、立体摆风；强力、极速、康达气流、省电和室外机静音。
- 各模式分别保存温度、风量及除湿微调。
- 可关联外部室温实体；提供遥控器报文抓取界面。

功率和插座供电状态优先读取原厂 IR 服务维护的属性。累计电量读取原厂服务的实际积分计数，包含尚未写入缓存的部分；HA 保存累计进度，处理服务重启、计数清零和原厂整数 Wh 取整，并支持 HA 能源统计。读取间隔在“选项 → 控制与传感器设置”中调整，默认15秒、最低10秒。实际测量值的变化频率取决于设备采样和上报策略，相同读数通常不会增加新的历史状态点。

定时开关机和康达舒睡默认禁用，可在设备页按需启用。插座供电操作提供独立动作与断电确认。

恢复出厂设置后或仅配对 HomeKit 时，也可读取电力数据并使用大金原始红外控制和声音功能。原厂缓存信息保留在下载的诊断数据中，`missing_resources` 列出尚未生成的缓存。电量读取会核对原厂程序指纹，支持已验证的 4.0.4 固件。设备重启前尚未保存、且 HA 尚未读到的用电量无法追溯；出厂重置前已被清除的历史电量也无法从设备恢复。

### 声音

独立的 “声音” 子设备提供音效选择、音量、播放和停止。自动化动作可指定音效、音量、总播放次数及停止时间。

## 自动化示例

在动作编辑器选择 **“Aqara P3：播放音效”**，可直接下拉选择音效。下方 `device_id` 替换为 HA 中选定设备的 ID，也可通过编辑器选择。

```yaml
action: aqara_p3.play_sound
data:
  device_id: YOUR_DEVICE_ID
  tone: DingDong
  volume: 20
  repeat: 3
  stop_after: 10
```

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `tone` | 内置音效 | 音效实体当前选择 |
| `volume` | 本次音量，0～100 | 设备音效音量 |
| `repeat` | 总播放次数，1～100 | 1 次 |
| `stop_after` | 最大播放时长，单位秒 | 播完指定次数 |

次数或停止时间先达到时结束。新播放替换旧任务；`aqara_p3.stop_sound` 接受相同的 `device_id`，停止当前音效并取消后续循环。

## 工作方式与限制

### 本地模式

在“设置 → 设备与服务 → Aqara P3 → 选项 → 运行模式”中选择：

- **米家云服务**：使用原厂云客户端。
- **本地模式（HA 维护）**：停止云客户端及其后台重试，保留本地 IPC、红外、声音和电力数据读取。

首次启用会在设备上保存约14 KB的原始监控脚本：

```text
/data/aqara_p3/local-mode-v1/app_monitor.original.sh
```

备份首次写入后长期保留，后续切换只校验、复用。备份损坏或监控脚本与已验证版本不符时，界面会提示错误。运行时覆盖放在内存中；切回米家云服务会撤销覆盖，核对原件后恢复云客户端和原厂监控。

HA在后台每60秒维护一次所选模式。设备重启后，HA连通时会重新应用；HA尚未连通时，云客户端可能暂时运行。切换过程独立于传感器轮询，关闭选项窗口后仍会完成切换并保存选择。此功能用于停止云客户端重试，网络隔离仍由路由器管理。

本地模式的恢复以 Telnet 可连接为前提。原厂固件通过按键开启的 Telnet 在重启后可能需要重新激活。

### 实现

- HA 使用异步 Telnet 读取设备缓存，通过本地 IPC 调用原厂红外和音频服务。
- 随组件提供约 107 KiB 的静态 MIPS helper，按需复制至设备 `/tmp`，校验设备身份、固件及文件哈希后运行。电量读取通过原厂 ELF 符号表定位变量，结合进程实际加载位置只读积分计数；符号缺失或有歧义时停止读取。累计电量的结构体成员偏移仍由已验证的 4.0.4 固件布局限定。原生源码和构建脚本随仓库提供。
- 空调状态来自最近一次发送或同步的遥控器设置。外部遥控器操作需通过抓取同步；原厂缓存与空调实际状态可能不同。
- 音效循环按原厂音频长度由 HA 调度，定时停止受 LAN 通信延迟影响，需要 HA 与设备保持连接。
- Telnet 使用明文通信，应在可信局域网内使用，并配置恰当的密码。

## 开发与测试

建议使用 Linux 或 WSL，Python 3.14：

```sh
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
python -m ruff check custom_components/aqara_p3 tests_component
python -m ruff format --check custom_components/aqara_p3 tests_component
```

测试使用回环地址、虚构设备标识和协议测试向量，可离线运行，不需要实机或固件镜像。测试覆盖 HA 配置流程、实体与动作、协议编解码、模式记忆、定时状态、红外抓取生命周期及音效播放任务。有声播放及部分空调特殊功能的物理效果仍需实机核对。

原生 helper 使用 **Zig 0.14.1** 构建，在 PowerShell 中运行：

```powershell
./scripts/build-native.ps1 -Zig <zig可执行文件路径>
```

脚本直接编译组件内的 `native/p3lan.c`，更新 helper 和 SHA-256。构建目标与许可信息见[原生程序说明](custom_components/aqara_p3/native/README.md)。

## 发布版本

先更新 `custom_components/aqara_p3/manifest.json` 中的版本号并提交，再推送对应 tag：

```sh
git tag v0.4.8
git push origin v0.4.8
```

GitHub Actions 会核对 tag 与组件版本，然后创建 Release、自动生成发布说明并上传：

- `aqara_p3.zip`：HACS 安装包；手动安装时解压到 `<HA 配置目录>/custom_components/aqara_p3/`。
- `aqara_p3.zip.sha256`：安装包校验值。
