"""分句合成再拼接：让每句单独起调，句内起伏比整段一次性合成更像说话。"""
import argparse
import re
import time
from pathlib import Path

import _boot  # noqa: F401,E402

import numpy as np
import soundfile as sf
from f5_tts.api import F5TTS

ROOT = Path(__file__).parent
CKPT = (ROOT / "hf/models--SWivid--F5-TTS/snapshots/84e5a410d9cead4de2f847e7c9369a6440bdfaca"
        "/F5TTS_v1_Base/model_1250000.safetensors")
VOC = (ROOT / "hf/models--charactr--vocos-mel-24khz/snapshots"
       "/0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21")

SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")
CLAUSE_SPLIT = re.compile(r"[，,、]+")


def split_sentences(text):
    parts = [p.strip() for p in SENT_SPLIT.split(text) if p.strip()]
    return parts or [text.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--out", default="tts/out/stitch.wav")
    ap.add_argument("--ref", default="ref/ref-clean-24k.wav")
    ap.add_argument("--ref-text", default="")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--cfg", type=float, default=2.0)
    ap.add_argument("--nfe", type=int, default=48)
    ap.add_argument("--gap", type=float, default=0.12, help="句间静音秒数")
    ap.add_argument("--keep-clauses", action="store_true", help="连逗号也拆（更碎）")
    a = ap.parse_args()

    ref = (ROOT / a.ref) if not Path(a.ref).is_absolute() else Path(a.ref)
    ref_text = a.ref_text or (ROOT / "ref/ref.txt").read_text(encoding="utf-8").strip()

    pieces = split_sentences(a.text)
    if a.keep_clauses:
        pieces = [c.strip() for p in pieces for c in CLAUSE_SPLIT.split(p) if c.strip()]

    t0 = time.time()
    f5 = F5TTS(device="cuda", hf_cache_dir=str(ROOT / "hf"),
               ckpt_file=str(CKPT), vocoder_local_path=str(VOC))
    print(f"[load] {time.time() - t0:.1f}s  pieces={len(pieces)}", flush=True)

    tmp_dir = ROOT / "tts/tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    sr_out = None
    for i, piece in enumerate(pieces):
        tmp = tmp_dir / f"piece-{i}.wav"
        t1 = time.time()
        f5.infer(ref_file=str(ref), ref_text=ref_text, gen_text=piece + "。",
                 file_wave=str(tmp), show_info=lambda *_: None,
                 speed=a.speed, cfg_strength=a.cfg, nfe_step=a.nfe, remove_silence=True)
        data, sr = sf.read(str(tmp))
        sr_out = sr
        chunks.append(data.astype(np.float32))
        print(f"[{i}] {piece}  {len(data)/sr:.2f}s  ({time.time()-t1:.1f}s)", flush=True)

    gap = np.zeros(int(sr_out * a.gap), dtype=np.float32)
    merged = chunks[0]
    for c in chunks[1:]:
        merged = np.concatenate([merged, gap, c])

    peak = float(np.abs(merged).max()) or 1.0
    merged = merged * (0.89 / peak)

    out = ROOT / a.out if not Path(a.out).is_absolute() else Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), merged, sr_out)
    print(f"[out] {out}  {len(merged)/sr_out:.2f}s", flush=True)


if __name__ == "__main__":
    main()
