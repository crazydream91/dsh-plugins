"""Tiled img2img upscale: split into overlapping horizontal bands so each pass
fits comfortably in 8 GB of VRAM, then blend the overlaps.

    python upscale_tiled.py <src.png> <dst.png> <scale> "<prompt>" [strength]
"""
from __future__ import annotations

import gc
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(ROOT / "hf"))
os.environ.setdefault("HF_HUB_CACHE", str(ROOT / "hf" / "hub"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "max_split_size_mb:256")

import numpy as np  # noqa: E402
import torch  # noqa: E402
from diffusers import StableDiffusionXLImg2ImgPipeline  # noqa: E402
from PIL import Image  # noqa: E402

MODEL = ROOT / "models" / "animagine-xl-4.0.safetensors"
CONFIG = ROOT / "models" / "animagine-config"
NEG = (
    "lowres, bad anatomy, bad hands, text, error, cropped, worst quality, low quality, "
    "normal quality, jpeg artifacts, signature, watermark, blurry, extra limbs, "
    "two heads, deformed"
)


def flatten(path: str) -> Image.Image:
    im = Image.open(path)
    if im.mode == "RGBA":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        return bg
    return im.convert("RGB")


def eight(v: float) -> int:
    return max(64, int(round(v / 8)) * 8)


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    scale = float(sys.argv[3])
    prompt = sys.argv[4]
    strength = float(sys.argv[5]) if len(sys.argv) > 5 else 0.30

    base = flatten(src)
    W, H = base.size
    out_w, out_h = eight(W * scale), eight(H * scale)

    # 两条水平带，带 overlap
    bh = eight(H * 0.56)
    bands = [(0, bh), (H - bh, H)]
    print(f"src {W}x{H} -> {out_w}x{out_h}, bands={bands}", flush=True)

    pipe = StableDiffusionXLImg2ImgPipeline.from_single_file(
        str(MODEL), torch_dtype=torch.float16, use_safetensors=True,
        add_watermarker=False, local_files_only=True,
        config=str(CONFIG) if CONFIG.exists() else None,
    )
    pipe.set_progress_bar_config(disable=True)
    if hasattr(pipe.vae, "enable_slicing"):
        pipe.vae.enable_slicing()
    if hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()
    for name in ("enable_vae_slicing", "enable_vae_tiling", "enable_attention_slicing"):
        helper = getattr(pipe, name, None)
        if callable(helper):
            helper()
    if torch.cuda.is_available():
        pipe.enable_model_cpu_offload()

    pieces = []
    for i, (y0, y1) in enumerate(bands):
        crop = base.crop((0, y0, W, y1))
        cw, ch = eight(crop.width * scale), eight(crop.height * scale)
        big = crop.resize((cw, ch), Image.LANCZOS)
        t0 = time.time()
        out = pipe(
            prompt=prompt, negative_prompt=NEG, image=big,
            strength=strength, num_inference_steps=36, guidance_scale=5.5,
        ).images[0]
        pieces.append((y0 * scale, np.asarray(out, np.float32)))
        print(f"band {i} [{y0}:{y1}] -> {cw}x{ch}  {time.time() - t0:.1f}s", flush=True)

    canvas = np.zeros((out_h, out_w, 3), np.float32)
    canvas[:] = 255.0
    (oy0, arr0), (oy1, arr1) = pieces
    oy0, oy1 = int(round(oy0)), int(round(oy1))
    canvas[oy0:oy0 + arr0.shape[0]] = arr0
    canvas[oy1:oy1 + arr1.shape[0]] = arr1
    # 重叠区线性混合
    lo = max(oy0, oy1)
    hi = min(oy0 + arr0.shape[0], oy1 + arr1.shape[0])
    if hi > lo:
        a = np.linspace(0, 1, hi - lo, dtype=np.float32)[:, None, None]
        canvas[lo:hi] = arr0[lo - oy0:hi - oy0] * (1 - a) + arr1[lo - oy1:hi - oy1] * a
    Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8)).save(dst)
    print(f"saved {dst}  {out_w}x{out_h}", flush=True)

    del pipe
    gc.collect()
    torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
