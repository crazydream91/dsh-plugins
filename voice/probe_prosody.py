"""找出"哪条参考音能把语调带起来"。

同一句话、同一组参数，只换参考音，量输出的音高起伏（半音标准差）。
起伏主要来自参考音的语调，不是后期能补的——所以先做这个对照。

    python probe_prosody.py "今天风挺大的，出门记得穿厚一点。晚上我等你回来。"
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
os.environ.setdefault("HF_HOME", str(ROOT / "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import _boot  # noqa: F401
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from f5_tts.api import F5TTS  # noqa: E402

from ayan import CKPT, VOCOS, ref_text  # noqa: E402
from master import build_filter  # noqa: E402

FF = ROOT / "bin/ffmpeg.exe"


def f0_stats(x, sr):
    win, hop = int(0.04 * sr), int(0.02 * sr)
    vals = []
    for i in range(0, len(x) - win, hop):
        fr = x[i:i + win]
        if np.sqrt((fr ** 2).mean()) < 0.012:
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, "full")[win - 1:]
        lo, hi = int(sr / 400), int(sr / 70)
        if hi >= len(ac):
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        if ac[lag] > 0.3 * ac[0]:
            vals.append(sr / lag)
    f0 = np.array(vals)
    sem = 12 * np.log2(f0 / np.median(f0))
    return np.median(f0), sem.std(), np.percentile(sem, 95) - np.percentile(sem, 5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--speed", type=float, default=0.90)
    ap.add_argument("--cfg", type=float, default=1.5)
    ap.add_argument("--nfe", type=int, default=64)
    a = ap.parse_args()

    f5 = F5TTS(device="cuda", hf_cache_dir=str(ROOT / "hf"),
               ckpt_file=str(CKPT), vocoder_local_path=str(VOCOS))

    rows = []
    for ref in sorted((ROOT / "ref2").glob("clip-*.wav")):
        rt = ref_text(ref)
        if not rt:
            continue
        wav, sr, _ = f5.infer(ref_file=str(ref), ref_text=rt, gen_text=a.text,
                              nfe_step=a.nfe, cfg_strength=a.cfg, speed=a.speed,
                              remove_silence=True, seed=11,
                              show_info=lambda *x, **y: None)
        x = np.asarray(wav, dtype=np.float32)
        med, sd, span = f0_stats(x, sr)
        peak = float(np.abs(x).max()) or 1.0
        out = ROOT / f"tts/out/probe-{ref.stem}.wav"
        sf.write(str(ROOT / "cache/_p.wav"), x / peak * 0.89, sr)
        subprocess.run([str(FF), "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(ROOT / "cache/_p.wav"),
                        "-af", build_filter(1.15, 1.35) + ",loudnorm=I=-19:TP=-4:LRA=9",
                        "-ac", "1", "-ar", "24000", str(out)], check=True)
        rows.append((ref.stem, med, sd, span, len(x) / sr))
        print(f"{ref.stem}  f0={med:.0f}Hz  sem_std={sd:.2f}  span={span:.2f}  {len(x)/sr:.2f}s", flush=True)

    print("\n按起伏排序：")
    for n, med, sd, span, dur in sorted(rows, key=lambda r: -r[2]):
        print(f"  {n}  sem_std={sd:.2f}  span={span:.2f}  {dur:.2f}s")


if __name__ == "__main__":
    sys.exit(main())
