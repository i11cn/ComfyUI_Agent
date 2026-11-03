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
                            .then((res) => res.json())
                            .then((data) => {
                                if (presetW.value !== MANUAL) textW.value = data.prompts[presetW.value] || '';
                            })
                            .catch(() => {});
                    };
                    presetW.callback();
                }
                const btnBox = document.createElement("div");
                const btn = document.createElement("div");
                btn.textContent = "⚙ 编辑提示词";
                btn.title = "管理所有系统提示词：改写内置、编辑用户预设、新增与删除";
                btn.style.cssText = "font-size:11px;opacity:0.75;cursor:pointer;padding:3px 6px;user-select:none;width:fit-content;";
                btn.onclick = () => openPromptsDialog();
                btnBox.append(btn);
                const editW = this.addDOMWidget("ComfyUI_Agent_prompts_edit", "btn", btnBox, { serialize: false });
                editW.computeSize = () => [0, 24];
                return r;
            };
            return;
        }
        if (nodeData.name !== "AgentChat") return;
        // public provider info from the schema (never contains the api_key); kept module-level
        // so the provider dialog can refresh every AgentChat node after saving
        providerInfo = ((((nodeData.input || {}).optional || {}).provider || [null, {}])[1]).llm_providers || {};

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
            const box = document.createElement("div");
            const infoRow = document.createElement("div");
            infoRow.style.cssText = "display:flex;align-items:center;justify-content:space-between;";
            const info = document.createElement("div");
            info.style.cssText = "font-size:10px;opacity:0.75;padding:2px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;";
            const gear = document.createElement("div");
            gear.textContent = "⚙ 服务商";
            gear.title = "管理服务商（config.json）：增删改、设默认";
            gear.style.cssText = "font-size:10px;opacity:0.65;cursor:pointer;padding:2px 6px;flex:none;user-select:none;";
            gear.onclick = () => openProviderDialog();
            infoRow.append(info, gear);
            const head = document.createElement("div");
            head.className = "llm-bridge-head";
            head.style.cssText = "font-size:10px;opacity:0.6;padding:2px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
            const status = document.createElement("div");
            status.className = "llm-bridge-status";
            status.style.cssText = "font-size:10px;padding:2px 6px;color:#7aa2f7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:none;";
            const body = document.createElement("div");
            body.className = "llm-bridge-body";
            body.style.cssText = "font-size:12px;font-family:monospace;line-height:1.35;padding:4px 6px;white-space:pre-wrap;word-break:break-word;overflow-y:auto;max-height:110px;background:rgba(0,0,0,0.25);border-radius:4px;margin:0 4px 4px;";
            box.append(infoRow, head, status, body);
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
                    renderInfo(this.llmBridge, Object.assign({ name }, providerInfo[name] || {}));
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

// ---- 服务商管理：读写 /ComfyUI_Agent/config，保存后原位刷新各 AgentChat 节点的服务商下拉框 ----
let providerInfo = {};

function applyProviderConfig(cfg) {
    // providerInfo is the schema's llm_providers object: updating it in place also
    // refreshes nodeData, so newly created nodes pick up the change without a reload
    for (const k of Object.keys(providerInfo)) delete providerInfo[k];
    Object.assign(providerInfo, cfg.providers);
    (app.graph._nodes || []).forEach((n) => {
        if (n.type !== "AgentChat") return;
        const w = (n.widgets || []).find(w => w.name === "provider");
        if (w) {
            if (!w.options) w.options = {};
            w.options.values = Object.keys(providerInfo);
            if (!providerInfo[w.value]) w.value = cfg.default;
        }
        const name = String(w ? w.value : cfg.default);
        if (n.llmBridge) renderInfo(n.llmBridge, Object.assign({ name }, providerInfo[name] || {}));
    });
}

function providerRow(name, p, isDefault) {
    const row = document.createElement('div');
    row.style.cssText = 'display:grid;grid-template-columns:58px 110px 1fr 130px 160px 26px;gap:6px;margin-bottom:6px;align-items:center;';
    const defBox = document.createElement('span');
    defBox.style.cssText = 'display:flex;align-items:center;gap:3px;font-size:11px;opacity:0.75;';
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'agent_default_provider';
    radio.checked = isDefault;
    radio.title = '设为默认服务商';
    defBox.append(radio, document.createTextNode('默认'));
    const fields = {};
    const specs = [
        ['name', name, '名称'],
        ['base', p.base_url || '', 'base_url'],
        ['model', p.model || '', 'model'],
        ['key', '', p.api_key ? '留空保持现有密钥（' + p.api_key + '）' : '密钥，可留空'],
    ];
    const del = document.createElement('div');
    del.textContent = '✕';
    del.title = '删除该服务商';
    del.style.cssText = 'cursor:pointer;opacity:0.6;text-align:center;font-size:12px;';
    row.append(defBox);
    for (const [key, value, ph] of specs) {
        const input = document.createElement('input');
        input.value = value;
        input.placeholder = ph;
        input.style.cssText = 'background:#141418;color:#e8e8ea;border:1px solid #444;border-radius:4px;padding:4px 6px;font-size:11px;min-width:0;';
        fields[key] = input;
        row.append(input);
    }
    row.append(del);
    return { row, radio, fields, del };
}

