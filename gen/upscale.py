"""Img2img upscale / restyle for a finished illustration.

    python upscale.py <src.png> <dst.png> <width> <height> "<prompt>" [strength]

Keeps the composition, re-renders detail at the larger size.
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

import torch  # noqa: E402
from diffusers import StableDiffusionXLImg2ImgPipeline  # noqa: E402
from PIL import Image  # noqa: E402

DEFAULT_MODEL = ROOT / "models" / "animagine-xl-4.0.safetensors"
CONFIG = ROOT / "models" / "animagine-config"
BASE_NEG = (
    "lowres, bad anatomy, bad hands, text, error, cropped, worst quality, low quality, "
    "normal quality, jpeg artifacts, signature, watermark, blurry, extra limbs"
)


def flatten(path: str) -> Image.Image:
    im = Image.open(path)
    if im.mode == "RGBA":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        return bg
    return im.convert("RGB")


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    width, height = int(sys.argv[3]), int(sys.argv[4])
    prompt = sys.argv[5]
    strength = float(sys.argv[6]) if len(sys.argv) > 6 else 0.33

    pipe = StableDiffusionXLImg2ImgPipeline.from_single_file(
        str(DEFAULT_MODEL), torch_dtype=torch.float16, use_safetensors=True,
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

    img = flatten(src).resize((width, height), Image.LANCZOS)
    t0 = time.time()
    out = pipe(
        prompt=prompt, negative_prompt=BASE_NEG, image=img,
        strength=strength, num_inference_steps=36, guidance_scale=5.5,
    ).images[0]
    out.save(dst)
    print(f"{dst}  ({width}x{height}, strength {strength}, {time.time() - t0:.1f}s)")

    del pipe
    gc.collect()
    torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
