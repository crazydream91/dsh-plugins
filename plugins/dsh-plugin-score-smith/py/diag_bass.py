# -*- coding: utf-8 -*-
"""诊断：贝斯轨 pyin 为什么听不出音。"""
import os

import librosa
import numpy as np

from theory import INSTRUMENTS

path = os.path.join(os.environ["TEMP"], "score-test", "stems", "bass.wav")
y, sr = librosa.load(path, sr=22050, mono=True)
print("sr", sr, "dur", round(len(y) / sr, 2), "rms_db",
      round(20 * np.log10(np.maximum(librosa.feature.rms(y=y, frame_length=2048, hop_length=256)[0], 1e-9)).mean(), 1))

spec = INSTRUMENTS["bass"]
lo, hi = spec["range"]
fmin = 440.0 * 2 ** ((lo - 69) / 12.0)
fmax = 440.0 * 2 ** ((hi - 69) / 12.0)
print("fmin", round(fmin, 1), "fmax", round(fmax, 1))

for fl in (2048, 4096):
    f0, voiced, prob = librosa.pyin(y, fmin=fmin, fmax=fmax, sr=sr,
                                    frame_length=fl, hop_length=256, fill_na=np.nan)
    ok = np.isfinite(f0)
    print(f"frame_length={fl}: frames={len(f0)} voiced={int(ok.sum())} ({ok.mean()*100:.1f}%)",
          "median_hz", round(float(np.nanmedian(f0)), 1) if ok.any() else None)

# 放松阈值试一下
f0b, vb, pb = librosa.pyin(y, fmin=fmin, fmax=fmax, sr=sr, frame_length=4096,
                           hop_length=256, fill_na=np.nan, max_transition_rate=35,
                           switch_prob=0.01, no_trough_prob=0.1, resolution=0.05)
okb = np.isfinite(f0b)
print("relaxed: voiced", int(okb.sum()), "pct", round(float(okb.mean() * 100), 1))