function openProviderDialog() {
    fetch('/ComfyUI_Agent/config')
        .then((r) => r.json())
        .then((cfg) => {
            const backdrop = document.createElement('div');
            backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:99999;display:flex;align-items:center;justify-content:center;';
            const panel = document.createElement('div');
            panel.style.cssText = 'background:#1e1e24;color:#e8e8ea;border:1px solid #444;border-radius:10px;padding:16px 18px;width:780px;max-width:94vw;max-height:86vh;display:flex;flex-direction:column;font-family:sans-serif;box-shadow:0 8px 40px rgba(0,0,0,0.6);';
            const titleRow = document.createElement('div');
            titleRow.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;';
            const title = document.createElement('div');
            title.textContent = '服务商管理（config.json）';
            title.style.cssText = 'font-size:15px;font-weight:600;';
            const close = document.createElement('div');
            close.textContent = '✕';
            close.style.cssText = 'cursor:pointer;opacity:0.7;font-size:14px;padding:2px 6px;';
            close.onclick = () => backdrop.remove();
            titleRow.append(title, close);
            const hint = document.createElement('div');
            hint.style.cssText = 'font-size:11px;opacity:0.65;margin-bottom:8px;';
            hint.textContent = '密钥不回显，留空表示保持现有密钥（新条目则为无密钥）；已保存的密钥无法单独清除，如需移除密钥，请删除整条记录后重新创建。「默认」勾选保存后生效。';
            const list = document.createElement('div');
            list.style.cssText = 'overflow-y:auto;min-height:0;';
            const rows = [];
            const addRow = (name, p, isDefault) => {
                const r = providerRow(name, p, isDefault);
                r.del.onclick = () => {
                    r.row.remove();
                    rows.splice(rows.indexOf(r), 1);
                    if (!rows.some((x) => x.radio.checked) && rows.length) rows[0].radio.checked = true;
                };
                rows.push(r);
                list.append(r.row);
            };
            const names = Object.keys(cfg.providers || {});
            names.forEach((name) => addRow(name, cfg.providers[name], name === cfg.default));
            if (!rows.length) addRow('', {}, true);
            const header = document.createElement('div');
            header.style.cssText = 'display:grid;grid-template-columns:58px 110px 1fr 130px 160px 26px;gap:6px;font-size:10px;opacity:0.6;margin-bottom:4px;';
            for (const label of ['默认', '名称', 'base_url', 'model', 'api_key', '']) {
                const span = document.createElement('span');
                span.textContent = label;
                header.append(span);
            }
            const addBtn = document.createElement('div');
            addBtn.textContent = '+ 添加服务商';
            addBtn.style.cssText = 'font-size:11px;opacity:0.8;cursor:pointer;padding:4px 2px;width:fit-content;';
            addBtn.onclick = () => {
                addRow('', {}, !rows.some((x) => x.radio.checked));
                // jump to the new row at the bottom and put the caret into its name field
                const newRow = list.lastElementChild;
                if (newRow) {
                    newRow.scrollIntoView({ behavior: 'smooth', block: 'end' });
                    const nameInput = newRow.querySelector("input:not([type='radio'])");
                    if (nameInput) nameInput.focus();
                }
            };
            const errLine = document.createElement('div');
            errLine.style.cssText = 'font-size:11px;color:#f87171;min-height:15px;margin-top:6px;';
            const footer = document.createElement('div');
            footer.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;margin-top:4px;';
            const mk = (label, primary) => {
                const b = document.createElement('button');
                b.textContent = label;
                b.style.cssText = 'padding:6px 14px;border-radius:6px;border:1px solid #555;cursor:pointer;font-size:12px;' +
                    (primary ? 'background:#3b82f6;color:#fff;border-color:#3b82f6;' : 'background:#2a2a30;color:#ddd;');
                return b;
            };
            const cancel = mk('取消', false);
            cancel.onclick = () => backdrop.remove();
            const save = mk('保存', true);
            save.onclick = async () => {
                errLine.textContent = '';
                const payload = {};
                for (const r of rows) {
                    const name = r.fields.name.value.trim();
                    if (!name) { errLine.textContent = '服务商名称不能为空'; return; }
                    if (name in payload) { errLine.textContent = '服务商名称重复: ' + name; return; }
                    payload[name] = { base_url: r.fields.base.value.trim(), model: r.fields.model.value.trim(), api_key: r.fields.key.value };
                }
                const defRow = rows.find((r) => r.radio.checked) || rows[0];
                save.disabled = true;
                try {
                    const resp = await fetch('/ComfyUI_Agent/config', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ default: defRow.fields.name.value.trim(), providers: payload }),
                    });
                    const data = await resp.json();
                    if (!resp.ok) {
                        errLine.textContent = data.error || ('保存失败 HTTP ' + resp.status);
                        return;
                    }
                    applyProviderConfig(data);
                    backdrop.remove();
                } catch (e) {
                    errLine.textContent = '保存失败: ' + e;
                } finally {
                    save.disabled = false;
                }
            };
            footer.append(cancel, save);
            panel.append(titleRow, hint, header, list, addBtn, errLine, footer);
            backdrop.append(panel);
            document.body.append(backdrop);
        })
        .catch((e) => alert('读取配置失败: ' + e));
}

