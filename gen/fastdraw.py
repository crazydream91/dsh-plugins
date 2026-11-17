"""SDXL + Lightning LoRA: 4-step generation for 8 GB cards.

    python fastdraw.py "prompt" --size 1152x1728 --steps 4 --out name
"""
from __future__ import annotations

import argparse
import gc
import os
import random
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
from diffusers import StableDiffusionXLPipeline  # noqa: E402

MODEL = ROOT / "models" / "animagine-xl-4.0.safetensors"
CONFIG = ROOT / "models" / "animagine-config"
LORA = ROOT / "models" / "sdxl_lightning_4step_lora.safetensors"
OUT = ROOT / "out"

BASE_NEG = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, "
    "signature, watermark, username, blurry, artist name"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--size", default="1152x1728")
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--cfg", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--out", default="fast")
    args = ap.parse_args()

    w, h = (int(v) // 8 * 8 for v in args.size.lower().split("x"))
    seed = args.seed if args.seed >= 0 else random.randint(0, 2**31 - 1)

    t0 = time.time()
    pipe = StableDiffusionXLPipeline.from_single_file(
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
    pipe.load_lora_weights(str(LORA), adapter_name="lightning")
    pipe.set_adapters(["lightning"], adapter_weights=[1.0])
    if torch.cuda.is_available():
        pipe.enable_model_cpu_offload()
    print(f"加载 {time.time() - t0:.1f}s", flush=True)

    gen = torch.Generator(device="cpu").manual_seed(seed)
    t1 = time.time()
    img = pipe(
        prompt=args.prompt,
        negative_prompt="" if args.cfg <= 0 else BASE_NEG,
        width=w, height=h,
        num_inference_steps=args.steps,
        guidance_scale=args.cfg,
        generator=gen,
    ).images[0]
    dt = time.time() - t1
    p = OUT / f"{args.out}-{seed}.png"
    img.save(p)
    print(f"{p}  ({w}x{h}, {args.steps} steps, seed {seed}, {dt:.1f}s)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
