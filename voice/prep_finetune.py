"""把 17 分钟人声轨切成训练用短句 + 逐句转写（F5-TTS / GPT-SoVITS 微调的数据准备）。

输出：
  voice/ft/wavs/*.wav       单声道 24 kHz 短句（2-10 s）
  voice/ft/list.txt         `wavs/xxx.wav|文本` 每行一条
"""
import json
import re
import subprocess
from pathlib import Path

import _boot  # noqa: F401,E402

import numpy as np
import soundfile as sf
import torch
from transformers import pipeline

ROOT = Path(__file__).parent
FF = ROOT / "bin/ffmpeg.exe"
SRC = ROOT / "sep-long/htdemucs/long-44k/vocals.wav"
OUT = ROOT / "ft"
WAVS = OUT / "wavs"
SR = 24000
MIN_DUR, MAX_DUR = 2.0, 10.0
PAD = 0.12


def silence_points(path):
    proc = subprocess.run(
        [str(FF), "-hide_banner", "-i", str(path),
         "-af", "silencedetect=noise=-32dB:d=0.30", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore")
    starts, ends = [], []
    for ln in proc.stderr.splitlines():
        if "silence_start:" in ln:
            starts.append(float(ln.split("silence_start:")[1].split()[0]))
        elif "silence_end:" in ln:
            ends.append(float(ln.split("silence_end:")[1].split()[0]))
    return starts, ends


def segments(total):
    starts, ends = silence_points(SRC)
    pts = [0.0]
    for s, e in zip(starts, ends):
        pts += [s, e]
    pts.append(total)
    pts = sorted(set(p0 for p0 in (round(p, 2) for p in pts)))
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def clean(text):
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[，。、；：,.!?！？\s]+|[，。、；：,.!?！？\s]+$", "", text)
    return text


def main():
    WAVS.mkdir(parents=True, exist_ok=True)
    data, sr = sf.read(str(SRC), always_2d=True)
    mono = data.mean(axis=1)
    total = len(mono) / sr

    segs = [(s, e) for s, e in segments(total) if MIN_DUR <= (e - s) <= MAX_DUR]
    print(f"segments kept: {len(segs)}", flush=True)

    pipe = pipeline("automatic-speech-recognition",
                    model=str(ROOT / "models/whisper-large-v3-turbo"),
                    device=0 if torch.cuda.is_available() else -1)

    items = []
    for i, (s, e) in enumerate(segs, 1):
        dst = WAVS / f"seg-{i:04d}.wav"
        subprocess.run([str(FF), "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{max(0, s - PAD):.2f}", "-to", f"{min(total, e + PAD):.2f}",
                        "-i", str(SRC), "-ac", "1", "-ar", str(SR), str(dst)],
                       capture_output=True)
        wav, _ = sf.read(str(dst))
        if len(wav) < SR * MIN_DUR * 0.8:
            dst.unlink(missing_ok=True)
            continue
        peak = float(np.abs(wav).max()) or 1.0
        if peak < 0.01:
            dst.unlink(missing_ok=True)
            continue
        sf.write(str(dst), wav * (0.89 / peak), SR)

        text = clean(pipe(str(dst), generate_kwargs={"language": "chinese", "task": "transcribe"})["text"])
        items.append({"file": f"wavs/{dst.name}", "text": text})
        if i % 20 == 0:
            print(f"{i}/{len(segs)}", flush=True)

    lines = [f"{it['file']}|{it['text']}" for it in items if it["text"]]
    (OUT / "list.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "meta.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: {len(lines)} samples", flush=True)


if __name__ == "__main__":
    main()
