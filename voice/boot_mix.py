"""把自制配音 + 柔和环境音混成 7 秒音轨，给启动动画第二幕用。"""
import numpy as np, soundfile as sf
from pathlib import Path

OUT = Path(r"D:\dsh 大肥鱼\cache\bootvoice")
SR = 32000
DUR = 6.9

def load(p, sr=SR):
    x, s = sf.read(str(p))
    if x.ndim > 1:
        x = x.mean(axis=1)
    if s != sr:
        n = int(round(len(x) * sr / s))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    # 去掉首尾静音
    thr = max(np.abs(x).max() * 0.03, 1e-4)
    idx = np.where(np.abs(x) > thr)[0]
    if len(idx):
        x = x[max(0, idx[0] - int(0.01 * sr)): idx[-1] + int(0.03 * sr)]
    return x

t = np.arange(int(DUR * SR)) / SR

# 柔和 pad：三个低音正弦 + 慢颤音，做一个很轻的“呼吸”底色
pad = np.zeros_like(t)
for f, a, det in ((110.0, 0.30, 0.15), (164.8, 0.22, 0.11), (220.0, 0.16, 0.08)):
    vib = 1.0 + det * np.sin(2 * np.pi * 0.17 * t + f)
    pad += a * np.sin(2 * np.pi * f * t * vib)
# 慢起慢落
env = np.minimum(t / 1.6, 1.0) * np.minimum((DUR - t) / 1.8, 1.0)
env = np.clip(env, 0, 1) ** 1.2
pad = pad * env

# 一点点“空气感”噪声，压得很低
rng = np.random.default_rng(7)
air = rng.normal(0, 1, len(t))
k = np.ones(24) / 24
air = np.convolve(air, k, mode="same") * 0.05 * env

mix = pad * 0.075 + air * 0.02

# 台词
starts = (0.55, 1.75, 3.05)
for name, at in zip(("line01.wav", "line02.wav", "line03.wav"), starts):
    v = load(OUT / "vo" / name) * 0.62
    i0 = int(at * SR)
    i1 = min(len(mix), i0 + len(v))
    mix[i0:i1] += v[: i1 - i0]

# 简单低通去毛刺
k = np.exp(-np.linspace(0, 3, 8)); k /= k.sum()
mix = np.convolve(mix, k, mode="same")

peak = np.abs(mix).max()
mix = mix / peak * 0.72

stereo = np.stack([mix, mix], axis=1)
wav = OUT / "whale-custom-32k.wav"
sf.write(str(wav), stereo.astype(np.float32), SR, subtype="PCM_16")
print(f"写出 {wav}  {len(mix)/SR:.2f}s  peak={np.abs(mix).max():.3f}")
