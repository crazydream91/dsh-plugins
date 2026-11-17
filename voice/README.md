# 本地语音克隆流水线

目标：用自己的参考音频，在本地 GPU 上合成一个固定音色，速度与可复现性优先。

## 流程

```
素材采集 → 切片 → 筛选 → 转写 → 微调 → 合成 → 后期 → 校验
 grab_frames/  slice_clips  build_refs  make_csv  train_f5  ayan.py  master  check_text
```

| 脚本 | 作用 |
|---|---|
| `slice_clips.py` | 用 ffmpeg `silencedetect` 切分长音频，按响度/时长挑候选 |
| `build_refs.py` | 滑窗挑参考片段（8 秒窗、1 秒步进），按背景泄漏与响度方差过滤 |
| `make_csv.py` | 过滤 ASR 幻觉文本，生成微调用的 `meta.csv` |
| `prep_finetune.py` | 按 2–10 秒切片，产出训练集 |
| `train_f5.py` | 调官方 F5-TTS `finetune_cli`，Windows 下必须 `num_workers=0` |
| `ayan.py` | 合成入口：参考音 + 文本 → wav，支持 `--batch` |
| `master.py` | 后期：去首尾静音、峰值归一到 -1 dBFS |
| `check_text.py` | 用本地 whisper 把合成结果**听回来**，和原文比对 |
| `asr_server.py` | 常驻听写服务（127.0.0.1:8123），给聊天机器人当耳朵 |

## 踩过的坑（都在代码注释里）

- **peak 削波听感像机器**：F5 输出常 peak 1.00，统一压到 0.89 之后"刺"的感觉消失
- **句首多字是随机种子问题**：同一句 seed 11 干净、seed 22 多出"可"，用 `--verify` 听回来自动换种子
- **参考文本必须与音频严格对应**：错一个字，时长对齐就散，整句被拉平、没起伏
- **起伏来自参考音**：同一句话换 10 条参考音，音高标准差从 2.94 到 4.62 半音不等

## 环境

F5-TTS + torch(cu128) + demucs + whisper-large-v3-turbo，全部本地，不调用任何在线 TTS。