"""小晏的声音 v1 —— F5-TTS 零样本克隆。

用法：
  python say.py "要念的话" --out tts/out/line-01.wav [--ref ref/ref-24k.wav] [--text 参考音的文字]
"""
import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
os.environ.setdefault("HF_HOME", str(ROOT / "hf"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import _boot  # noqa: F401,E402  (环境 + 音频后端)
from f5_tts.api import F5TTS  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--out", default="tts/out/line.wav")
    ap.add_argument("--ref", default="ref/ref-24k.wav")
    ap.add_argument("--text_ref", default="")
    ap.add_argument("--nfe", type=int, default=32)
    ap.add_argument("--cfg", type=float, default=2.0)
    ap.add_argument("--speed", type=float, default=0.95)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--ckpt", default="", help="本地 safetensors，给了就不联网")
    ap.add_argument("--vocoder", default="", help="本地 vocos 目录（含 config.yaml + pytorch_model.bin）")
    a = ap.parse_args()

    out = Path(a.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    f5 = F5TTS(
        device="cuda",
        hf_cache_dir=str(ROOT / "hf"),
        ckpt_file=a.ckpt,
        vocoder_local_path=(a.vocoder or None),
    )
    print(f"[load] {time.time() - t0:.1f}s", flush=True)

    t1 = time.time()
    wav, sr, spec = f5.infer(
        ref_file=str((ROOT / a.ref).resolve()),
        ref_text=a.text_ref,
        gen_text=a.text,
        nfe_step=a.nfe,
        cfg_strength=a.cfg,
        speed=a.speed,
        seed=a.seed,
        file_wave=str(out),
        show_info=print,
    )
    print(f"[synth] {time.time() - t1:.1f}s  -> {out}  ({len(wav) / sr:.2f}s)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
