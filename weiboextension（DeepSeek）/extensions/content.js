
const BACKEND_URLS = ['http://127.0.0.1:8000', 'http://localhost:8000'];
const DEBUG = true;
const MIN_TEXT_LENGTH = 2;          // 至少个字符才检测
const CHECK_DEBOUNCE_MS = 700;       // 防抖
const SAME_TEXT_SKIP_MS = 6000;      // 相同文本跳过
const MODAL_CLOSE_COOLDOWN_MS = 5000; // 关闭后冷却

let activeModal = null;
let pendingCheck = false;
let lastCheckedText = '';
let lastCheckedTime = 0;
let lastFocusedEditable = null;
let lastKnownText = '';
let checkTimer = null;
let detectionPausedUntil = 0;
let activeBackendUrl = BACKEND_URLS[0];
let isComposing = false;
let intervenedFingerprints = new Set();  // 已弹窗文本指纹：对同一段发言只弹一次

function log(...args) {
    if (DEBUG) console.log('[AI]', ...args);
}

// ========== 辅助函数 ==========
function normalizeText(v) { return (v || '').replace(/\s+/g, ' ').trim(); }

// ---------- 重触发检测：对同一段发言只弹一次 ----------
function textFingerprint(text) {
    // 宽松指纹：去空白、小写、只留中英文数字，让「废物」和「废物！/废物?」算同一段
    const s = normalizeText(text || '').toLowerCase().replace(/[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]/g, '');
    if (!s) return '';
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
    return h.toString(36);
}
function hasIntervened(text) {
    const fp = textFingerprint(text);
    return fp ? intervenedFingerprints.has(fp) : false;
}
function markIntervened(text) {
    const fp = textFingerprint(text);
    if (fp) intervenedFingerprints.add(fp);
}
function resetInterventionMemory() {
    if (intervenedFingerprints.size > 0) {
        intervenedFingerprints.clear();
        log('干预记忆已重置（输入框空白/已发送）');
    }
}
function isEditable(el) {
    if (!el) return false;
    const tag = el.tagName;
    return tag === 'TEXTAREA' || tag === 'INPUT' || el.isContentEditable || el.getAttribute('contenteditable') === 'true';
}
function isVisible(el) {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function getEditableText(el) {
    if (!el) return '';
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') return normalizeText(el.value);
    return normalizeText(el.innerText || el.textContent);
}
function getDeepActiveElement() {
    let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
}
function findEditableFromTarget(target) {
    let cur = target;
    for (let i = 0; cur && i < 10; i++, cur = cur.parentElement) {
        if (isEditable(cur) && isVisible(cur)) return cur;
    }
    return null;
}
function collectEditableFields() {
    const selectors = ['textarea', 'input[type="text"]', '[contenteditable]', '[role="textbox"]'];
    const found = [];
    for (const sel of selectors) {
        document.querySelectorAll(sel).forEach(el => {
            if (isEditable(el) && isVisible(el) && !el.disabled && !el.readOnly) found.push(el);
        });
    }
    return found;
}
function getCurrentTextCandidate(event) {
    let field = null, text = '';
    if (event) {
        field = findEditableFromTarget(event.target);
        if (field) text = getEditableText(field);
    }
    if (!field || text.length < MIN_TEXT_LENGTH) {
        const active = getDeepActiveElement();
        if (isEditable(active) && isVisible(active)) {
            field = active;
            text = getEditableText(active);
        }
    }
    if (!field || text.length < MIN_TEXT_LENGTH) {
        if (lastFocusedEditable && isVisible(lastFocusedEditable)) {
            field = lastFocusedEditable;
            text = getEditableText(lastFocusedEditable);
        }
    }
    if (!field || text.length < MIN_TEXT_LENGTH) {
        const all = collectEditableFields();
        for (const f of all) {
            const t = getEditableText(f);
            if (t.length >= MIN_TEXT_LENGTH) { field = f; text = t; break; }
        }
    }
    if (field) lastFocusedEditable = field;
    if (text) lastKnownText = text;
    return { field, text };
}

// ========== API 请求 ==========
async function apiFetch(path, options = {}) {
    const candidates = [activeBackendUrl, ...BACKEND_URLS.filter(u => u !== activeBackendUrl)];
    let lastError = null;
    for (const base of candidates) {
        try {
            const resp = await fetch(base + path, options);
            activeBackendUrl = base;
            return resp;
        } catch (e) { lastError = e; }
    }
    throw lastError || new Error('All backends failed');
}

// ========== 弹窗 UI（沿用原设计） ==========
function closeModal() {
    if (activeModal && activeModal.parentNode) {
        activeModal.parentNode.removeChild(activeModal);
    }
    activeModal = null;
    pendingCheck = false;
    detectionPausedUntil = Date.now() + MODAL_CLOSE_COOLDOWN_MS;
    clearTimeout(checkTimer);
    log('modal closed, cooldown until', detectionPausedUntil);
}

function appendBubble(chat, text, isUser) {
    const bubble = document.createElement('div');
    bubble.textContent = text;
    bubble.style.cssText = isUser
        ? 'background:#2454d6;color:#fff;border-radius:12px 12px 4px 12px;padding:10px 14px;margin-bottom:10px;font-size:14px;align-self:flex-end;max-width:85%;line-height:1.5;'
        : 'background:#fff;border:1px solid #e2e8f0;border-radius:12px 12px 12px 4px;padding:10px 14px;margin-bottom:10px;font-size:14px;color:#172033;max-width:85%;line-height:1.5;';
    chat.appendChild(bubble);
}

function renderModalContent(result, originalText, messages) {
    const chat = activeModal.querySelector('[data-role="chat-body"]');
    const status = activeModal.querySelector('[data-role="status-bar"]');
    const reasonBar = activeModal.querySelector('[data-role="reason-bar"]');
    const emotion = result.emotion_analysis || {};
    if (status) {
        status.textContent = `当前情绪：${emotion.emotion_label || '未知'} · 提醒类型：${result.intervention_level || '建议'}`;
    }
    if (reasonBar) {
        reasonBar.textContent = result.modal_reason || '';
    }
    if (chat) {
        chat.innerHTML = '';
        const firstReply = result.ai_response || '建议用更温和的方式表达。';
        appendBubble(chat, firstReply, false);
        if (result.has_factual_error && result.corrected_fact) {
            appendBubble(chat, `事实纠正：${result.corrected_fact}`, false);
        }
        if (result.fact_explanation) {
            appendBubble(chat, `说明：${result.fact_explanation}`, false);
        }
        chat.dataset.originalText = originalText || '';
        // 把风险类型一并存下，发给 /chat 让大模型知道为什么拦截
        chat.dataset.riskInfo = JSON.stringify({
            has_hate_speech: !!result.has_hate_speech,
            has_emotional_issue: !!result.has_emotional_issue,
            has_factual_error: !!result.has_factual_error,
            intervention_level: result.intervention_level || ''
        });
        chat.__messages = messages || [{ role: 'assistant', content: firstReply }];
    }
}

function createModal(result, originalText, reason) {
    // 严格守卫：只有 deep 检查且 llm_used 为 true 且 should_intervene 为 true 才弹窗
    if (!result || !result.should_intervene || !result.llm_used || result.stage !== 'deep') {
        log('弹窗守卫阻止：', { should: result?.should_intervene, llm: result?.llm_used, stage: result?.stage });
        return;
    }

    // 记录原因
    console.log('[AI] 弹窗生成原因:', reason);
    console.log('[AI] 结果详情:', result);

    // 后端日志
    apiFetch('/log_modal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            reason,
            preview: result.preview || '',
            should_intervene: result.should_intervene,
            llm_used: result.llm_used,
            has_factual_error: result.has_factual_error,
            has_emotional_issue: result.has_emotional_issue,
            has_hate_speech: result.has_hate_speech,
            intervention_level: result.intervention_level,
        })
    }).catch(e => console.warn('log_modal failed', e));

    if (activeModal) {
        renderModalContent({ ...result, modal_reason: reason }, originalText);
        return;
    }

    // 构造 DOM
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,0.45);z-index:2147483647;display:flex;align-items:center;justify-content:center;font-family:-apple-system,sans-serif;';
    const modal = document.createElement('div');
    modal.style.cssText = 'background:#fff;border-radius:12px;width:440px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 24px 70px rgba(15,23,42,0.28);overflow:hidden;';
    const header = document.createElement('div');
    header.style.cssText = 'padding:14px 18px;border-bottom:1px solid #e5e7eb;display:flex;align-items:center;justify-content:space-between;';
    const title = document.createElement('div');
    title.textContent = 'AI 理性助手';
    title.style.cssText = 'font-size:16px;font-weight:700;color:#111827;';
    const closeBtn = document.createElement('button');
    closeBtn.textContent = '×';
    closeBtn.style.cssText = 'background:none;border:none;font-size:24px;color:#64748b;cursor:pointer;';
    closeBtn.onclick = closeModal;
    const statusBar = document.createElement('div');
    statusBar.setAttribute('data-role', 'status-bar');
    statusBar.style.cssText = 'padding:9px 18px 0 18px;font-size:13px;color:#64748b;';
    const reasonBar = document.createElement('div');
    reasonBar.setAttribute('data-role', 'reason-bar');
    reasonBar.style.cssText = 'padding:4px 18px 10px 18px;font-size:12px;color:#94a3b8;min-height:18px;';
    const chat = document.createElement('div');
    chat.setAttribute('data-role', 'chat-body');
    chat.style.cssText = 'flex:1;overflow-y:auto;padding:14px 18px 10px 18px;display:flex;flex-direction:column;background:#f8fafc;';
    const inputWrap = document.createElement('div');
    inputWrap.style.cssText = 'padding:12px 16px 16px 16px;border-top:1px solid #e5e7eb;display:flex;gap:8px;background:#fff;';
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = '回复 AI，继续对话...';
    input.style.cssText = 'flex:1;border:1px solid #cbd5e1;border-radius:20px;padding:9px 14px;font-size:14px;outline:none;';
    const sendBtn = document.createElement('button');
    sendBtn.textContent = '发送';
    sendBtn.style.cssText = 'border:none;border-radius:20px;padding:9px 16px;background:#2454d6;color:#fff;font-size:14px;cursor:pointer;';

    inputWrap.append(input, sendBtn);
    header.append(title, closeBtn);
    modal.append(header, statusBar, reasonBar, chat, inputWrap);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    activeModal = overlay;
    markIntervened(originalText);  // 弹窗真正挂上后才记指纹，避免 DOM 异常导致误记

    renderModalContent({ ...result, modal_reason: reason }, originalText);
    input.focus();

    // 发送消息（多轮对话）
    function sendMessage() {
        const msg = input.value.trim();
        if (!msg) return;
        const messages = chat.__messages || [];
        messages.push({ role: 'user', content: msg });
        chat.__messages = messages;
        appendBubble(chat, msg, true);
        input.value = '';
        chat.scrollTop = chat.scrollHeight;
        let riskInfo = {};
        try { riskInfo = JSON.parse(chat.dataset.riskInfo || '{}'); } catch (e) {}
        apiFetch('/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ messages, original_text: chat.dataset.originalText || '', risk_info: riskInfo })
        })
        .then(r => r.json())
        .then(data => {
            const reply = data.reply || '好的，请继续。';
            messages.push({ role: 'assistant', content: reply });
            chat.__messages = messages;
            appendBubble(chat, reply, false);
            chat.scrollTop = chat.scrollHeight;
        })
        .catch(() => {
            appendBubble(chat, '连接后端失败，请稍后重试。', false);
        });
    }
    input.addEventListener('keydown', e => { if (e.key === 'Enter') sendMessage(); });
    sendBtn.onclick = sendMessage;
}

