"""Local anime image generation (SDXL checkpoint, no API, no C: drive writes).

    python draw.py "1girl, cat ears, maid outfit" [--n 1] [--size 832x1216]
                   [--steps 28] [--cfg 5.5] [--seed 0] [--out name] [--model path]

Output goes to <gen>/out/*.png and the paths are printed one per line.
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

DEFAULT_MODEL = ROOT / "models" / "animagine-xl-4.0.safetensors"
OUT = ROOT / "out"
BASE_NEG = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, "
    "signature, watermark, username, blurry, artist name"
)


def parse_size(text: str) -> tuple[int, int]:
    w, h = text.lower().split("x")
    return int(w) // 8 * 8, int(h) // 8 * 8


def load_pipeline(model_path: Path, config_path: Path) -> StableDiffusionXLPipeline:
    if not model_path.exists():
        sys.exit(f"checkpoint not found: {model_path}")
    pipe = StableDiffusionXLPipeline.from_single_file(
        str(model_path),
        torch_dtype=torch.float16,
        use_safetensors=True,
        add_watermarker=False,
        local_files_only=True,
        config=str(config_path) if config_path.exists() else None,
    )
    pipe.set_progress_bar_config(disable=True)
    # diffusers 0.42 moved the VAE helpers onto the VAE itself.
    if hasattr(pipe.vae, "enable_slicing"):
        pipe.vae.enable_slicing()
    if hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()
    for name in ("enable_vae_slicing", "enable_vae_tiling", "enable_attention_slicing"):
        helper = getattr(pipe, name, None)
        if callable(helper):
            helper()
    # 8 GB of VRAM: keep everything on the GPU. model_cpu_offload() streams the
    # whole UNet up and back on every single step, which turned a 40 s render
    # into 16 minutes (961 s for 1024x640 / 26 steps).
    if torch.cuda.is_available():
        pipe.enable_model_cpu_offload()
    return pipe


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--size", default="832x1216")
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--cfg", type=float, default=5.5)
    ap.add_argument("--seed", type=int, default=-1)
    ap.add_argument("--negative", default="")
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--config", default=str(ROOT / "models" / "animagine-config"))
    ap.add_argument("--out", default="")
    ap.add_argument("--style", default="", help="appended to the prompt, e.g. 'masterpiece, best quality'")
    args = ap.parse_args()

    prompt = args.prompt.strip()
    if args.style:
        prompt = f"{prompt}, {args.style}"
    negative = (args.negative + ", " + BASE_NEG).strip(", ") if args.negative else BASE_NEG
    width, height = parse_size(args.size)
    seed = args.seed if args.seed >= 0 else random.randint(1, 2**31 - 1)

    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    pipe = load_pipeline(Path(args.model), Path(args.config))
    print(f"model loaded in {time.time() - t0:.1f}s", file=sys.stderr)

    for i in range(args.n):
        s = seed + i
        gen = torch.Generator(device="cpu").manual_seed(s)
        t1 = time.time()
        image = pipe(
            prompt=prompt,
            negative_prompt=negative,
            width=width,
            height=height,
            num_inference_steps=args.steps,
            guidance_scale=args.cfg,
            generator=gen,
        ).images[0]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{args.out}-{i + 1}.png" if args.out else f"draw-{stamp}-{s}.png"
        path = OUT / name
        image.save(path)
        print(f"{path}  ({width}x{height}, {args.steps} steps, seed {s}, {time.time() - t1:.1f}s)")

    del pipe
    gc.collect()
    torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
