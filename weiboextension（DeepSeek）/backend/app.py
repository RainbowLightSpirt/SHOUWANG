import json
import os
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
try:
    import httpx
except ImportError:
    httpx = None
try:
    from snownlp import SnowNLP
except ImportError:
    SnowNLP = None
try:
    import macbert_detector
except ImportError:
    macbert_detector = None

# 加载 .env
def _load_dotenv():
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
_load_dotenv()

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_FORCE_DIRECT = os.getenv("DEEPSEEK_FORCE_DIRECT", "1").strip().lower() not in {"0","false","no","off"}
try:
    DEEPSEEK_TIMEOUT = float(os.getenv("DEEPSEEK_TIMEOUT", "30"))
except ValueError:
    DEEPSEEK_TIMEOUT = 30.0

PASS = "PASS"
# 本地词库
# 负面情绪词（扩充版）：覆盖纯吐槽表达，命中即 suggest（提示条），不升级弹窗
NEGATIVE_WORDS = {"傻","滚","闭嘴","垃圾","废物","蠢","去死","恶心","讨厌","生气","吵","骂",
                  "烦","破事","气死","无语","离谱","心累","难受","委屈","崩溃","emo","烦躁",
                  "焦虑","摆烂","内耗","想死","不想活","烦死","破防","受不了","凭什么",
                  "好累","累了","累死","唉"}
AGGRESSIVE_WORDS = {"去死","弄死","打死","杀","威胁","傻逼","废物","垃圾","闭嘴"}
# 事实线索：收紧后的具体事实词（去掉“是/为/最”等高频虚词，避免吐槽句误判事实线索→误弹窗）
# 数字检测收紧：只有“数字+明确事实量词”才像断言（14亿人口/2400万），纯数字（666/用了3年）不算
# 事实线索词加入省会/海拔/距离/长度/首位/诞生/纪录/冠军/最高/最长/最早/最短（组合词，单字“最”不进），量词加“米”
# 离线模拟验证：正常句误报零新增，假事实句捞回翻倍
FACT_CLAIM_HINTS = {"等于","首都","人口","面积","发明","出生","去世","成立于","位于",
                  "省会","海拔","距离","长度","首位","诞生","纪录","冠军","最高","最长","最早","最短"}
FACT_NUM_PATTERN = re.compile(r'\d+\s*(亿|万|公里|千米|公斤|吨|%|％|摄氏度|米)')

# FastAPI app
@asynccontextmanager
async def lifespan(_app: FastAPI):
    print("[startup] OpenAI available:", OpenAI is not None)
    print("[startup] httpx available:", httpx is not None)
    print("[startup] SnowNLP available:", SnowNLP is not None)
    print("[startup] DEEPSEEK_API_KEY configured:", bool(DEEPSEEK_API_KEY))
    yield

app = FastAPI(title="Intervention Service", lifespan=lifespan)

# CORS
@app.middleware("http")
async def cors_handler(request: Request, call_next):
    if request.method == "OPTIONS":
        return Response(status_code=200, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "86400"
        })
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

# LLM client
@lru_cache(maxsize=1)
def get_llm_client():
    if not (DEEPSEEK_API_KEY and OpenAI is not None):
        return None
    if DEEPSEEK_FORCE_DIRECT and httpx is not None:
        http_client = httpx.Client(trust_env=False, timeout=DEEPSEEK_TIMEOUT)
        return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, http_client=http_client)
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# 辅助函数
def _safe_json_loads(raw):
    if not raw:
        return None
    # Ensure we are working with a str for regex/search operations
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except:
            raw = raw.decode("utf-8", "ignore")
    try:
        return json.loads(raw)
    except:
        match = re.search(r'\{.*\}', str(raw), re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except:
                pass
    return None

def _as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"true","1","yes","y"}
    return bool(v)

def _empty_emotion():
    return {"emotion_label":"中性","secondary_emotion":"无内容","emotion_intensity":0.0,"is_negative":False,"tone":"平静","intervention_mode":"无动作","description":"","suggestion":"","reply_style":"平静"}