// ---------- 建议档：输入框上方小提示条（不打扰，不唤醒 LLM） ----------
let suggestionTipEl = null;
const SUGGESTION_TIP_TEXT = '这句话有点冲，要不要换个温和的说法？';
function showSuggestionTip(field, text) {
    if (!field || hasIntervened(text)) return;
    markIntervened(text);  // 与警告弹窗共用指纹：同一段发言只提醒一次，也不重复弹窗
    if (suggestionTipEl && suggestionTipEl.parentNode) suggestionTipEl.parentNode.removeChild(suggestionTipEl);
    const tip = document.createElement('div');
    tip.textContent = SUGGESTION_TIP_TEXT;
    // 样式与弹窗同一套蓝白体系，轻量不抢眼
    tip.style.cssText = 'position:fixed;z-index:2147483646;background:#eff6ff;border:1px solid #bfdbfe;color:#1e40af;font-size:13px;line-height:1.5;padding:8px 14px;border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,0.10);cursor:pointer;font-family:-apple-system,sans-serif;max-width:min(360px, 80vw);';
    const rect = field.getBoundingClientRect();
    tip.style.left = Math.max(8, rect.left) + 'px';
    tip.style.top = Math.max(8, rect.top - 46) + 'px';
    tip.addEventListener('click', () => {
        if (tip.parentNode) tip.parentNode.removeChild(tip);
        if (suggestionTipEl === tip) suggestionTipEl = null;
    });
    document.body.appendChild(tip);
    suggestionTipEl = tip;
    setTimeout(() => {
        if (tip.parentNode) tip.parentNode.removeChild(tip);
        if (suggestionTipEl === tip) suggestionTipEl = null;
    }, 6000);  // 6 秒自动消失
    log('suggestion tip shown');
}

