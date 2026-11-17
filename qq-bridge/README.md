# QQ ↔ DSH 桥接

在 QQ 里和被接入的 AI 助手聊天。基于 **OneBot v11** 协议（NapCat / LLOneBot / Lagrange 都能对接）。

## 架构

```
QQ 客户端 ──> NapCat（OneBot v11） ──WS──> bridge.mjs ──子进程──> DSH headless 一回合
                    ^                          │
                    └────── send_msg ──────────┘
```

- 每条 QQ 消息触发**一个独立会话**，按 chatKey 复用 `--session-id`，保持连续性
- 群里默认只有 @ 才响应，可配置成"自由插话"并带冷却
- 支持 **文字 / 图片 / 视频抽帧 / 语音转写 / 语音回复** 收发
- 断线 30 秒重试；被 QQ 踢下线时由 `kick-watch.mjs` 弹二维码并通知

## 文件

| 文件 | 作用 |
|---|---|
| `bridge.mjs` | 主程序：事件循环、白名单、排队、权限、多模态收发 |
| `ws-lite.mjs` | 零依赖 WebSocket 客户端/服务端/帧编解码 |
| `drive.mjs` | 内驱力模型：决定"要不要主动开口" |
| `promise.mjs` | 承诺账本：说过的事要能核销 |
| `schedule.mjs` | 日程/提醒/忌口账本（JSON 存储 + 文件锁） |
| `active.mjs` | 常驻巡查器：日程提醒、课前提醒、主动开口 |
| `doctor.mjs` | 自检：QQ 登录、ASR 端口、上游连通性、应用运行时长、磁盘 |
| `panel.mjs` | 生成状态面板图 |
| `kick-watch.mjs` | 掉线看门狗：日志里出现 KickOffLine 就弹码+通知 |
| `mock-napcat.mjs` | 假 OneBot 服务端，不登录 QQ 也能测全链路 |

## 快速开始

1. 装 NapCat，登录一个小号，开 OneBot v11 的 **正向 WebSocket**，端口 3001
2. `copy config.example.json config.json`，填 token 与白名单
3. 同步 DSH 的 headless 覆盖配置（`dsh-overlay.yml`，指定 provider / model / 权限）
4. `run-bridge.cmd` 启动

## 设计取舍

- **零依赖**：不引 `ws`、不引 `axios`，全用 `node:http`/`node:tls`/`node:crypto` 手写。
  代价是多写了 200 行帧编解码，好处是在没有构建环境的机器上直接能跑。
- **每聊天一会话**：不给所有群共享上下文，避免互相串味。
- **权限分级**：主人私聊给 `danger-full-access`（能改文件、跑命令），其他人 `read-only`。
- **静音优先**：21:00–08:00 群里不接话，私聊保留——半夜只有主人能叫醒。