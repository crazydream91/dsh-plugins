"""录系统正在播放的声音（WASAPI loopback），按秒数或手动停止。

用法:
    python record_loopback.py OUT.wav --seconds 90
    python record_loopback.py OUT.wav --seconds 120 --samplerate 44100

用 soundcard 库抓默认扬声器的回环流，分块写盘，不会把整首歌堆在内存里。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import soundcard as sc
import soundfile as sf


def record(path: str, seconds: float, samplerate: int = 44100, channels: int = 2, block: float = 0.5) -> dict:
    speaker = sc.default_speaker()
    mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    total = 0
    t0 = time.time()
    peak = 0.0
    with sf.SoundFile(path, mode="w", samplerate=samplerate, channels=channels, subtype="PCM_16") as out:
        with mic.recorder(samplerate=samplerate, channels=channels) as rec:
            while True:
                elapsed = time.time() - t0
                if seconds > 0 and elapsed >= seconds:
                    break
                chunk = rec.record(numframes=int(samplerate * block))
                if chunk.size == 0:
                    continue
                if chunk.ndim == 1:
                    chunk = chunk[:, None]
                if chunk.shape[1] != channels:
                    if chunk.shape[1] == 1:
                        chunk = np.repeat(chunk, channels, axis=1)
                    else:
                        chunk = chunk[:, :channels]
                peak = max(peak, float(np.max(np.abs(chunk))))
                out.write(chunk)
                total += chunk.shape[0]
                print(f"\rrecorded {total / samplerate:6.1f}s  peak={peak:.3f}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return {"path": path, "seconds": round(total / samplerate, 2), "samplerate": samplerate, "peak": round(peak, 4)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("output")
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--samplerate", type=int, default=44100)
    ap.add_argument("--channels", type=int, default=2)
    args = ap.parse_args(argv[1:])
    info = record(args.output, args.seconds, args.samplerate, args.channels)
    import json
    print(json.dumps({"ok": True, **info}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
