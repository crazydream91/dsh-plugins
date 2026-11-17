#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dsh-plugin-stems 的分离内核。

只做一件事：把一段音频按 Demucs 模型拆成 stem，写盘，并把元数据用 JSON 事件
吐到 stdout。所有面向用户的编排（抽音频、混音、合回视频）都在 Node 侧做。

stdout 协议（每行一个 JSON 对象）：
  {"type":"phase","pct":..,"stage":"load_model","msg":".."}
  {"type":"start","model":..,"device":..,"sources":[..],"samplerate":..}
  {"type":"progress","pct":..}
  {"type":"result","ok":true,"stems":[{...}],"elapsedSec":..}
  {"type":"error","ok":false,"message":"..","traceback":".."}

stderr 只放人能看的东西（torch 的 warning、tqdm 之类），Node 侧不解析。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class Progress:
    """把 demucs 的分段回调折算成一个 0-100 的总进度，并且做节流。"""

    def __init__(self) -> None:
        self.last = -1.0
        self.calls = 0

    def __call__(self, data: dict) -> None:
        self.calls += 1
        try:
            length = float(data.get("audio_length") or 0)
            offset = float(data.get("segment_offset") or 0)
            models = max(1, int(data.get("models") or 1))
            model_idx = max(0, int(data.get("model_idx_in_bag") or 0))
            shift_idx = max(0, int(data.get("shift_idx") or 0))
            state = data.get("state")
        except (TypeError, ValueError):
            return
        if length <= 0:
            return

        # 每个 segment 起点回调一次 start，下一次回调时 offset 已经前进。
        frac = min(1.0, max(0.0, offset / length))
        overall = (shift_idx + (model_idx + frac) / models) / max(1, shift_idx + 1)
        pct = 5.0 + 93.0 * max(0.0, min(1.0, overall))
        if pct - self.last >= 1.0 or state == "start" and self.last < 5.0:
            self.last = pct
            emit({"type": "progress", "pct": round(pct, 1)})


def db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-9))


def default_repo_dir() -> str:
    """没显式给 --repo-dir 时找一个本地模型库。

    顺序：环境变量 DEMUCS_REPO_DIR → 工作区 voice/demucs-repo → ~/voice/demucs-repo。
    找不到就返回空，交给 demucs 自己联网（国内会很慢，但至少能跑）。
    """
    env = os.environ.get("DEMUCS_REPO_DIR", "").strip()
    if env:
        return env
    here = Path(__file__).resolve()
    candidates = []
    # plugins/<pkg>/py/stems_worker.py → 往上四层是工作区根
    if len(here.parents) > 3:
        candidates.append(here.parents[3] / "voice" / "demucs-repo")
    candidates.append(Path.home() / "voice" / "demucs-repo")
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    return ""


def resolve_device(requested: str, torch) -> str:
    if requested and requested != "auto":
        return requested
    return "cuda" if torch.cuda.is_available() else "cpu"


def build_stems(separated: dict, mix, two_stems: str):
    """two_stems 非空时，把目标 stem 之外的残差合成一条 no_<stem>（伴奏/背景音）。"""
    if not two_stems:
        return dict(separated)

    if two_stems not in separated:
        raise SystemExit(
            f"模型没有名为 {two_stems!r} 的源，可选：{', '.join(separated)}"
        )
    target = separated[two_stems]
    rest = mix - target
    # 残差和声部叠加后可能略微超出 [-1,1]，按峰值回缩，避免削波失真。
    peak = float(rest.abs().max()) if rest.numel() else 0.0
    if peak > 0.999:
        rest = rest * (0.999 / peak)
    return {two_stems: target, f"no_{two_stems}": rest}