def _empty_fact():
    return {"fact_check":PASS,"fact_check_status":"pass","has_factual_error":False,"corrected_fact":"","fact_explanation":"","llm_error":"","has_hate_speech":False,"needs_intervention":False,"intervention_level":"none","ai_response":""}

# 危险信号 → 干预级别（none/suggest/warn）
# 语义解耦：攻击词/事实线索 → warn（弹窗）；负面情绪（词表或 SnowNLP 判负）→ suggest（提示条）
# 注意：纯情绪负面不管 intensity 多高都封顶 suggest，只有攻击才警告（产品语义：吐槽温柔介入，攻击才弹窗）
# intensity 保留为输出字段，供 Bert 融合/校准用，不直接驱动档位
def _map_danger_to_level(intensity: float, has_hate: bool = False, has_fact_claim: bool = False, is_negative: bool = False) -> str:
    if has_hate or has_fact_claim:
        return "warn"        # 攻击词或需 LLM 核实（事实线索）：走 deep 弹窗
    if is_negative:
        return "suggest"     # 负面情绪：输入框上方小提示条，不唤醒 LLM
    return "none"

# 终端显示从前端截获的内容（调试/监控功能）
def _log_capture(text: str, source: str = "capture") -> None:
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    safe = (text or "").replace("\n", " ↵ ")
    print(f"[CAPTURE] {ts} [{source}] 长度={len(safe)} 内容: {safe}")

# 快速检查（本地规则）
def quick_check_text(text: str) -> dict:
    # MacBert 门卫打分（实装：ONNX INT8 量化的 c 组最优模型，替代 SnowNLP 担任攻击主判）
    # 放在最前：低中文占比文本（nmsl/cnm 等纯缩写攻击）也必须过模型，不能先被过滤掉
    macbert = None
    if macbert_detector is not None and macbert_detector.is_available():
        macbert = macbert_detector.predict(text)
    # 过滤无意义文本（中文字符少于30%视为无意义）
    # 豁免：攻击词表命中 或 MacBert 判攻击，两者都防纯缩写/低中文占比攻击漏拦
    lower_scan = text.lower()
    hit_aggressive_word = any(w in lower_scan for w in AGGRESSIVE_WORDS)
    chinese_chars = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    total = len(text.strip())
    if total > 0 and (chinese_chars / total) < 0.3 and not hit_aggressive_word and not (macbert and macbert["is_attack"]):
        # 非中文内容过多，不干预
        return {
            "stage": "quick",
            "should_intervene": False,
            "is_preliminary": True,
            "is_reviewing": False,
            "emotion_analysis": _empty_emotion(),
            "has_emotional_risk": False,
            "has_fact_claim": False,
            "has_hate_speech": False,
            "fact_check": PASS,
            "fact_check_status": "pass",
            "intervention_level": "none",
            "ai_response": ""
        }
    # 情绪分析
    stripped = text.strip()
    score = 0.0
    lower = text.lower()
    # 负面词（词表命中是独立负面信号，不依赖 SnowNLP/intensity）
    has_neg_word = any(w in lower for w in NEGATIVE_WORDS)
    if has_neg_word:
        score += 0.3
    # 攻击词
    has_hate = any(w in lower for w in AGGRESSIVE_WORDS)
    if has_hate:
        score = max(score, 0.8)
    if macbert and macbert["is_attack"]:
        has_hate = True
        score = max(score, 0.8)
    if any(mark in text for mark in ("!","！","？？","??")):
        score += 0.15
    # SnowNLP（判负是独立负面信号）
    snow_negative = False
    if SnowNLP is not None:
        try:
            snow_score = SnowNLP(stripped).sentiments
            score = max(score, 1 - snow_score)
            snow_negative = snow_score < 0.5
        except:
            pass
    intensity = min(round(score, 3), 1.0)
    # 负面判定：词表命中 或 SnowNLP 判负，独立于 intensity 数值
    is_negative = has_neg_word or snow_negative
    has_emotional_risk = has_hate or is_negative
    # 事实线索：词表 + 数字量词 + “是”字陈述句模式（R2：含“是”且排除人称主语）
    # 离线模拟验证：A版+R2 联合覆盖 17/25=68%，正常句误报 13/225=5.8%
    _person_hints = ("我", "你", "他", "她", "我们", "你们", "他们", "她们", "咱", "大家", "自己")
    _is_claim = ("是" in text) and not any(p in text for p in _person_hints)
    has_fact_claim = any(hint in text for hint in FACT_CLAIM_HINTS) or bool(FACT_NUM_PATTERN.search(text)) or _is_claim
    should_intervene = has_emotional_risk or has_fact_claim

    emotion = {
        "emotion_label": "愤怒" if has_hate else ("负面" if is_negative else "中性"),
        "secondary_emotion": "攻击倾向" if has_hate else ("不满" if is_negative else "平稳"),
        "emotion_intensity": intensity,
        "is_negative": is_negative or has_hate,
        "tone": "激动" if has_hate else ("紧张" if is_negative else "平静"),
        "intervention_mode": "提醒" if has_hate else ("劝导" if is_negative else "无动作"),
        "description": "",
        "suggestion": "建议用更温和的方式表达。" if is_negative or has_hate else "",
        "reply_style": "温和"
    }
    return {
        "stage": "quick",
        "should_intervene": should_intervene,
        "is_preliminary": True,
        "is_reviewing": should_intervene,
        "emotion_analysis": emotion,
        "has_emotional_risk": has_emotional_risk,
        "has_fact_claim": has_fact_claim,
        "has_hate_speech": has_hate,
        "fact_check": PASS,
        "fact_check_status": "pending" if has_fact_claim else "pass",
        "intervention_level": _map_danger_to_level(intensity, has_hate, has_fact_claim, is_negative),
        "ai_response": "",
        "macbert_score": round(macbert["score"], 4) if macbert else None,
        "macbert_latency_ms": macbert["latency_ms"] if macbert else None,
    }

