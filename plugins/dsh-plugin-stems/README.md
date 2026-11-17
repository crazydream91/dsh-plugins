# dsh-plugin-stems

给 DSH 加一组"拆音轨"的工具：视频/音频进去，人声、伴奏、逐件乐器出来。

- Node 侧（`lib/`）负责编排：用 ffmpeg 抽音频、混音、合回视频，管任务目录和 `job.json`。
- Python 侧（`py/stems_worker.py`）只负责 Demucs 分离，stdout 走 JSON 事件。

## 依赖（本机已就位）

| 东西 | 默认路径 | 说明 |
| --- | --- | --- |
| Python + torch + demucs | `D:\dsh 大肥鱼\voice\tts\venv\Scripts\python.exe` | torch 2.11+cu128，demucs 4.1 |
| ffmpeg | `D:\dsh 大肥鱼\voice\bin\ffmpeg.exe` | 不需要 ffprobe |
| 模型库（离线） | `D:\dsh 大肥鱼\voice\demucs-repo` | 放 `<sig>.th` 和 `<bag>.yaml`，见下 |
| 模型权重缓存 | `D:\dsh 大肥鱼\voice\cache`（作 `TORCH_HOME`） | 兜底用的 torch hub 缓存 |
| 产物根目录 | `D:\dsh 大肥鱼\stems` | 每个任务一个子目录 |

这些都在插件的 `cordis.patch.yml` 里，换机器改那里就行。

**模型库为什么必须有**：demucs 的 `get_model(name, repo=None)` 会先去 HuggingFace 查包，国内连接超时重试 5 次，一次就白等 ~160 秒（GPU 全程 0%）。传了 `repo=` 就完全不联网。
`demucs/repo.py` 的 `LocalRepo.scan()` 只在文件名里含 `-` 时按 `sig-checksum` 校验 sha256，所以把权重命名成 `<sig>.th` 就跳过校验；`BagOnlyRepo` 从同目录读 `<bag>.yaml`（`htdemucs.yaml` → `models: ['955717e8']`，`htdemucs_6s.yaml` → `models: ['5c90dfd2']`）。
worker 没收到 `--repo-dir` 时会自己按 `DEMUCS_REPO_DIR` → `voice/demucs-repo` → `~/voice/demucs-repo` 的顺序找，所以旧宿主进程也不会再卡 160 秒。

## 工具

| 工具 | 干什么 |
| --- | --- |
| `stems_probe` | 看时长/有没有画面/音轨参数 |
| `stems_split` | `mode=vocals` 出人声+伴奏；`mode=instruments` 出鼓/贝斯/吉他/钢琴/人声/其他 |
| `stems_list` | 回看历史任务；给 jobId 看全部产物路径 |
| `stems_mix` | 挑几轨按 dB 增益重新叠一条，可顺带截一段 |
| `stems_mux` | 把某条轨合回原视频（视频流不重编码） |
| `stems_selftest` | 体检：ffmpeg / torch / 显卡 / demucs / 模型库 / 目录 |

典型流程：

```
stems_probe  →  stems_split(mode=vocals)  →  stems_mux(stem=no_vocals)   # 去人声的 MV
stems_split(mode=vocals)  →  stems_split(mode=instruments)               # 再逐件拆
stems_mix(jobId, stems=[{name:'drums'},{name:'bass'}], trim={startSec:30,durationSec:20})
```

## 任务目录

```
stems/<yyyyMMdd-HHmmss>-<文件名>/
  job.json        清单：源、模型、每条轨的路径/时长/峰值、再加工产物
  input.wav       抽出来的 44.1k 立体声（Demucs 的输入）
  vocals.wav      ...
  no_vocals.wav   （vocals 模式）
  drums.wav bass.wav guitar.wav piano.wav other.wav vocals.wav （instruments 模式）
```

## 安装

插件以本地包的形式装进 profile（默认 `C:\Users\USER\.dsh\profiles\desktop`）。要用**源码联动**就装成 `link:`：

```bash
# 在 profile 目录下，用 DSH 自带的 pnpm
node <runtime>\node\bin\node.exe <runtime>\pnpm\bin\pnpm.mjs add "link:D:\dsh 大肥鱼\plugins\dsh-plugin-stems"
```

`link:` 装出来的是 junction，改源码即时生效；`file:` 是拷贝，改完要重装。
`plugin_manager install_bundle` 走的是 `file:`，而且本机既有的 protobufjs 构建脚本会让它报 `ERR_PNPM_IGNORED_BUILDS` 退出，所以手动装更省事。

再把包名加进 profile `package.json` 的 `dsh.profile.bundles`，然后重启 DSH。
`cordis.patch.yml` 里已经写好挂载条目，不需要手动改配置树。

**改完代码不用重启也能验**：宿主不会重新 import 模块（enable/disable 只做生命周期，Node 按 realpath 缓存 ESM），所以用假 ctx 跑一遍：

```bash
node test/plugin-smoke.mjs selftest   # 注册全部工具 + 打印体检结果
node test/smoke.mjs <媒体文件> [目录]  # 直接跑 probe→两轨→六轨→mix
```

## 回退

按这个顺序拆，拆完重启就回到装之前：

1. profile `package.json` 的 `dsh.profile.bundles` 里删掉 `dsh-plugin-stems`
2. profile `package.json` 的 `dependencies` 里删掉 `"dsh-plugin-stems": "link:..."`
3. 在 profile 目录跑 `pnpm install` 清理 node_modules
4. 产物目录 `D:\dsh 大肥鱼\stems` 可以整个删掉，不影响别的插件

插件源码在 `D:\dsh 大肥鱼\plugins\dsh-plugin-stems`，删不删随意。
装之前备份了 `C:\Users\USER\.dsh\profiles\desktop\package.json.bak-20261105-134922`，出问题直接覆盖回去再 `pnpm install`。

## 已知限制

- `htdemucs_6s` 只有六类；京胡、唢呐、合成器、人声和声这些都落在 `other.wav` 里，这是模型能力边界，不是参数问题。
- `htdemucs_ft` 是同一网络的微调版，质量更好但慢约 4 倍（`stems_split` 的 `model` 参数可以指定）。
- 分离 8GB 显存够用；再长再大的文件用 `segmentSec` 切片。
- 视频合回去时视频流是 copy，音频换 AAC，所以音轨不是无损的——要无损就用 `stems_mix` 直接拿 wav。
