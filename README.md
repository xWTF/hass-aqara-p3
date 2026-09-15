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

## 功能

### 电力与空调

- 功率、累计电量和插座供电状态。
- 开关、制冷、制热、除湿、送风、自动模式。
- 制冷 18～32°C、制热 10～30°C、自动 18～30°C，步进 0.5°C。
- 除湿温度微调 −2～+2，步进 0.5；风量自动、静音及 1～5 档。
- 上下、左右、立体摆风；强力、极速、康达气流、省电和室外机静音。
- 各模式分别保存温度、风量及除湿微调。
- 可关联外部室温实体；提供遥控器报文抓取界面。

定时开关机、康达舒睡和原厂缓存诊断实体默认禁用，可在设备页按需启用。插座供电操作提供独立动作与断电确认。

### 声音

独立的 “声音” 子设备提供音效选择、音量、播放和停止。自动化动作可指定音效、音量、总播放次数及停止时间。

## 安装

### 手动安装

1. 将本仓库的 `custom_components/aqara_p3` 整个文件夹复制到 `<HA 配置目录>/custom_components/`。
2. 确认 `custom_components/aqara_p3/manifest.json` 存在，然后重启 Home Assistant。
3. 在“设置 → 设备与服务 → 添加集成”中选择 **Aqara P3**。
4. 填写设备 IP 和 Telnet 密码；登录用户为 `admin`。

升级时覆盖 `aqara_p3` 文件夹并重启 Home Assistant，已有配置保留。

### HACS 自定义仓库

仓库发布后，在 HACS 的“自定义存储库”中填写本仓库地址，类别选择“集成”，下载后重启 Home Assistant，再按上述步骤添加集成。

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

- HA 使用异步 Telnet 读取设备缓存，通过本地 IPC 调用原厂红外和音频服务。
- 随组件提供约 74 KB 的静态 MIPS helper，按需复制至设备 `/tmp`，校验设备身份、固件及文件哈希后运行。原生源码和构建脚本随仓库提供。
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
