"""Generate a closed-eyes version of the character's face via SDXL img2img.

    python blink.py [--strength 0.5] [--steps 30]

Reads   gen/out/wp-girl-hd.png  (1152x1619)
Writes  gen/out/face-closed.png  (same crop geometry as the input crop)
"""
from __future__ import annotations

import argparse
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

MODEL = ROOT / "models" / "animagine-xl-4.0.safetensors"
CONFIG = ROOT / "models" / "animagine-config"
SRC = ROOT / "out" / "wp-girl-hd.png"
DST = ROOT / "out" / "face-closed.png"

# face crop inside the 1152x1619 character: eyes at (603,95) and (693,113)
CROP = (470, 10, 830, 350)  # 360 x 340

PROMPT = (
    "1girl, solo, closed eyes, eyelashes, gentle smile, soft expression, "
    "white hair, bangs, detailed face, looking at viewer, "
    "masterpiece, best quality, very aesthetic"
)
NEG = (
    "open eyes, pupils, iris, deformed, bad anatomy, bad hands, text, watermark, "
    "lowres, worst quality, low quality, jpeg artifacts, blurry"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strength", type=float, default=0.5)
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--cfg", type=float, default=6.0)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--scale", type=float, default=1.5)
    ap.add_argument("--out", default=str(DST))
    args = ap.parse_args()

    src = Image.open(SRC).convert("RGB").crop(CROP)
    w, h = src.size
    gw = int(w * args.scale) // 8 * 8
    gh = int(h * args.scale) // 8 * 8
    big = src.resize((gw, gh), Image.LANCZOS)
    print(f"crop {src.size} -> {big.size}, strength {args.strength}", flush=True)

    pipe = StableDiffusionXLImg2ImgPipeline.from_single_file(
        str(MODEL),
        torch_dtype=torch.float16,
        use_safetensors=True,
        add_watermarker=False,
        local_files_only=True,
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

    gen = torch.Generator(device="cpu").manual_seed(args.seed)
    t0 = time.time()
    out = pipe(
        prompt=PROMPT,
        negative_prompt=NEG,
        image=big,
        strength=args.strength,
        num_inference_steps=args.steps,
        guidance_scale=args.cfg,
        generator=gen,
    ).images[0]
    print(f"生成 {time.time() - t0:.1f}s", flush=True)

    out = out.resize((w, h), Image.LANCZOS)
    out.save(args.out)
    src.save(str(ROOT / "out" / "face-crop-src.png"))
    print(f"saved {args.out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
