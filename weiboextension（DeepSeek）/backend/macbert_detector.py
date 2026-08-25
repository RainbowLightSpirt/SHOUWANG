# -*- coding: utf-8 -*-
"""
macbert_detector.py — MacBert 攻击检测模块（实装版）
=====================================================
职责：作为「快检门卫」顶替 SnowNLP，用 ONNX INT8 量化的 MacBert 模型做
      攻击性二分类打分。词表/SnowNLP 保留为回退层（模型不可用时门卫不挂）。

模型来源：bert_experiment c 组最优配置 c_macbert_lr3e-5_ep4（F1 0.8992）
量化：ONNX 动态 INT8（复测 F1 0.9004，延迟 mean 34ms，冷启动 0.38s）

设计要点：
- lazy 加载（首次调用才建 session），失败静默回退，不影响主流程
- max_len=64 与训练/复测一致，tokenizer 行为与 transformers 对齐
- 阈值分层：score > 0.9 定案攻击 / < 0.1 定案安全，模糊区走原逻辑
  （本模块只负责打分，分层决策在 app.py 里做）
"""
import math
import os
import time
from functools import lru_cache
from pathlib import Path

try:
    import numpy as np
    import onnxruntime as ort
except ImportError:
    np = None
    ort = None

# 模型与词表路径（backend/models/ 下）
BASE_DIR = Path(__file__).resolve().parent
INT8_MODEL_PATH = BASE_DIR / "models" / "macbert_c_int8" / "model_int8.onnx"
TOKENIZER_DIR = BASE_DIR / "models" / "macbert_c"

MAX_LEN = 64          # 与训练（train_one.py --max-len 64）及复测一致
ATTACK_THRESHOLD = 0.5  # 二分类判定阈值（softmax p(attack) >= 0.5）
VERY_HIGH = 0.9       # 分层：高分定案攻击
VERY_LOW = 0.1        # 分层：低分定案安全


@lru_cache(maxsize=1)
def _get_session():
    if ort is None or not INT8_MODEL_PATH.exists():
        return None
    try:
        return ort.InferenceSession(str(INT8_MODEL_PATH), providers=["CPUExecutionProvider"])
    except Exception:
        return None


@lru_cache(maxsize=1)
def _get_tokenizer():
    try:
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(str(TOKENIZER_DIR))
    except Exception:
        return None


def is_available() -> bool:
    return _get_session() is not None and _get_tokenizer() is not None


def predict(text: str) -> dict:
    """单条打分。

    返回 dict：{score(攻击概率 0~1), is_attack(bool), latency_ms(float)}
    模型不可用或出错返回 None（调用方走词表/SnowNLP 回退）。
    """
    sess = _get_session()
    tok = _get_tokenizer()
    if sess is None or tok is None:
        return None
    try:
        enc = tok(
            text, truncation=True, max_length=MAX_LEN,
            padding="max_length", return_tensors="np",
        )
        feed = {k: v.astype("int64") for k, v in enc.items()}
        t0 = time.perf_counter()
        logits = sess.run(None, feed)[0][0]
        latency_ms = (time.perf_counter() - t0) * 1000
        e0, e1 = float(logits[0]), float(logits[1])
        # softmax 二分类概率：p(attack) = sigmoid(logit1 - logit0)
        score = 1.0 / (1.0 + math.exp(-(e1 - e0)))
        return {
            "score": score,
            "is_attack": e1 >= e0,
            "latency_ms": round(latency_ms, 1),
        }
    except Exception:
        return None


if __name__ == "__main__":
    # 冒烟自检：加载耗时 + 样例打分
    t0 = time.perf_counter()
    ok = is_available()
    load_s = time.perf_counter() - t0
    print(f"[self-check] available={ok} 加载耗时 {load_s:.2f}s")
    for s in ["你真是个傻逼，去死吧", "今天天气不错，心情很好", "nmsl", "cnm 你妈的", "这家店的服务太差了"]:
        r = predict(s)
        if r is None:
            print(f"  {s!r} -> None（模型不可用）")
        else:
            print(f"  {s!r} -> score={r['score']:.3f} is_attack={r['is_attack']} lat={r['latency_ms']}ms")
