# 本地出图脚本

基于 `diffusers` + SDXL 系模型，在 8 GB 显存上跑通。

## 脚本

| 文件 | 作用 |
|---|---|
| `draw.py` | 主入口：单图/批量、负向提示词、seed 复现、峰值归一 |
| `upscale.py` / `upscale_tiled.py` | 分块放大，避开显存墙 |
| `esrgan_up.py` | ESRGAN 放大 |
| `cutout.py` | 去背景 |
| `blink.py` | 给立绘做眨眼帧 |

## 坑

- `from_single_file` 会联网拉配置 → 离线环境必须显式给 `--config` 指向配置目录
- diffusers 0.42 把 VAE 方法挪到了 `pipe.vae.enable_slicing()`，旧方法要用 `hasattr` 兜底
- 从国内 pypi 镜像装包可能把 torch 换成 CPU 版，装完必须验 `torch.cuda.is_available()`