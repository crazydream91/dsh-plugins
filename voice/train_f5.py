"""F5-TTS 微调启动器：导入 _boot（音频/环境修补），再跑官方的 finetune_cli。"""
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import _boot  # noqa: F401,E402

CKPT = (ROOT / "hf/models--SWivid--F5-TTS/snapshots/84e5a410d9cead4de2f847e7c9369a6440bdfaca"
        "/F5TTS_v1_Base/model_1250000.safetensors")
CLI = ROOT / "tts/venv/Lib/site-packages/f5_tts/train/finetune_cli.py"

EPOCHS = sys.argv[1] if len(sys.argv) > 1 else "12"
LR = sys.argv[2] if len(sys.argv) > 2 else "1e-5"
BS = sys.argv[3] if len(sys.argv) > 3 else "2"

sys.argv = [
    "finetune",
    "--exp_name", "F5TTS_v1_Base",
    "--dataset_name", "ayan",
    "--finetune",
    "--pretrain", str(CKPT),
    "--tokenizer", "pinyin",
    "--epochs", EPOCHS,
    "--learning_rate", LR,
    "--batch_size_per_gpu", BS,
    "--batch_size_type", "sample",
    "--max_samples", BS,
    "--grad_accumulation_steps", "4",
    "--bnb_optimizer",
    "--num_warmup_updates", "10",
    "--save_per_updates", "15",
    "--last_per_updates", "15",
    "--keep_last_n_checkpoints", "4",
]
runpy.run_path(str(CLI), run_name="__main__")
