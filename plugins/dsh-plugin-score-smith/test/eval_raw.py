"""评估单音轨识别（不改谱、不量化）的音高准确度。

用法:
    python test/eval_raw.py <stem.wav> <instrument> [--pyin]

打印：音符数、与 pyin 的逐帧对照命中率（音高 ±0.5 半音）、错音样例。
"""
import sys
import os

import numpy as np
import librosa

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py"))

import theory  # noqa: E402
import transcribe as T  # noqa: E402


def main() -> int:
    wav = sys.argv[1]
    instrument = sys.argv[2] if len(sys.argv) > 2 else "vocals"
    spec = theory.instrument_spec(instrument)

    if spec["kind"] == "mono":
        notes = T.transcribe_monophonic(wav, spec)
    else:
        notes = T.transcribe_polyphonic(wav, spec)
    print(f"notes={len(notes)}")
    if notes:
        lo = min(n["pitch"] for n in notes)
        hi = max(n["pitch"] for n in notes)
        print(f"range={librosa.midi_to_note(lo)}..{librosa.midi_to_note(hi)}")

    y, sr = librosa.load(wav, sr=22050, mono=True)
    f0, _, _ = librosa.pyin(
        y, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C6"),
        sr=sr, frame_length=2048, hop_length=256,
    )
    times = librosa.times_like(f0, sr=sr, hop_length=256)
    midi = np.where(np.isfinite(f0), librosa.hz_to_midi(np.where(np.isfinite(f0), f0, 440.0)), np.nan)

    diffs = []
    unvoiced = 0
    for n in notes:
        a = n["start"] + 0.25 * (n["end"] - n["start"])
        b = n["start"] + 0.78 * (n["end"] - n["start"])
        sel = (times >= a) & (times <= b)
        vals = midi[sel]
        vals = vals[np.isfinite(vals)]
        if len(vals) < 2:
            unvoiced += 1
            continue
        diffs.append(float(np.median(vals)) - n["pitch"])
    d = np.array(diffs) if diffs else np.array([0.0])
    print(f"compared={len(d)} unvoiced={unvoiced}")
    print(f"within0.5={np.mean(np.abs(d) < 0.5) * 100:.1f}%  within1={np.mean(np.abs(d) < 1.0) * 100:.1f}%")
    h, e = np.histogram(np.clip(d, -6.5, 6.5), bins=np.arange(-6.5, 7.5, 1))
    print("hist:", [(int(e[i]), int(c)) for i, c in enumerate(h) if c])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