# 深度检查（调用大模型）
def deep_check_text(text: str) -> dict:
    quick = quick_check_text(text)
    client = get_llm_client()
    if client is None:
        # 大模型不可用，返回快速结果但标记 llm_used=False
        return {**quick, "stage":"deep", "is_preliminary":False, "is_reviewing":False, "llm_used":False, "should_intervene":False, "ai_response":""}
    # 构建提示词（大模型触发后只有弹窗/不弹窗两档，suggest 只在本地 quick 阶段出现）
    system = (
        "You are a Chinese online speech intervention assistant. Review the user's draft for emotional/aggressive expression and factual errors. "
        "If no issues, return needs_intervention=false. Return JSON only with fields: "
        "has_emotional_issue(boolean), emotional_reply, has_factual_error(boolean), corrected_fact, fact_explanation, "
        "has_hate_speech(boolean), needs_intervention(boolean), intervention_level(warn or none), ai_response. "
        "intervention_level allows ONLY two values: warn (show a popup warning) or none (do not intervene). "
        "When has_factual_error is true, default to warn; choose none only when the context is clearly harmless "
        "(e.g. obvious joke, fiction, or sarcasm). "
        "ai_response is a concise, gentle, second-person Chinese suggestion."
    )
    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role":"system","content":system}, {"role":"user","content":text}],
            temperature=0.2,
            max_tokens=600
        )
        # Safely extract content (handle different SDK shapes and possible None)
        raw_content = ""
        try:
            choice0 = response.choices[0]
            # openai-like: choice0.message.content
            msg = getattr(choice0, "message", None)
            if msg is not None:
                # msg may be a dict-like or object with .content
                if isinstance(msg, dict):
                    raw_content = msg.get("content", "")
                else:
                    raw_content = getattr(msg, "content", "")
            else:
                # fallback: choice0.text or dict shape
                if isinstance(choice0, dict):
                    raw_content = choice0.get("text") or choice0.get("message", {}).get("content", "")
                else:
                    raw_content = getattr(choice0, "text", "") or str(choice0)
        except Exception:
            raw_content = ""
        raw = (raw_content or "").strip()
        parsed = _safe_json_loads(raw) or {}
    except Exception as e:
        # 出错则返回快速结果但标记 llm_used=False, should_intervene=False
        return {**quick, "stage":"deep", "is_preliminary":False, "is_reviewing":False, "llm_used":False, "should_intervene":False, "ai_response":"", "llm_error":str(e)}

    # 提取字段
    has_emotional = _as_bool(parsed.get("has_emotional_issue", False))
    has_error = _as_bool(parsed.get("has_factual_error", False))
    has_hate = _as_bool(parsed.get("has_hate_speech", False)) or quick.get("has_hate_speech", False)
    needs = _as_bool(parsed.get("needs_intervention", False))
    ai_resp = parsed.get("ai_response", "")
    # 白捡的深度危险值：LLM 提示词要求输出 emotion_intensity，之前没提取，现在存下来备用（可与 quick 的 intensity 融合/校准）
    try:
        llm_emotion_intensity = float(parsed.get("emotion_intensity", 0.0) or 0.0)
    except (TypeError, ValueError):
        llm_emotion_intensity = 0.0
    # 如果 ai_response 里明确表示无倾向，且无其他风险，则强制设为 false
    if ai_resp and any(p in ai_resp for p in ["没有明显的倾向", "无明显倾向", "中性", "没有风险"]):
        if not has_emotional and not has_error and not has_hate:
            needs = False
            ai_resp = ""
    should_intervene = (has_emotional or has_error or has_hate or needs) and bool(ai_resp)  # 必须有实际建议才干预
    # 如果 should_intervene 为 False，清空 ai_response
    if not should_intervene:
        ai_resp = ""
    # 合并 emotion 信息
    emotion = quick.get("emotion_analysis", _empty_emotion())
    # 大模型触发后只有 warn/none 两档，suggest 只在本地 quick 阶段出现
    # 事实错误：prompt 引导默认 warn；LLM 明确判 none（玩笑/无害语境）→ 豁免不弹窗
    _lv = (parsed.get("intervention_level") or "").strip().lower()
    if _lv in ("warn", "block", "none"):
        pass  # 合法两档（block 为更强档保留）
    elif has_error:
        _lv = "warn"   # 事实错误且 LLM 输出模糊：保守弹窗
    else:
        _lv = "warn" if should_intervene else "none"   # 回退原判定
    if has_error and not ai_resp:
        ai_resp = f"这句话可能存在事实错误：{parsed.get('corrected_fact', '') or parsed.get('fact_explanation', '')}".strip()
    if _lv == "none":
        should_intervene = False
        ai_resp = ""
    return {
        "stage": "deep",
        "should_intervene": should_intervene,
        "is_preliminary": False,
        "is_reviewing": False,
        "llm_used": True,
        "emotion_analysis": emotion,
        "has_emotional_risk": quick.get("has_emotional_risk", False),
        "has_emotional_issue": has_emotional,
        "emotional_reply": parsed.get("emotional_reply", ""),
        "has_fact_claim": quick.get("has_fact_claim", False),
        "has_factual_error": has_error,
        "corrected_fact": parsed.get("corrected_fact", ""),
        "fact_explanation": parsed.get("fact_explanation", ""),
        "has_hate_speech": has_hate,
        "intervention_level": _lv,
        "llm_emotion_intensity": llm_emotion_intensity,  # 深度危险值（新提取）
        "ai_response": ai_resp,
        "fact_check": PASS if not has_error else "ERROR",
        "fact_check_status": "error" if has_error else "pass",
        "llm_error": "",
    }