// suggest 送审：deep 复核判无害时撤销提示条（三态裁决）
function removeSuggestionTip() {
    if (suggestionTipEl && suggestionTipEl.parentNode) {
        suggestionTipEl.parentNode.removeChild(suggestionTipEl);
    }
    suggestionTipEl = null;
}

// ========== 核心检查流程 ==========
function maybeCheckCurrentText(event, reason) {
    if (Date.now() < detectionPausedUntil) { log('skip cooldown'); return; }
    if (activeModal) { log('skip modal open'); return; }
    if (pendingCheck) { log('skip pending'); return; }

    const { field, text } = getCurrentTextCandidate(event);
    if (!text || text.length < MIN_TEXT_LENGTH) return;

    const now = Date.now();
    if (text === lastCheckedText && now - lastCheckedTime < SAME_TEXT_SKIP_MS) {
        log('skip same text');
        return;
    }
    // 重触发检测：这段发言已经弹过窗，不重复打扰
    if (hasIntervened(text)) {
        log('skip already intervened text');
        return;
    }

    pendingCheck = true;
    lastCheckedText = text;
    lastCheckedTime = now;

    log('starting quick check...', text.slice(0, 50));

    // 第一步：quick_check
    apiFetch('/quick_check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
    })
    .then(r => r.json())
    .then(quickData => {
        log('quick result:', quickData);
        if (!quickData.should_intervene) {
            pendingCheck = false;
            return; // 终止
        }
        // 建议档：提示条是 quick 层本地产物，秒显；后台送 deep 只裁决是否升级弹窗
        // LLM 判 warn → 弹窗覆盖；判 none → 提示条保持自然消失（不撤销，吐槽给提示条语义不丢）
        if (quickData.intervention_level === 'suggest') {
            showSuggestionTip(field, text);
            apiFetch('/deep_check', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text })
            })
            .then(r => r.json())
            .then(deepData => {
                log('deep result (suggest review):', deepData);
                if (!deepData || !deepData.llm_used) return;  // LLM 不可用/失败：提示条自然消失
                if (deepData.should_intervene && deepData.llm_used && deepData.intervention_level === 'warn') {
                    // 升级：检出事实错误/攻击 → 弹窗覆盖提示条
                    let reasonStr = '';
                    if (deepData.has_factual_error) reasonStr = '大模型事实核查生成';
                    else if (deepData.has_hate_speech) reasonStr = '大模型情绪判定生成';
                    else reasonStr = '大模型综合判断';
                    deepData.preview = text.slice(0, 100);
                    removeSuggestionTip();
                    createModal(deepData, text, reasonStr);
                }
                // 判 none：提示条照常显示，6 秒自然消失（不撤销）
            })
            .catch(() => { /* 网络失败：提示条自然消失 */ });
            pendingCheck = false;
            return;
        }
        // 警告档及以上：走 deep 弹窗（原有逻辑）
        return apiFetch('/deep_check', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text })
        })
        .then(r => r.json())
        .then(deepData => {
            log('deep result:', deepData);
            // 双重检查：quick 和 deep 都 true，且 deep 使用了 LLM
            if (quickData.should_intervene && deepData.should_intervene && deepData.llm_used) {
                // 生成原因
                let reasonStr = '';
                if (deepData.has_factual_error) reasonStr = '大模型事实核查生成';
                else if (deepData.has_emotional_issue || deepData.has_hate_speech) reasonStr = '大模型情绪判定生成';
                else if (deepData.intervention_level === 'warn') reasonStr = '大模型风险警告';
                else reasonStr = '大模型综合判断';
                // 补充预览
                deepData.preview = text.slice(0, 100);
                createModal(deepData, text, reasonStr);
            } else {
                log('双重条件未满足，不弹窗');
            }
            pendingCheck = false;
        });
    })
    .catch(err => {
        log('check error', err);
        pendingCheck = false;
    });
}

