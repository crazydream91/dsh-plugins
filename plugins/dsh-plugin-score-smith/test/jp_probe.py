"""简谱播放器离线自检：headless Edge 里用 OfflineAudioContext 渲染，看真的有波形。

用法：python test/jp_probe.py <jianpu.html> [秒数]
输出：PEAK / RMS / NOTES 三个数，PEAK 明显大于 0 才算播放器真能出声。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

PROBE = """
<div id="probe">pending</div>
<script>
(async function () {
  try {
    const r = await window.__jpProbe(__SECONDS__);
    document.getElementById('probe').textContent =
      'PROBE ' + JSON.stringify({ peak: r.peak, rms: r.rms, notes: r.notes });
  } catch (e) {
    document.getElementById('probe').textContent = 'PROBE-FAIL ' + e;
  }
})();
</script>
"""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: jp_probe.py <jianpu.html> [seconds]")
        return 2
    src = os.path.abspath(sys.argv[1])
    seconds = sys.argv[2] if len(sys.argv) > 2 else "12"
    with open(src, "r", encoding="utf-8") as fh:
        page = fh.read()
    page = page.replace("</body>", PROBE.replace("__SECONDS__", seconds) + "</body>")

    out_dir = os.path.join(tempfile.gettempdir(), "score_smith_probe")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "jp_probe.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)

    url = "file:///" + out.replace("\\", "/")
    cmd = [EDGE, "--headless=new", "--disable-gpu", "--mute-audio",
           "--virtual-time-budget=30000", "--dump-dom", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    dom = proc.stdout or ""
    m = re.search(r"PROBE(-FAIL)?\s*(\{[^<]*\}|[^<]*)", dom)
    if not m:
        print("没抓到探针输出，DOM 长度", len(dom))
        return 1
    if m.group(1):
        print("PROBE 失败：", (m.group(2) or "").strip()[:300])
        return 1
    data = json.loads(m.group(2))
    print(f"PEAK={data['peak']:.6f} RMS={data['rms']:.6f} NOTES={data['notes']}")
    return 0 if data["peak"] > 0.01 else 3


if __name__ == "__main__":
    sys.exit(main())
