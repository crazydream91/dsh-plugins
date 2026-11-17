"""把一段合成音频用本地 whisper 听回来，用来核对"多字/少字"。

用法: python check_text.py <wav> [<wav> ...]
"""
import sys
from pathlib import Path

import _boot  # noqa: F401,E402

import torch
from transformers import pipeline

ROOT = Path(__file__).parent
MODEL = ROOT / "models/whisper-large-v3-turbo"


def main(paths):
    pipe = pipeline("automatic-speech-recognition", model=str(MODEL),
                    device=0 if torch.cuda.is_available() else -1)
    for p in paths:
        res = pipe(str(p), generate_kwargs={"language": "chinese", "task": "transcribe"},
                   return_timestamps=False)
        print(f"{Path(p).name}\t{res['text'].strip()}", flush=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