// ========== 事件绑定 ==========
function scheduleCheck(event, reason) {
    if (Date.now() < detectionPausedUntil || activeModal) return;
    // 注：不在 input 事件里重置干预记忆。之前这里检测“输入框空白就 reset”，
    // 但删字重打/切输入框/短暂清空都会误清记忆，导致同一句再弹。
    // 记忆只在「点击发送且确认清空」时重置（见下方 click 监听），保证未修改的内容不重复弹。
    clearTimeout(checkTimer);
    checkTimer = setTimeout(() => maybeCheckCurrentText(event, reason), CHECK_DEBOUNCE_MS);
}

document.addEventListener('input', e => scheduleCheck(e, 'input'), true);
document.addEventListener('keyup', e => scheduleCheck(e, 'keyup'), true);
document.addEventListener('paste', e => scheduleCheck(e, 'paste'), true);
document.addEventListener('compositionend', e => {
    isComposing = false;
    scheduleCheck(e, 'compositionend');
}, true);
document.addEventListener('compositionstart', () => { isComposing = true; }, true);
// 点击发送/发布类按钮：一段发言已提交，重置「只弹一次」记忆
// 误触防护：微博满屏「评论/回复」，所以不立即重置，延迟确认输入框真的被清空（发送成功）才重置
// 点无关按钮时主输入框内容还在 → 不重置，不会误清记忆
const SEND_BTN_RE = /发送|发布|评论|回帖/;
document.addEventListener('click', e => {
    if (activeModal) return;
    const el = e.target;
    const label = (el && (el.textContent || (el.getAttribute && el.getAttribute('aria-label')) || '')) || '';
    if (!SEND_BTN_RE.test(label)) return;
    const targetField = lastFocusedEditable;
    const before = targetField ? getEditableText(targetField) : '';
    setTimeout(() => {
        const after = targetField ? getEditableText(targetField) : '';
        if (before && !after) resetInterventionMemory();
    }, 400);
}, true);

// 定期扫描（兜底）
setInterval(() => {
    // 注：不在这里重置干预记忆（之前会因 lastFocusedEditable 短暂为空误清，导致同一句再弹）。
    // 记忆只在 click 发送确认清空时重置。
    if (!activeModal && !pendingCheck && Date.now() >= detectionPausedUntil) {
        maybeCheckCurrentText(null, 'interval');
    }
}, 3000);

// 初始化
apiFetch('/healthz')
    .then(r => log('backend healthy'))
    .catch(e => log('backend unavailable', e));

log('content script loaded');
