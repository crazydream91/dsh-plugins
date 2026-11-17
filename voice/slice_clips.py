"""把整段语音切成短片段，挑出适合做声音克隆参考的几条。

依据 silencedetect 的静音区间切句，再按响度、时长挑干净的。
输出：voice/ref/clip-NN.wav + 一份清单（时长、RMS）。
"""
import re
import wave
from pathlib import Path

import numpy as np

SRC = Path(r"D:\dsh 大肥鱼\voice\src")
WAV = SRC / "voice-32k.wav"
LOG = SRC / "silence.log"
OUT = Path(r"D:\dsh 大肥鱼\voice\ref")
OUT.mkdir(parents=True, exist_ok=True)

text = LOG.read_text(encoding="utf-8", errors="ignore")
starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", text)]
ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", text)]

with wave.open(str(WAV), "rb") as w:
    sr = w.getframerate()
    total = w.getnframes() / sr
    raw = w.readframes(w.getnframes())

samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

# 静音段 = [starts[i], ends[i]]；语音段 = ends[i] .. starts[i+1]
speech = []
for i, s in enumerate(starts):
    seg_end = starts[i + 1] if i + 1 < len(starts) else total
    if i >= len(ends):
        break
    seg_start = ends[i]
    if seg_end - seg_start >= 1.2:
        speech.append((seg_start, seg_end))

cands = []
for start, end in speech:
    dur = end - start
    if not (2.5 <= dur <= 14.0):
        continue
    a, b = int(start * sr), int(end * sr)
    chunk = samples[a:b]
    if chunk.size == 0:
        continue
    rms = float(np.sqrt(np.mean(chunk ** 2)))
    db = 20 * np.log10(rms + 1e-9)
    peak = float(np.max(np.abs(chunk)))
    # 太轻（可能是伴奏/呼吸）或削波的不收
    if db < -32 or peak > 0.99:
        continue
    score = db - abs(dur - 7.5) * 0.6
    cands.append((score, start, end, dur, db, a, b))

cands.sort(reverse=True)
print(f"语音段 {len(speech)} 个，可用候选 {len(cands)} 个")

manifest = []
for idx, (score, start, end, dur, db, a, b) in enumerate(cands[:10], start=1):
    path = OUT / f"clip-{idx:02d}.wav"
    seg = samples[a:b].astype(np.float32)
    peak = float(np.max(np.abs(seg))) or 1.0
    seg = np.clip(seg * (0.89 / peak), -1, 1)
    chunk = (seg * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(chunk.tobytes())
    manifest.append((idx, start, end, dur, db, path.name, score))
    print(f"clip-{idx:02d}  {start:7.1f}s → {end:7.1f}s  {dur:5.1f}s  {db:6.1f} dBFS")

(OUT / "manifest.txt").write_text(
    "\n".join(f"{n}\t{s:.2f}\t{e:.2f}\t{d:.2f}\t{db:.1f}\t{name}" for n, s, e, d, db, name, _ in manifest),
    encoding="utf-8",
)
print("清单写到", OUT / "manifest.txt")
