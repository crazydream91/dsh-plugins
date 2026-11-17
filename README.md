# dsh-plugins

给桌面 AI 助手 **DeepSeek Harness（DSH）** 做的自建插件与配套工具链。

全部代码都在 Windows 11 + 无管理员权限的条件下跑通，且刻意保持**零第三方运行时依赖**（Node 只用一个 `node:` 标准库 API 集合，Python 只用 pip 里必要的几个包）。

## 目录

| 目录 | 是什么 | 技术点 |
|---|---|---|
| `plugins/dsh-plugin-stems` | **音轨分离插件**：把歌曲/视频拆成鼓、贝斯、人声、其他 | DSH 插件协议、`ctx.tools.register`、demucs 子进程、任务队列 |
| `plugins/dsh-plugin-score-smith` | **听音制谱**：分轨 → 识音 → 可编辑曲谱（五线谱/简谱/六线谱/鼓谱） | 音高识别、量化、乐理库、verovio 排版 |
| `plugins/dsh-plugin-hello` | 最小可运行插件模板 | 插件启动契约、`inject`、patch 注入 |
| `qq-bridge` | **QQ ↔ DSH 桥接**：在 QQ 里和被接入的助手聊天 | OneBot v11、零依赖 WebSocket 实现、断线自愈、多模态收发 |
| `voice` | **本地语音克隆流水线** | F5-TTS 微调、素材切片筛选、ASR 校验、后期链 |
| `gen` | 本地出图脚本 | SDXL / diffusers、显存不足降级、分块放大 |

版本与更新记录见 [`CHANGELOG.md`](./CHANGELOG.md)。

## 为什么值得看

**`qq-bridge/ws-lite.mjs`**：200 多行手写的 WebSocket 客户端 + 服务端 + 帧编解码，用来避开原生模块编译。
踩过的坑写在注释里：客户端发出的 pong 必须打掩码，否则对端抛 `Invalid WebSocket frame: MASK must be set` 并直接崩进程。

**`qq-bridge/drive.mjs`**：把"主动开口"从定时器改成**内驱力模型**——
四个 0~1 状态量（精神/想说话/被冷落/亲密）按真实时间演化，超过阈值才开口，
有最小间隔和每日上限。比"每天 8:10 发早安"自然得多。

**`qq-bridge/promise.mjs`**：承诺账本。助手说过"明早七点叫你"要能记账、到点核销，
否则 AI 的承诺就只是话术。

**`voice/`**：从 B 站视频里扒素材 → 切片 → 按响度/频谱筛 → whisper 转写 → 微调 → 合成 → 峰值归一，
整条链路自己搭，没依赖任何在线 TTS 服务。

## 运行前提

- **DSH**：DeepSeek Harness 桌面端（插件宿主）
- **Node**：>= 22（DSH 自带运行时即可，不需要单独装 Node）
- **Python**：3.12（语音与出图部分；插件本身不需要）
- **NapCat**：OneBot v11 实现，用于 QQ 桥接

## 配置

`qq-bridge/config.json` **不入库**（内含 token 与账号白名单）。从示例复制：

```
copy qq-bridge\config.example.json qq-bridge\config.json
```

然后填自己的 OneBot token、白名单 QQ 号、DSH 安装路径。

## 许可

个人项目，供学习参考。第三方模型（demucs / F5-TTS / SDXL）各自遵循其原始许可。