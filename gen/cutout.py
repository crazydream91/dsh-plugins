"""Cut the character out of its background using BriaRMBG-1.4 (local weights).

    python cutout.py <src> <dst> [upscale_factor]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(ROOT / "hf"))
sys.path.insert(0, str(ROOT / "rmbg"))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import torch  # noqa: E402
from PIL import Image, ImageFilter  # noqa: E402
from safetensors.torch import load_file  # noqa: E402

from briarmbg import BriaRMBG  # noqa: E402

SNAP = ROOT / "hf" / "hub" / "models--briaai--RMBG-1.4" / "snapshots" / "2ceba5a5efaec153162aedea169f76caf9b46cf8"
NET = None


def load_net():
    global NET
    if NET is None:
        NET = BriaRMBG()
        NET.load_state_dict(load_file(str(SNAP / "model.safetensors")), strict=False)
        NET.eval()
    return NET


@torch.no_grad()
def alpha_of(rgb: Image.Image) -> Image.Image:
    net = load_net()
    w, h = rgb.size
    small = rgb.resize((1024, 1024), Image.LANCZOS)
    t = torch.from_numpy(np.asarray(small, np.float32) / 255.0)
    t = t.unsqueeze(0).permute(0, 3, 1, 2).contiguous()
    t = (t - 0.5) / 1.0
    m = net(t)
    while isinstance(m, (list, tuple)):
        m = m[0]
    m = m.squeeze().cpu().numpy()
    lo, hi = float(m.min()), float(m.max())
    m = (m - lo) / (hi - lo + 1e-8)
    return Image.fromarray((m * 255).astype(np.uint8)).resize((w, h), Image.LANCZOS)


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    scale = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    im = Image.open(src).convert("RGB")
    if scale > 0:
        im = im.resize((int(round(im.width * scale)), int(round(im.height * scale))), Image.LANCZOS)
        im = im.filter(ImageFilter.UnsharpMask(radius=2.0, percent=55, threshold=3))
    a = alpha_of(im)
    out = im.convert("RGBA")
    out.putalpha(a)
    out.save(dst)
    print(f"{dst}  {out.width}x{out.height}  bbox={out.getchannel('A').getbbox()}")


if __name__ == "__main__":
    main()
