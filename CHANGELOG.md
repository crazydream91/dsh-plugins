# CHANGELOG

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## 插件清单

| 插件 | 版本 | 说明 |
|---|---|---|
| `dsh-plugin-score-smith` | 0.1.0 | 听音制谱：分轨 → 识音 → 可编辑曲谱（五线谱 / 简谱 / 六线谱 / 鼓谱） |
| `dsh-plugin-stems` | 0.1.0 | 音轨分离：demucs 六轨拆分与混音 |
| `dsh-plugin-hello` | 0.1.0 | 最小可运行插件模板 |
| `qq-bridge` | — | QQ ↔ DSH 桥接（非插件，独立进程） |
| `voice` / `gen` | — | 语音克隆与本地出图脚本集 |

## [Unreleased]

### Added
- 仓库初始化：四个模块（插件 / QQ 桥接 / 语音 / 出图）
- `dsh-plugin-score-smith` 并入（0.1.0）：分轨、音高识别、量化、乐理库、MusicXML/MIDI 导出、多谱种排版
- `qq-bridge` 脱敏入库（`config.example.json`，不含 token 与白名单）

### 说明
- 版本以各插件 `package.json` 为准；发布时打 tag，如 `score-smith-v0.1.0`