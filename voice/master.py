"""把合成出来的干声做成"冰雪 + 慈爱"的听感：柔一点、飘一点、咬字轻一点。

用法: python master.py in.wav out.wav [--air 1.0] [--soft 1.0]
"""
import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent
FF = ROOT / "bin/ffmpeg.exe"


def build_filter(air=1.0, soft=1.0):
    # 低切去掉嗡声；250 Hz 略收（不浑浊）；2.5–4.5k 略压（不刺、咬字变轻）；
    # 6.5k 高架 + 10.5k 低通（有空气感但不齿音）；两段极短反射做空间感
    parts = [
        # 掐掉首尾多余的静音（F5 经常在句尾多留 2–4 秒）
        "silenceremove=start_periods=1:start_threshold=-60dB:start_silence=0.25",
        "areverse",
        "silenceremove=start_periods=1:start_threshold=-60dB:start_silence=0.30",
        "areverse",
        "highpass=f=72",
        f"equalizer=f=250:t=q:w=0.9:g={-1.5 * soft:.1f}",
        f"equalizer=f=3000:t=q:w=1.0:g={-1.8 * soft:.1f}",
        f"equalizer=f=4300:t=q:w=1.2:g={-1.2 * soft:.1f}",
        f"highshelf=f=6500:g={2.2 * air:.1f}",
        "lowpass=f=10500",
        # 极轻的两拍反射，制造"空间里说话"的飘感，不是混响
        f"aecho=0.85:0.9:{int(45)}:{0.10 * air:.2f}",
        f"aecho=0.85:0.9:{int(90)}:{0.06 * air:.2f}",
    ]
    return ",".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--air", type=float, default=1.0)
    ap.add_argument("--soft", type=float, default=1.0)
    ap.add_argument("--peak", type=float, default=-3.0, help="最终峰值 dBFS")
    a = ap.parse_args()

    src = Path(a.src) if Path(a.src).is_absolute() else ROOT / a.src
    dst = Path(a.dst) if Path(a.dst).is_absolute() else ROOT / a.dst
    dst.parent.mkdir(parents=True, exist_ok=True)

    # loudnorm 收一下整体电平，再 peak 归一，避免忽大忽小
    af = build_filter(a.air, a.soft) + ",loudnorm=I=-19:TP=-4:LRA=9"
    subprocess.run([str(FF), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(src), "-af", af, "-ac", "1", "-ar", "24000", str(dst)],
                   check=True)
    print(f"out {dst}")


if __name__ == "__main__":
    main()
