"""小晏的耳朵：常驻的 whisper 听写服务。

POST /asr  {"path": "C:/.../x.wav"}  ->  {"text": "...", "seconds": 1.2}
GET  /health                        ->  {"ok": true, "model": "whisper-large-v3-turbo"}

模型只在启动时加载一次（约 6 秒），之后每句 1~3 秒。
"""
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _boot  # noqa: E402  —— 修 torchaudio/ffmpeg

import torch  # noqa: E402
from transformers import pipeline  # noqa: E402

PORT = int(os.environ.get("ASR_PORT", "8123"))
MODEL_DIR = ROOT / "models" / "whisper-large-v3-turbo"

print(f"[asr] 加载模型 {MODEL_DIR}", flush=True)
started = time.time()
asr = pipeline(
    "automatic-speech-recognition",
    model=str(MODEL_DIR),
    device=0 if torch.cuda.is_available() else -1,
)
print(f"[asr] 就绪，用时 {time.time() - started:.1f}s，端口 {PORT}", flush=True)


def transcribe(path: str) -> str:
    result = asr(
        path,
        generate_kwargs={"language": "chinese", "task": "transcribe"},
        chunk_length_s=30,
        stride_length_s=5,
    )
    text = (result.get("text") or "").strip()
    # whisper 在没有语音时会吐"字幕由…提供"这类幻觉，直接丢掉
    junk = ("字幕", "志愿者", "优优独播", "请不吝点赞", "订阅", "打赏")
    if len(text) < 2 or any(word in text for word in junk):
        return ""
    return text


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "model": MODEL_DIR.name})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/asr":
            self._send(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as error:  # noqa: BLE001
            self._send(400, {"ok": False, "error": f"bad json: {error}"})
            return
        path = payload.get("path") or ""
        if not path or not Path(path).exists():
            self._send(400, {"ok": False, "error": "file not found"})
            return
        begin = time.time()
        try:
            text = transcribe(path)
        except Exception as error:  # noqa: BLE001
            self._send(500, {"ok": False, "error": str(error)})
            return
        self._send(200, {"ok": True, "text": text, "seconds": round(time.time() - begin, 2)})

    def log_message(self, fmt, *args):  # 别刷屏
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
