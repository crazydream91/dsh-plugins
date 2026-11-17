# -*- coding: utf-8 -*-
"""测试用：把一段歌截成 30 秒，并同步截 6 条 stem。"""
import os

import librosa
import soundfile as sf

SRC = r"D:\dsh 大肥鱼\stems\20261105-142052-BV1k5a468EEH\input.wav"
STEMS = r"D:\dsh 大肥鱼\stems\demo-instruments-6s"
OUT = os.path.join(os.environ.get("TEMP", "."), "score-test")
os.makedirs(os.path.join(OUT, "stems"), exist_ok=True)

SEC = 30.0
START = float(os.environ.get("CLIP_START", "75"))

y, sr = librosa.load(SRC, sr=None, mono=False, offset=START, duration=SEC)
sf.write(os.path.join(OUT, "clip.wav"), y.T if y.ndim > 1 else y, sr)
print("clip", y.shape, sr)

for name in ("vocals", "drums", "bass", "guitar", "piano", "other"):
    src = os.path.join(STEMS, f"{name}.wav")
    if not os.path.isfile(src):
        continue
    z, zsr = librosa.load(src, sr=None, mono=False, offset=START, duration=SEC)
    sf.write(os.path.join(OUT, "stems", f"{name}.wav"), z.T if z.ndim > 1 else z, zsr)
    print("stem", name, z.shape, round(20 * __import__("numpy").log10(float(__import__("numpy").max(__import__("numpy").abs(z))) + 1e-9), 1), "dBFS")

print("OUT", OUT)