# API endpoints
@app.post("/quick_check")
async def quick_check(request: Request):
    data = await request.json()
    text = data.get("text", "")
    _log_capture(text, "quick")  # 终端显示前端截获内容
    if len(text.strip()) < 2:
        return {
            "stage": "quick",
            "should_intervene": False,
            "is_preliminary": True,
            "is_reviewing": False,
            "emotion_analysis": _empty_emotion(),
            "has_emotional_risk": False,
            "has_fact_claim": False,
            "has_hate_speech": False,
            "fact_check": PASS,
            "fact_check_status": "pass",
            "intervention_level": "none",
            "ai_response": ""
        }
    return quick_check_text(text)

@app.post("/deep_check")
async def deep_check(request: Request):
    data = await request.json()
    text = data.get("text", "")
    _log_capture(text, "deep")  # 终端显示进入深度检查的内容
    if len(text.strip()) < 2:
        return {
            "stage": "deep",
            "should_intervene": False,
            "is_preliminary": False,
            "is_reviewing": False,
            "llm_used": True,
            "emotion_analysis": _empty_emotion(),
            "has_emotional_risk": False,
            "has_emotional_issue": False,
            "emotional_reply": "",
            "has_fact_claim": False,
            "has_factual_error": False,
            "corrected_fact": "",
            "fact_explanation": "",
            "has_hate_speech": False,
            "intervention_level": "none",
            "ai_response": "",
            "fact_check": PASS,
            "fact_check_status": "pass",
            "llm_error": "",
        }
    result = deep_check_text(text)
    return result