// ---- 系统提示词管理：读写 /ComfyUI_Agent/prompts，保存后原位刷新下拉框与文本镜像 ----
function applyPromptsRefresh(data) {
    const names = Object.keys(data.prompts || {}).sort();
    // mutate the existing arrays in place: replacing them with new arrays would go
    // unnoticed by layers holding the old reference (mounted combo menu components)
    const sync = (arr, first) => {
        const next = [first].concat(names);
        if (Array.isArray(arr)) {
            arr.length = 0;
            for (const v of next) arr.push(v);
            return arr;
        }
        return next;
    };
    const cls = window.LiteGraph.registered_node_types["SystemPromptSelector"];
    const input = ((cls || {}).nodeData || {}).input || {};
    const opt = (input.optional || {}).preset || (input.required || {}).preset;
    if (opt && opt[1]) opt[1].options = sync(opt[1].options, "(手工输入)");
    (app.graph._nodes || []).forEach((n) => {
        if (n.type !== "SystemPromptSelector") return;
        const w = (n.widgets || []).find((w) => w.name === "preset");
        if (!w) return;
        if (!w.options) w.options = {};
        w.options.values = sync(w.options.values, "(手工输入)");
        if (w.value !== "(手工输入)" && !names.includes(w.value)) w.value = "(手工输入)";
        if (w.callback) w.callback();
    });
}

