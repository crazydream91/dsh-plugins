"""下载 B 站视频的画面并按秒抽帧，供看界面用。

用法: python grab_frames.py BV号 [--page 1] [--every 5] [--width 1280]
输出: voice/watch/<BV>-p<n>/frames/f01.jpg ...
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
ROOT = Path(__file__).parent
FFMPEG = ROOT / "bin" / "ffmpeg.exe"


def api(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    return json.load(urllib.request.urlopen(req, timeout=25))


def main():
    bv = sys.argv[1]
    page = 1
    every = 5.0
    width = 1280
    args = sys.argv[2:]
    for i, arg in enumerate(args):
        if arg == "--page":
            page = int(args[i + 1])
        elif arg == "--every":
            every = float(args[i + 1])
        elif arg == "--width":
            width = int(args[i + 1])

    view = api(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}")["data"]
    pages = view["pages"]
    cid = pages[page - 1]["cid"]
    print(f"{bv} p{page} cid={cid} 时长 {pages[page-1]['duration']}s")

    data = api(f"https://api.bilibili.com/x/player/playurl?bvid={bv}&cid={cid}&fnval=16")["data"]
    videos = data["dash"]["video"]
    best = sorted(videos, key=lambda v: v.get("bandwidth", 0))[-1]
    out_dir = ROOT / "watch" / f"{bv}-p{page}"
    out_dir.mkdir(parents=True, exist_ok=True)
    vid = out_dir / "v.m4s"
    if not vid.exists() or vid.stat().st_size < 10000:
        req = urllib.request.Request(best["baseUrl"], headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        blob = urllib.request.urlopen(req, timeout=180).read()
        vid.write_bytes(blob)
        print(f"视频 {len(blob)/1048576:.1f} MB")

    frames = out_dir / "frames"
    frames.mkdir(exist_ok=True)
    for old in frames.glob("*.jpg"):
        old.unlink()
    cmd = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error", "-i", str(vid),
           "-vf", f"fps=1/{every},scale={width}:-1", str(frames / "f%02d.jpg")]
    subprocess.run(cmd, check=False)
    made = sorted(frames.glob("*.jpg"))
    print(f"抽帧 {len(made)} 张 → {frames}")


if __name__ == "__main__":
    main()
