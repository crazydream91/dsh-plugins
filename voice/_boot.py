"""导入 f5_tts 之前必须先跑这里：环境变量 + 绕过 torchcodec。"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).parent
os.environ.setdefault("HF_HOME", str(ROOT / "hf"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# ffmpeg：系统没有，用 imageio-ffmpeg 自带的那份，复制成规范名字挂到 PATH（transformers 的 ASR 要用）
_FF_SRC = Path(r"C:\Users\USER\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python"
               r"\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe")
_BIN = ROOT / "bin"
if _FF_SRC.exists():
    _BIN.mkdir(exist_ok=True)
    _dst = _BIN / "ffmpeg.exe"
    if not _dst.exists() or _dst.stat().st_size != _FF_SRC.stat().st_size:
        shutil.copy2(_FF_SRC, _dst)
    os.environ["PATH"] = str(_BIN) + os.pathsep + os.environ.get("PATH", "")

# torchaudio 2.11 的 load() 走 torchcodec，要 FFmpeg 共享库（本机没有）；改用 soundfile。
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
import torchaudio  # noqa: E402


def _load(path, *args, **kwargs):
    data, sr = sf.read(str(path), always_2d=True, dtype="float32")
    return torch.from_numpy(data.T.copy()), sr


torchaudio.load = _load
