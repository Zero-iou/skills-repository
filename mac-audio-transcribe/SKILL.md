---
name: mac-audio-transcribe
description: macOS 系统音频实时录制与语音识别（火山引擎 ASR），支持 ⌘⇧E 仅系统音频、⌘⇧D 麦克风+系统音频，按一次开始再按一次停止。
---

macOS 音频录制与转录系统的配置助手。这套方案完全不涉及 yap，链路为：Hammerspoon 快捷键 → Shell 脚本 → Python → ffmpeg 录音 → 火山引擎极速识别 API。

## 系统要求

- macOS（Intel / Apple Silicon）
- Python 3（自带）
- Homebrew

## 依赖安装（自动）

部署前自动检查并安装以下依赖。用 `command -v` 或 `brew list` 检查是否存在，缺失的自动执行 `brew install`：

1. **BlackHole 2ch** — `brew install blackhole-2ch`
   - 安装后提示用户打开「音频 MIDI 设置」，创建多输出设备「BlackHole + Speakers」，勾选 BlackHole 2ch 和 MacBook 扬声器（这一步无法自动化，需要用户手动操作）。

2. **SwitchAudioSource** — `brew install switchaudio-osx`

3. **Hammerspoon** — `brew install --cask hammerspoon`
   - 安装后提示用户打开 Hammerspoon 并确保「在菜单栏中显示」已启用。

4. **ffmpeg** — `brew install ffmpeg`

## 一键部署

本 skill 目录下包含全部脚本文件。执行以下命令复制到 `~/.hammerspoon/`：

```bash
mkdir -p ~/.hammerspoon
cp "${CLAUDE_SKILL_DIR}/scripts/init.lua" ~/.hammerspoon/
cp "${CLAUDE_SKILL_DIR}/scripts/record_e.sh" ~/.hammerspoon/
cp "${CLAUDE_SKILL_DIR}/scripts/record_d.sh" ~/.hammerspoon/
cp "${CLAUDE_SKILL_DIR}/scripts/volc_asr.py" ~/.hammerspoon/
chmod +x ~/.hammerspoon/record_e.sh ~/.hammerspoon/record_d.sh
```

### 配置火山引擎凭证

复制文件后，检查 `~/.hammerspoon/volc_asr.py` 中的 `APPID` 和 `ACCESS_TOKEN` 是否为空字符串。如果是，**必须询问用户提供**，然后用 Edit 工具自动写入，不要让用户手动改文件。

替换目标：
```python
APPID = ""
ACCESS_TOKEN = ""
```

### 文件说明

- **[scripts/init.lua](scripts/init.lua)** — Hammerspoon 快捷键绑定（⌘⇧E / ⌘⇧D）
- **[scripts/record_e.sh](scripts/record_e.sh)** — 仅录制系统音频的 toggle 脚本
- **[scripts/record_d.sh](scripts/record_d.sh)** — 录制麦克风+系统音频的 toggle 脚本
- **[scripts/volc_asr.py](scripts/volc_asr.py)** — 核心脚本：录音、音频切换、火山引擎 API 转录、通知弹窗

## 使用方式

| 快捷键 | 功能 |
|--------|------|
| `⌘⇧E` | 仅录制系统音频 |
| `⌘⇧D` | 录制麦克风 + 系统音频 |

操作：按一次开始，再按一次停止并自动转录。结果保存到 `~/workspace/record/YYYYMMDD_HHMMSS-record.txt`。

## 故障排查

- **没有弹窗通知**：检查「系统设置 → 通知 → Hammerspoon / Terminal」是否允许通知。
- **音频没切回来**：检查 SwitchAudioSource 安装路径是否为 `/opt/homebrew/bin/SwitchAudioSource`（Apple Silicon）。Intel Mac 用 `/usr/local/bin/SwitchAudioSource`。
- **ffmpeg 找不到设备**：确认 BlackHole 2ch 已安装，在「音频 MIDI 设置」中可见。
- **转录失败**：检查 Terminal 中的 `[HTTP 错误]` 或 `[请求异常]` 日志，确认网络可访问 `openspeech.bytedance.com`。
