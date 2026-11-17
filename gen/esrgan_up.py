"""Real-ESRGAN x4 upscale via spandrel, then downscale to the requested size.

    python esrgan_up.py <src> <dst> <width> <height>
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from spandrel import ModelLoader

ROOT = Path(__file__).resolve().parent
WEIGHTS = ROOT / "models" / "RealESRGAN_x4.pth"


def to_t(img: Image.Image) -> torch.Tensor:
    a = np.asarray(img.convert("RGB"), np.float32) / 255.0
    return torch.from_numpy(a).permute(2, 0, 1).unsqueeze(0)


def to_img(t: torch.Tensor) -> Image.Image:
    a = t.squeeze(0).clamp(0, 1).cpu().numpy().transpose(1, 2, 0)
    return Image.fromarray((a * 255.0 + 0.5).astype(np.uint8))


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    tw, th = int(sys.argv[3]), int(sys.argv[4])

    desc = ModelLoader().load_from_file(str(WEIGHTS))
    model = desc.model.eval()
    print(f"model: {desc.architecture.name if desc.architecture else '?'} scale={desc.scale}", flush=True)
    if torch.cuda.is_available():
        model = model.cuda()

    img = Image.open(src)
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    if img.mode == "RGBA":
        flat = Image.new("RGB", img.size, (255, 255, 255))
        flat.paste(img, mask=alpha)
        img = flat
    else:
        img = img.convert("RGB")

    t0 = time.time()
    x = to_t(img)
    if torch.cuda.is_available():
        x = x.cuda()
    with torch.no_grad():
        y = model(x)
    big = to_img(y)
    print(f"esrgan {img.size} -> {big.size} in {time.time() - t0:.1f}s", flush=True)
    big = big.resize((tw, th), Image.LANCZOS)

    if alpha is not None:
        a2 = alpha.resize((tw, th), Image.LANCZOS)
        big = big.convert("RGBA")
        big.putalpha(a2)
    big.save(dst)
    print(f"saved {dst} {big.size}", flush=True)


if __name__ == "__main__":
    main()