@app.post("/chat")
async def chat(request: Request):
    data = await request.json()
    messages = data.get("messages", [])
    original = data.get("original_text", "")
    risk_info = data.get("risk_info", {}) or {}
    if original:
        _log_capture(original, "chat")  # 终端显示弹窗对话时的原文
    client = get_llm_client()
    if client is None:
        return {"reply":"建议用事实和感受表达。"}

    # 立住话题：让大模型知道自己正在劝哪句发言、为什么被拦
    original_display = (original or "").strip()
    reasons = []
    if _as_bool(risk_info.get("has_hate_speech")) or _as_bool(risk_info.get("has_emotional_issue")):
        reasons.append("情绪化/攻击性表达")
    if _as_bool(risk_info.get("has_factual_error")):
        reasons.append("事实性错误")
    if not reasons:
        reasons.append("存在不适宜直接发布的内容")
    reason_text = "、".join(reasons)

    if original_display:
        topic_block = (
            "用户刚才写下了一句发言，因【" + reason_text + "】被系统拦截，无法直接发送。"
            "你的任务是围绕这句发言对用户进行劝导，帮 TA 把意思用更温和、客观、准确的方式重新表达出来。"
            "被拦截的发言原文：\"" + original_display + "\""
        )
    else:
        topic_block = (
            "用户刚刚有一句发言因【" + reason_text + "】被系统拦截，无法直接发送。"
            "你的任务是围绕这句被拦截的发言劝导用户，帮 TA 用更温和、客观、准确的方式重新表达。"
            "原文未提供，请根据对话内容推断具体话题。"
        )

    system_prompt = (
        "You are a warm and patient Chinese online speech intervention assistant. "
        "你正在与一位刚刚发言被拦截的用户对话。"
        "始终保持话题聚焦在那句被拦截的发言上，围绕它劝导并帮助用户重新表达。"
        "语气温和、自然，用第二人称，不要偏离话题。"
    )
    chat_messages = [
        {"role":"system","content":system_prompt},
        {"role":"system","content":topic_block}
    ] + messages[-10:]
    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=chat_messages,
            temperature=0.7,
            max_tokens=500
        )
        # Safely extract reply content similar to above
        reply_content = ""
        try:
            c0 = resp.choices[0]
            m = getattr(c0, "message", None)
            if m is not None:
                if isinstance(m, dict):
                    reply_content = m.get("content", "")
                else:
                    reply_content = getattr(m, "content", "")
            else:
                if isinstance(c0, dict):
                    reply_content = c0.get("text") or c0.get("message", {}).get("content", "")
                else:
                    reply_content = getattr(c0, "text", "") or str(c0)
        except Exception:
            reply_content = ""
        return {"reply": (reply_content or "").strip()}
    except:
        return {"reply":"请用事实和具体诉求表达观点。"}

@app.post("/log_modal")
async def log_modal(request: Request):
    data = await request.json()
    reason = data.get("reason", "")
    print(f"[log_modal] 弹窗原因: {reason}")
    return {"status":"ok"}

@app.get("/healthz")
async def health():
    return {"status":"ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)