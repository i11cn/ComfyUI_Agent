import { app } from "/scripts/app.js";

const UI_VERSION = Number((import.meta.url.match(/\/v(\d+)\//) || [])[1]) || 0;

// ---- 权限审批弹窗：服务端在工具执行前推送确认请求，三选一后回传 ----
const confirmQueue = [];
let confirmOpen = false;

function answerConfirm(id, decision) {
    fetch('/ComfyUI_Agent/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, decision })
    }).catch((e) => console.error('[ComfyUI_Agent] approval POST failed:', e));
}

function showNextConfirm() {
    if (confirmOpen || !confirmQueue.length) return;
    confirmOpen = true;
    const d = confirmQueue.shift();

    const backdrop = document.createElement('div');
    backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:99999;display:flex;align-items:center;justify-content:center;';
    const panel = document.createElement('div');
    panel.style.cssText = 'background:#1e1e24;color:#e8e8ea;border:1px solid #444;border-radius:10px;padding:18px 20px;width:430px;max-width:92vw;font-family:sans-serif;box-shadow:0 8px 40px rgba(0,0,0,0.6);';
    const title = document.createElement('div');
    title.textContent = '智能体请求执行工具';
    title.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:10px;';
    const body = document.createElement('div');
    body.style.cssText = 'font-size:12px;line-height:1.5;margin-bottom:14px;word-break:break-all;';
    body.innerHTML = '<b style="color:#e5c07b">' + d.tool + '</b>' + (d.detail ? '<div style="margin-top:6px;background:rgba(0,0,0,0.3);padding:6px 8px;border-radius:4px;font-family:monospace;max-height:90px;overflow-y:auto;">' + d.detail + '</div>' : '') +
        '<div style="margin-top:8px;opacity:0.7">会话 ' + (d.conversation || '') + '</div>';
    const buttons = document.createElement('div');
    buttons.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';
    const mk = (label, decision, primary) => {
        const b = document.createElement('button');
        b.textContent = label;
        b.style.cssText = 'padding:6px 14px;border-radius:6px;border:1px solid #555;cursor:pointer;font-size:12px;' +
            (primary ? 'background:#3b82f6;color:#fff;border-color:#3b82f6;' : 'background:#2a2a30;color:#ddd;');
        b.onclick = () => {
            answerConfirm(d.id, decision);
            backdrop.remove();
            confirmOpen = false;
            showNextConfirm();
        };
        return b;
    };
    buttons.append(mk('拒绝', 'deny', false), mk('对话期间都允许', 'allow_conversation', false), mk('允许', 'allow', true));
    panel.append(title, body, buttons);
    backdrop.append(panel);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) mk('拒绝', 'deny', false).onclick(); });
    document.body.append(backdrop);
}

app.api.addEventListener('ComfyUI_Agent_confirm', (e) => {
    if (e.detail) {
        confirmQueue.push(e.detail);
        showNextConfirm();
    }
});

// ui payloads arrive as lists, occasionally nested; unwrap to the primitive
function firstValue(v) {
    return Array.isArray(v) ? firstValue(v[0]) : v;
}

function render(display, data) {
    if (!display || !data) return;
    display.head.textContent = "上下文 " + data.context + " 条 · 会话 " + (data.conversation_id || "");
    display.body.textContent = data.reply || "";
}

function renderHead(display, context, conversationId) {
    if (!display) return;
    display.head.textContent = "上下文 " + (context ?? 0) + " 条 · 会话 " + (conversationId || "未开始");
}

function renderStatus(display, text) {
    if (!display) return;
    display.status.textContent = text || "";
    display.status.style.display = text ? "block" : "none";
}

function renderInfo(display, provider) {
    if (!display || !provider) return;
    display.info.textContent = provider.name + " → " + (provider.base_url || "?") + " · " + (provider.model || "?") + " · 界面 v" + UI_VERSION;
}

