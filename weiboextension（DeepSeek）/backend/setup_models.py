#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键下载望潮 TideWatcher 模型并放置到插件期望的目录结构。

用法:
  python backend/setup_models.py

依赖:
  pip install huggingface_hub

国内网络无法直连 HuggingFace 时，先设置镜像端点:
  PowerShell:  $env:HF_ENDPOINT="https://hf-mirror.com"
  CMD:         set HF_ENDPOINT=https://hf-mirror.com
  Linux/mac:   export HF_ENDPOINT=https://hf-mirror.com
"""
import shutil
import sys
from pathlib import Path

try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("缺少依赖 huggingface_hub，请先运行: pip install huggingface_hub")
    sys.exit(1)

REPO_ID = "RainbowLIght/tidewatcher-macbert-c2"

BACKEND = Path(__file__).resolve().parent
MODELS = BACKEND / "models"

TOKENIZER_FILES = [
    "config.json",
    "vocab.txt",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
]
ONNX_FILE = "model_int8.onnx"


def main() -> None:
    tmp = MODELS.parent / ".tmp_tidewatcher"
    tmp.mkdir(parents=True, exist_ok=True)
    print(f"从 {REPO_ID} 拉取模型 ...")
    try:
        snapshot_download(
            repo_id=REPO_ID,
            local_dir=str(tmp),
            allow_patterns=[ONNX_FILE] + TOKENIZER_FILES,
        )
    except Exception as exc:
        print(f"下载失败: {exc}")
        print("若网络无法访问 huggingface.co，请先设置镜像端点（见脚本头部注释）后重试。")
        sys.exit(1)

    # ONNX 模型 -> macbert_c_int8/
    int8_dir = MODELS / "macbert_c_int8"
    int8_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(tmp / ONNX_FILE, int8_dir / ONNX_FILE)

    # 分词器与配置 -> macbert_c/
    tok_dir = MODELS / "macbert_c"
    tok_dir.mkdir(parents=True, exist_ok=True)
    for name in TOKENIZER_FILES:
        shutil.copy(tmp / name, tok_dir / name)

    shutil.rmtree(tmp, ignore_errors=True)
    print("完成 ✓ 模型已就位：")
    print(f"  {int8_dir.relative_to(BACKEND.parent)}/model_int8.onnx")
    print(f"  {tok_dir.relative_to(BACKEND.parent)}/（分词器全套）")


if __name__ == "__main__":
    main()
