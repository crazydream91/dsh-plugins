"""看视频：给一个 BV 号，把视频里的「话」全部听写成带时间轴的文字。

用法:
    python watch.py BV1zU7E6PEi2            # 第 1 个分 P
    python watch.py BV1zU7E6PEi2 --page 2   # 第 2 个分 P
    python watch.py BV1zU7E6PEi2 --full     # 打印全文

产出: voice/watch/<BV号>-p<页码>/audio.wav + transcript.md

注意：能听懂的只有"说出来的话"。画面里的字幕、图表、动作它看不到。
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "watch"
FF = ROOT / "bin/ffmpeg.exe"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
API = "https://api.bilibili.com"


def api(path: str, **params) -> dict:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API}{path}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("code") != 0:
        raise SystemExit(f"接口报错 {path}: {data.get('code')} {data.get('message')}")
    return data["data"]


def download(url: str, dst: Path):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=60) as r, dst.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                print(f"\r  下载 {got / total * 100:5.1f}%  {got >> 20}/{total >> 20} MB", end="", flush=True)
        print()


def mmss(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def transcribe(wav: Path):
    import _boot  # noqa: F401
    import torch
    from transformers import pipeline

    pipe = pipeline("automatic-speech-recognition",
                    model=str(ROOT / "models/whisper-large-v3-turbo"),
                    device=0 if torch.cuda.is_available() else -1,
                    chunk_length_s=30, stride_length_s=5)
    res = pipe(str(wav), return_timestamps=True,
               generate_kwargs={"language": "chinese", "task": "transcribe"})
    return res.get("chunks") or [], res.get("text", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bvid")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--full", action="store_true", help="打印全文而不是只给开头")
    a = ap.parse_args()

    bvid = a.bvid.strip()
    if not bvid.startswith("BV"):
        bvid = "BV" + bvid
    if not bvid[2:].strip():
        raise SystemExit("BV 号不对")

    t0 = time.time()
    view = api("/x/web-interface/view", bvid=bvid)
    pages = view["pages"]
    if a.page < 1 or a.page > len(pages):
        raise SystemExit(f"这个视频有 {len(pages)} 个分 P")
    page = pages[a.page - 1]
    cid, title, dur = page["cid"], view["title"], page.get("duration") or view["duration"]
    print(f"[视频] {title}  P{a.page}  {mmss(dur)}")

    work = OUT / f"{bvid}-p{a.page}"
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "audio.wav"

    if not wav.exists():
        play = api("/x/player/playurl", bvid=bvid, cid=cid, fnval=16, fourk=0)
        audios = (play.get("dash") or {}).get("audio") or []
        if not audios:
            raise SystemExit("这个视频没有 DASH 音轨（可能是直播回放或会员内容）")
        src = audios[0]
        print(f"[音轨] id={src.get('id')} {src.get('bandwidth', 0) // 1000} kbps")
        m4s = work / "audio.m4s"
        download(src["baseUrl"], m4s)
        subprocess.run([str(FF), "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(m4s), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
        m4s.unlink(missing_ok=True)

    import soundfile as sf
    info = sf.info(str(wav))
    print(f"[音频] {info.frames / info.samplerate:.1f}s  -> 转写中（{info.frames / info.samplerate / 60:.0f} 分钟音频，耐心等）")

    chunks, text = transcribe(wav)
    lines = []
    for c in chunks:
        ts = c.get("timestamp") or (None, None)
        start = ts[0] if ts[0] is not None else 0
        lines.append(f"[{mmss(start)}] {c['text'].strip()}")
    md = work / "transcript.md"
    md.write_text("\n".join([f"# {title} P{a.page}", "", f"时长 {mmss(dur)}", ""] + lines),
                  encoding="utf-8")

    n = len(text)
    print(f"[完成] 用时 {time.time() - t0:.0f}s  共 {n} 字  段落 {len(lines)}  -> {md}")
    if a.full:
        print("\n" + text)
    else:
        print("\n" + text[:400] + ("…" if n > 400 else ""))


if __name__ == "__main__":
    sys.exit(main())
