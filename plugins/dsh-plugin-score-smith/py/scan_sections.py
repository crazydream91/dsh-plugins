# -*- coding: utf-8 -*-
"""扫一遍整首歌，找出各 stem 都真有声音的 30 秒区间，作为测试段落。"""
import os

import librosa
import numpy as np

STEMS = r"D:\dsh 大肥鱼\stems\demo-instruments-6s"
names = ("vocals", "drums", "bass", "guitar")
tracks = {}
for name in names:
    y, sr = librosa.load(os.path.join(STEMS, f"{name}.wav"), sr=8000, mono=True)
    tracks[name] = y
dur = min(len(v) for v in tracks.values()) / 8000.0
print("duration", round(dur, 1), "s")

win = 15.0
for start in np.arange(0, max(0.0, dur - win), win):
    lo = int(start * 8000)
    hi = int((start + win) * 8000)
    row = []
    for name in names:
        seg = tracks[name][lo:hi]
        rms = float(np.sqrt(np.mean(seg ** 2)) + 1e-9)
        row.append(f"{name}={20 * np.log10(rms):6.1f}")
    print(f"{start:6.1f}s  " + "  ".join(row))