function openPromptsDialog() {
    fetch('/ComfyUI_Agent/prompts')
        .then((r) => r.json())
        .then((cfg) => {
            const builtinContent = cfg.builtin || {};
            const userNames = new Set(cfg.user || []);
            const backdrop = document.createElement('div');
            backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:99999;display:flex;align-items:center;justify-content:center;';
            const panel = document.createElement('div');
            panel.style.cssText = 'background:#1e1e24;color:#e8e8ea;border:1px solid #444;border-radius:10px;padding:16px 18px;width:780px;max-width:94vw;height:86vh;display:flex;flex-direction:column;font-family:sans-serif;box-shadow:0 8px 40px rgba(0,0,0,0.6);';
            const titleRow = document.createElement('div');
            titleRow.style.cssText = 'display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;';
            const title = document.createElement('div');
            title.textContent = '系统提示词管理';
            title.style.cssText = 'font-size:15px;font-weight:600;';
            const close = document.createElement('div');
            close.textContent = '✕';
            close.style.cssText = 'cursor:pointer;opacity:0.7;font-size:14px;padding:2px 6px;';
            close.onclick = () => backdrop.remove();
            titleRow.append(title, close);
            const hint = document.createElement('div');
            hint.style.cssText = 'font-size:11px;opacity:0.65;margin-bottom:8px;line-height:1.5;';
            hint.textContent = '所有提示词都可改写：内置预设（黄名）修改后保存即写入用户覆盖，与系统自带内容一致时不产生覆盖。✕：内置行=恢复系统自带内容（该行保留），用户行=删除该预设。下拉框保存后自动更新。';
            const list = document.createElement('div');
            list.style.cssText = 'overflow-y:auto;min-height:0;flex:1;';
            const rows = [];

            const addRow = (name, text) => {
                const isBuiltin = Object.prototype.hasOwnProperty.call(builtinContent, name);
                const row = document.createElement('div');
                row.style.cssText = 'display:grid;grid-template-columns:150px 1fr 26px;gap:6px;margin-bottom:8px;align-items:start;';
                let nameEl;
                if (isBuiltin) {
                    nameEl = document.createElement('div');
                    nameEl.textContent = name;
                    nameEl.title = '内置预设：内容可改写（保存后写入用户覆盖），不可删除';
                    nameEl.style.cssText = 'font-size:12px;font-weight:600;padding:6px 4px 0;color:#e5c07b;word-break:break-all;';
                } else {
                    nameEl = document.createElement('input');
                    nameEl.value = name;
                    nameEl.placeholder = '预设名称';
                    nameEl.style.cssText = 'background:#141418;color:#e8e8ea;border:1px solid #444;border-radius:4px;padding:4px 6px;font-size:12px;';
                }
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.rows = 5;
                ta.style.cssText = 'background:#141418;color:#e8e8ea;border:1px solid #444;border-radius:4px;padding:4px 6px;font-size:11px;line-height:1.45;resize:vertical;min-height:60px;';
                const del = document.createElement('div');
                del.textContent = '✕';
                del.style.cssText = 'cursor:pointer;opacity:0.6;text-align:center;font-size:12px;padding-top:6px;';
                const syncDel = () => {
                    // 内置行只在内容偏离内置原文时显示 ✕（=恢复内置）；用户行始终可删
                    const keepBuiltin = isBuiltin && ta.value === builtinContent[name];
                    del.style.visibility = keepBuiltin ? 'hidden' : 'visible';
                    del.title = isBuiltin ? '恢复为系统自带内容' : '删除该预设';
                };
                syncDel();
                ta.addEventListener('input', syncDel);
                del.onclick = () => {
                    if (isBuiltin) {
                        ta.value = builtinContent[name];
                        syncDel();
                    } else {
                        row.remove();
                        rows.splice(rows.indexOf(entry), 1);
                    }
                };
                const entry = { name, isBuiltin, nameEl, ta };
                row.append(nameEl, ta, del);
                rows.push(entry);
                list.append(row);
            };
            for (const name of Object.keys(cfg.prompts || {})) addRow(name, cfg.prompts[name]);
            const addBtn = document.createElement('div');
            addBtn.textContent = '+ 添加预设';
            addBtn.style.cssText = 'font-size:11px;opacity:0.8;cursor:pointer;padding:6px 2px;width:fit-content;';
            addBtn.onclick = () => {
                addRow('', '');
                // jump to the new row at the bottom and put the caret into its name field
                const newRow = list.lastElementChild;
                if (newRow) {
                    newRow.scrollIntoView({ behavior: 'smooth', block: 'end' });
                    const nameInput = newRow.querySelector("input[placeholder='预设名称']");
                    if (nameInput) nameInput.focus();
                }
            };
            const errLine = document.createElement('div');
            errLine.style.cssText = 'font-size:11px;color:#f87171;min-height:15px;margin-top:6px;';
            const footer = document.createElement('div');
            footer.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;margin-top:4px;';
            const mk = (label, primary) => {
                const b = document.createElement('button');
                b.textContent = label;
                b.style.cssText = 'padding:6px 14px;border-radius:6px;border:1px solid #555;cursor:pointer;font-size:12px;' +
                    (primary ? 'background:#3b82f6;color:#fff;border-color:#3b82f6;' : 'background:#2a2a30;color:#ddd;');
                return b;
            };
            const cancel = mk('取消', false);
            cancel.onclick = () => backdrop.remove();
            const save = mk('保存', true);
            save.onclick = async () => {
                errLine.textContent = '';
                const overrides = {};
                const rowNames = new Set();
                for (const r of rows) {
                    const name = r.isBuiltin ? r.name : r.nameEl.value.trim();
                    if (!name) { errLine.textContent = '预设名称不能为空'; return; }
                    if (rowNames.has(name)) { errLine.textContent = '预设名称重复: ' + name; return; }
                    rowNames.add(name);
                    if (builtinContent[name] !== undefined && r.ta.value === builtinContent[name]) continue;
                    overrides[name] = r.ta.value;
                }
                // 用户文件里存在、但不再以覆盖形式出现的条目 = 已被恢复内置或删除
                const deletions = [...userNames].filter((n) => !(n in overrides));
                save.disabled = true;
                try {
                    const resp = await fetch('/ComfyUI_Agent/prompts', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ overrides, delete: deletions }),
                    });
                    const data = await resp.json();
                    if (!resp.ok) {
                        errLine.textContent = data.error || ('保存失败 HTTP ' + resp.status);
                        return;
                    }
                    applyPromptsRefresh(data);
                    backdrop.remove();
                } catch (e) {
                    errLine.textContent = '保存失败: ' + e;
                } finally {
                    save.disabled = false;
                }
            };
            footer.append(cancel, save);
            panel.append(titleRow, hint, list, addBtn, errLine, footer);
            backdrop.append(panel);
            document.body.append(backdrop);
        })
        .catch((e) => alert('读取系统提示词失败: ' + e));
}
