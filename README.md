# ACE Batch Remix

一个独立的 Windows 批处理客户端：把 `input/` 中的音乐通过已建立的本机 HTTP 隧道提交给远端 ACE-Step 1.5，并为每首歌下载两个 Remix MP3。

> ACE-Step 仓库与本仓库完全独立。本项目不会启动、修改、fork 或复制 ACE-Step 模型源码，也不负责 SSH 或 Tailscale。

## 工作流

```text
远端 Windows：启动 ACE-Step API Server
  uv run --offline --no-sync acestep-api --host 127.0.0.1 --port 8001
            ↓
本机：在仓库外建立 SSH Tunnel
            ↓
input/ 放入音乐，修改 config.json 的 music_caption
            ↓
双击 run.bat
            ↓
outputs/ 获得每首歌的两个 MP3
```

## 首次使用

1. 安装 Windows Python 3.10+（安装时勾选 Python Launcher）。
2. 在本目录运行一次：`py -3 -m pip install -r requirements.txt`
3. 确认远端服务和隧道使 `http://127.0.0.1:8001/health` 可访问。
4. 将 `.mp3`、`.wav` 或 `.flac` 放入 `input/`。
5. 编辑 `config.json` 中的 `music_caption`，不要保留 `CHANGE_ME`。
6. 双击 `run.bat`。

## 固定实验设置

`config.json` 将实验参数集中保存。当前工具强制 `generation_mode=remix`、`batch_size=2`、`audio_format=mp3` 与随机 seed；服务器返回的实际 `seed_value` 会写入 `manifest.json`。

当前 ACE-Step REST 映射仅在 [`src/api.py`](src/api.py) 的 `build_remix_payload()`：

| 本项目设置 | REST 字段 |
| --- | --- |
| Remix | `task_type=cover` + multipart `src_audio` |
| Music Caption | `prompt` |
| Remix Strength | `audio_cover_strength` |
| Cover Strength | `cover_noise_strength` |
| 2 个版本 | `batch_size=2` |
| MP3 | `audio_format=mp3` |

映射基于 ACE-Step 的[官方 API 文档](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/API.md)和[推理参数源码](https://github.com/ace-step/ACE-Step-1.5/blob/main/acestep/inference.py)。升级远端 ACE-Step 后，如需调整字段，只修改该函数并先做一首歌的人工验证。

## 恢复与输出

- `manifest.json` 以源文件 SHA-256 记录 task ID、状态、重试、输出路径、服务器 seed 与错误；它被 Git 忽略，保留在本机即可断点续跑。
- 已成功写入两个非空 MP3 的歌曲会跳过，不会重新生成。
- 旧 task ID 从服务器结果中消失时，会在 `max_retries` 范围内重新提交该歌曲，不会影响已完成歌曲。
- 下载写入 `.part` 临时文件，只有非空完整下载后才原子重命名为最终 MP3。
- 输出名称支持中文和日文；清理 Windows 非法字符。不同源文件同名时，目录会追加短 SHA-256，避免覆盖。
- 详细 HTTP/错误记录保存在 `logs/`，终端只显示状态和最终缺失项。

## 验证范围

本仓库的自动测试使用 Mock API 验证上传、批量查询、恢复、下载原子性和 Unicode 文件名。真实 ACE-Step Server 的可达性、音频格式、模型版本兼容性及实际 Remix 音质仍需要在远端 GPU 环境用一首测试音乐人工确认。
