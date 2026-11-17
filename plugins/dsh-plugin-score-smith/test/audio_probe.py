"""离线渲染自检：不占屏幕、不需要点击，直接用 OfflineAudioContext 跑播放器的合成链路，
把前 N 秒的峰值 / RMS 打出来。

用法（在 score-venv 的 python 下）：
    python test/audio_probe.py D:\\dsh 大肥鱼\\scores\\hoyo-pillow [秒数]

原理：把 render.build_player_html 生成的页面写进临时目录，末尾追加一段脚本，把
AudioContext 换成 OfflineAudioContext，跑 startRendering()，再把 PEAK/RMS 写进 DOM；
然后用 Edge `--headless=new --dump-dom --virtual-time-budget=...` 把 DOM 抓回来解析。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY_DIR = os.path.join(os.path.dirname(HERE), "py")
sys.path.insert(0, PY_DIR)

import render  # noqa: E402

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PROBE_JS = r"""
<script>
(async () => {
  const info = {};
  try {
    const SECONDS = __SECONDS__;
    const off = new OfflineAudioContext(2, Math.floor(44100 * SECONDS), 44100);
    window.AudioContext = function () { return off; };
    window.webkitAudioContext = window.AudioContext;
    ensureCtx();
    buildEvents();
    info.gains = state.gains.length;
    info.master = state.master ? state.master.gain.value : null;
    info.events = state.events.length;
    info.firstT = state.events.length ? state.events[0].t : null;
    info.firstDur = state.events.length ? state.events[0].dur : null;
    info.firstNote = state.events.length ? JSON.stringify(state.events[0]) : null;
    const secPerQL = 60 / SCORE.meta.tempo;
    info.tempo = SCORE.meta.tempo;
    info.meter = SCORE.meta.meter;
    info.secPerQL = secPerQL;
    state.events.forEach((e) => { e.t = e.o * secPerQL; e.dur = e.d * secPerQL; });
    state.events.sort((a, b) => a.t - b.t);
    info.sampleT = state.events.length ? state.events[0].t : null;
    info.sampleT1 = state.events.length > 1 ? state.events[1].t : null;
    info.until = SECONDS;
    state.startCtxTime = 0;
    state.idx = 0;
    state.playing = true;
    scheduleEvents(0, SECONDS);
    info.idxAfter = state.idx;

    // 对照组：一条最直接的链路，绕过 playNote，看离线上下文本身有没有问题
    const ref = off.createOscillator(), refGain = off.createGain();
    ref.frequency.value = 440;
    refGain.gain.value = 0.5;
    ref.connect(refGain); refGain.connect(off.destination);
    ref.start(0.1); ref.stop(0.3);

    const buffer = await off.startRendering();
    let peak = 0, sum = 0, n = 0;
    for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
      const data = buffer.getChannelData(ch);
      for (let i = 0; i < data.length; i++) {
        const v = Math.abs(data[i]);
        if (v > peak) peak = v;
        sum += data[i] * data[i];
        n += 1;
      }
    }
    const rms = Math.sqrt(sum / Math.max(1, n));
    const el = document.createElement('div');
    el.id = 'probe';
    el.textContent = 'PEAK=' + peak.toFixed(5) + ' RMS=' + rms.toFixed(5)
      + ' TRACKS=' + SCORE.tracks.length
      + ' EVENTS=' + state.events.length
      + ' NOTE0=' + (SCORE.tracks[0] && SCORE.tracks[0].notes.length)
      + ' INFO=' + JSON.stringify(info);
    document.body.appendChild(el);
  } catch (err) {
    const el = document.createElement('div');
    el.id = 'probe';
    el.textContent = 'PROBE_ERROR ' + (err && err.message ? err.message : String(err))
      + ' INFO=' + JSON.stringify(info);
    document.body.appendChild(el);
  }
})();
</script>
"""


def find_edge() -> str:
    for path in EDGE_CANDIDATES:
        if os.path.isfile(path):
            return path
    raise SystemExit("找不到 Edge")


def main() -> int:
    project = sys.argv[1] if len(sys.argv) > 1 else r"D:\dsh 大肥鱼\scores\hoyo-pillow"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
    with open(os.path.join(project, "score.json"), encoding="utf-8") as handle:
        model = json.load(handle)

    title = model.get("title") or "probe"
    html = render.build_player_html(model, [], title, "离线渲染自检")
    html = html.replace("</body>", PROBE_JS.replace("__SECONDS__", str(seconds)) + "</body>")

    out_dir = os.path.join(tempfile.gettempdir(), "score_smith_probe")
    os.makedirs(out_dir, exist_ok=True)
    page = os.path.join(out_dir, "probe.html")
    with open(page, "w", encoding="utf-8") as handle:
        handle.write(html)

    url = "file:///" + page.replace("\\", "/").replace(" ", "%20")
    started = time.time()
    proc = subprocess.run(
        [find_edge(), "--headless=new", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=20000", "--dump-dom", url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    dom = proc.stdout or ""
    match = re.search(r'id="probe"[^>]*>([^<]*)<', dom)
    print(json.dumps({
        "ok": bool(match),
        "elapsedSec": round(time.time() - started, 2),
        "probe": match.group(1).strip() if match else None,
        "rc": proc.returncode,
        "page": page,
        "stderrTail": (proc.stderr or "")[-300:],
    }, ensure_ascii=False))
    return 0 if match and "PEAK=" in match.group(1) else 1


if __name__ == "__main__":
    raise SystemExit(main())