app.registerExtension({
    name: "ComfyUI_Agent.chat",
    beforeRegisterNodeDef(nodeType, nodeData) {        if (nodeData.name === "SystemPromptSelector") {
            const MANUAL = "(手工输入)";
            const onSelectorCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onSelectorCreated ? onSelectorCreated.apply(this, arguments) : undefined;
                const presetW = this.widgets.find(w => w.name === "preset");
                const textW = this.widgets.find(w => w.name === "text");
                if (presetW && textW) {
                    // the text box mirrors the selection live; "(手工输入)" leaves it blank for free editing
                    presetW.callback = () => {
                        if (presetW.value === MANUAL) {
                            textW.value = "";
                            return;
                        }
                        // 拉取 system.json 当前内容，避免依赖页面加载时的定义快照
                        fetch('/ComfyUI_Agent/prompts')
                            .then((r) => r.json())
                            .then((prompts) => {
                                if (presetW.value !== MANUAL) textW.value = prompts[presetW.value] || '';
                            })
                            .catch(() => {});
                    };
                    presetW.callback();
                }
                return r;
            };
            return;
        }
        if (nodeData.name !== "AgentChat") return;
        // public provider info from the schema (never contains the api_key)
        const providers = ((((nodeData.input || {}).optional || {}).provider || [null, {}])[1]).llm_providers || {};

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            const box = document.createElement("div");
            const info = document.createElement("div");
            info.style.cssText = "font-size:10px;opacity:0.75;padding:2px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
            const head = document.createElement("div");
            head.className = "llm-bridge-head";
            head.style.cssText = "font-size:10px;opacity:0.6;padding:2px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
            const status = document.createElement("div");
            status.className = "llm-bridge-status";
            status.style.cssText = "font-size:10px;padding:2px 6px;color:#7aa2f7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:none;";
            const body = document.createElement("div");
            body.className = "llm-bridge-body";
            body.style.cssText = "font-size:12px;font-family:monospace;line-height:1.35;padding:4px 6px;white-space:pre-wrap;word-break:break-word;overflow-y:auto;max-height:110px;background:rgba(0,0,0,0.25);border-radius:4px;margin:0 4px 4px;";
            box.append(info, head, status, body);
            const widget = this.addDOMWidget("ComfyUI_Agent_display", "display", box, { serialize: false });
            widget.computeSize = () => [0, 170];
            this.llmBridge = { info, head, status, body };
            renderHead(this.llmBridge, 0, "");
            // conversation_id is internal state: a stable per-node random key minted at
            // creation, serialized with the workflow (the server cannot supply unique_id,
            // so this widget is the single source of the conversation key)
            const conv = this.widgets.find(w => w.name === "conversation_id");
            if (conv) {
                if (!conv.value) conv.value = "c" + Math.random().toString(16).slice(2, 14);
                conv.computeSize = () => [0, -4];
                conv.draw = () => {};
            }
            // seed-widget strategy: flip new_conversation AT SERIALIZE TIME so the queued
            // inputs already carry false + a fresh conversation_id, and the node's inputs
            // stay identical between runs (no wasted cache-miss re-execution)
            const fresh = this.widgets.find(w => w.name === "new_conversation");
            if (conv && fresh) {
                fresh.serializeValue = function (workflow, nodeIndex) {
                    if (fresh.value === true) {
                        conv.value = "c" + Math.random().toString(16).slice(2, 14);
                        fresh.value = false;
                    }
                    return fresh.value;
                };
            }
            // the provider selection decides endpoint and model; show them live
            const providerW = this.widgets.find(w => w.name === "provider");
            if (providerW) {
                const update = () => {
                    const name = String(providerW.value);
                    renderInfo(this.llmBridge, Object.assign({ name }, providers[name] || {}));
                };
                providerW.callback = update;
                update();
            }
            render(this.llmBridge, this.properties.ComfyUI_Agent_last);
            return r;
        };

        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            const r = onExecuted ? onExecuted.apply(this, arguments) : undefined;
            if (!message || !message.llm_reply) return r;
            const data = {
                reply: String(firstValue(message.llm_reply)),
                context: firstValue(message.llm_context),
                conversation_id: String(firstValue(message.llm_conv) || ""),
            };
            this.properties.ComfyUI_Agent_last = data;
            render(this.llmBridge, data);
            renderStatus(this.llmBridge, "");
            // one-shot switch: this run opened a new conversation, reset it and adopt the new id
            const conv = (this.widgets || []).find(w => w.name === "conversation_id");
            const fresh = (this.widgets || []).find(w => w.name === "new_conversation");
            if (conv && data.conversation_id) conv.value = data.conversation_id;
            if (fresh) fresh.value = false;
            return r;
        };
    },
});

// live execution status pushed by the server during node execution; routed by conversation_id
app.api.addEventListener("ComfyUI_Agent_status", (e) => {
    const d = e.detail || {};
    const node = (app.graph._nodes || []).find(n => {
        const w = (n.widgets || []).find(w => w.name === "conversation_id");
        return w && String(w.value) === String(d.conversation);
    });
    if (node && node.llmBridge) renderStatus(node.llmBridge, String(d.text || ""));
});