def main() -> int:
    ap = argparse.ArgumentParser(prog="stems_worker")
    ap.add_argument("--input", required=True, help="待分离的音频文件（wav）")
    ap.add_argument("--out-dir", required=True, help="stem 输出目录")
    ap.add_argument("--model", default="htdemucs")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--shifts", type=int, default=1)
    ap.add_argument("--overlap", type=float, default=0.25)
    ap.add_argument("--segment", type=float, default=0.0)
    ap.add_argument("--two-stems", default="", help="例如 vocals，只保留目标+伴奏")
    ap.add_argument("--format", default="wav", choices=["wav", "flac", "mp3"])
    ap.add_argument("--mp3-bitrate", type=int, default=320)
    ap.add_argument("--clip-mode", default="rescale",
                    choices=["rescale", "clamp", "none"])
    ap.add_argument("--save-only", default="", help="逗号分隔，只落盘这些 stem")
    ap.add_argument("--repo-dir", default=default_repo_dir(),
                    help="本地模型库目录（放 <sig>.th 和 <bag>.yaml）；给了就不联网")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = Path(args.input)
    if not src.is_file():
        emit({"type": "error", "ok": False,
              "message": f"输入文件不存在: {src}"})
        return 2

    started = time.time()
    try:
        import torch
        from demucs.api import Separator, save_audio
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "ok": False,
              "message": f"无法导入 demucs/torch: {exc}",
              "traceback": traceback.format_exc()})
        return 3

    device = resolve_device(args.device, torch)
    emit({"type": "phase", "pct": 1.0, "stage": "load_model",
          "msg": f"加载模型 {args.model}（{device}）"})

    # demucs 默认先去 huggingface 查模型（国内常常几百秒超时）。本地库既快又稳。
    repo_dir = Path(args.repo_dir) if args.repo_dir else None
    if repo_dir is not None and not repo_dir.is_dir():
        emit({"type": "phase", "pct": 1.0, "stage": "load_model",
              "msg": f"本地模型库 {repo_dir} 不存在，改用在线仓库（可能很慢）"})
        repo_dir = None
    if repo_dir is not None:
        emit({"type": "phase", "pct": 2.0, "stage": "load_model",
              "msg": f"用本地模型库 {repo_dir}"})

    try:
        # 换 TORCH_HOME 后首次会联网拉权重，这里把进度说清楚，免得看着像卡死。
        sep = Separator(
            model=args.model,
            repo=repo_dir,
            device=device,
            shifts=max(1, args.shifts),
            overlap=args.overlap,
            split=True,
            segment=(args.segment or None),
            callback=Progress(),
        )
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "ok": False,
              "message": f"模型加载失败（权重没下下来？）: {exc}",
              "traceback": traceback.format_exc()})
        return 4

    sources = list(sep.model.sources)
    emit({"type": "start", "model": args.model, "device": device,
          "sources": sources, "samplerate": sep.samplerate,
          "audioChannels": sep.audio_channels})
    emit({"type": "phase", "pct": 4.0, "stage": "separate", "msg": "开始分离"})

    try:
        mix, separated = sep.separate_audio_file(src)
    except KeyboardInterrupt:
        emit({"type": "error", "ok": False, "message": "被中断"})
        return 130
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "ok": False,
              "message": f"分离失败: {exc}",
              "traceback": traceback.format_exc()})
        return 5

    emit({"type": "phase", "pct": 98.0, "stage": "save", "msg": "写盘"})

    wanted = {s.strip() for s in args.save_only.split(",") if s.strip()}
    results = []
    try:
        for name, tensor in build_stems(separated, mix, args.two_stems).items():
            if wanted and name not in wanted:
                continue
            target = out_dir / f"{name}.{args.format}"
            if args.format == "mp3":
                save_audio(tensor.cpu(), str(target), samplerate=sep.samplerate,
                           bitrate=args.mp3_bitrate, clip=args.clip_mode)
            elif args.format == "flac":
                save_audio(tensor.cpu(), str(target), samplerate=sep.samplerate,
                           clip=args.clip_mode, bits_per_sample=16)
            else:
                save_audio(tensor.cpu(), str(target), samplerate=sep.samplerate,
                           clip=args.clip_mode, bits_per_sample=16)
            peak = float(tensor.abs().max()) if tensor.numel() else 0.0
            rms = float(tensor.pow(2).mean().sqrt()) if tensor.numel() else 0.0
            results.append({
                "name": name,
                "path": str(target),
                "durationSec": round(tensor.shape[-1] / sep.samplerate, 2),
                "peakDb": round(db(peak), 2),
                "rmsDb": round(db(rms), 2),
                "bytes": target.stat().st_size if target.exists() else 0,
            })
            emit({"type": "phase",
                  "pct": round(98.0 + 1.8 * len(results) / 8, 1),
                  "stage": "save", "msg": f"已写出 {name}"})
    except Exception as exc:  # noqa: BLE001
        emit({"type": "error", "ok": False,
              "message": f"写盘失败: {exc}",
              "traceback": traceback.format_exc()})
        return 6

    emit({
        "type": "result",
        "ok": True,
        "model": args.model,
        "device": device,
        "samplerate": sep.samplerate,
        "twoStems": args.two_stems or None,
        "format": args.format,
        "shifts": args.shifts,
        "stems": results,
        "elapsedSec": round(time.time() - started, 2),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
