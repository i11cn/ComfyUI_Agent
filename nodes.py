import json
import os
import uuid

from comfy_api.latest import ComfyExtension, io
from typing_extensions import override

from .agent_loop import audio_to_content_part, chat, chat_with_tools, frames_to_data_urls, image_to_data_url
from . import tools as llm_tools
from . import skill_manager
from .skill_manager import skill_description

FOLDER = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(FOLDER, "config.json")
SKILLS_DIR = os.path.join(FOLDER, "skills")
SYSTEM_PROMPTS_FILE = os.path.join(FOLDER, "system.json")
DEFAULT_SYSTEM_PROMPTS_FILE = os.path.join(FOLDER, "system.default.json")
MANUAL_PROMPT = "(手工输入)"

MAX_MEDIA_INPUTS = 15
# conversation ids approved for the rest of the session (对话期间都允许)
APPROVED_CONVERSATIONS = set()

# conversations kept in RAM; oldest-unused is dropped beyond this many
MAX_CONVERSATIONS = 100


def load_config():
    if not os.path.isfile(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def mask_api_key(key):
    if not key:
        return ""
    return key[:3] + "***" + key[-4:] if len(key) > 8 else "***"


def public_config():
    config = load_config()
    return {
        "default": config.get("default", ""),
        "providers": {name: {"base_url": conf.get("base_url", ""), "model": conf.get("model", ""),
                             "api_key": mask_api_key(conf.get("api_key", ""))}
                      for name, conf in config.get("providers", {}).items()},
    }


def save_config(data):
    """Validate an editor payload and write config.json; an empty api_key keeps the stored one."""
    old = load_config().get("providers", {})
    providers_in = data.get("providers")
    if not isinstance(providers_in, dict) or not providers_in:
        raise ValueError("providers 必须是非空对象")
    providers = {}
    for name, conf in providers_in.items():
        name = str(name).strip()
        if not name:
            raise ValueError("服务商名称不能为空")
        if name in providers:
            raise ValueError("服务商名称重复: " + name)
        if not isinstance(conf, dict):
            raise ValueError("服务商 '{}' 的配置必须是对象".format(name))
        entry = {}
        for field in ("base_url", "model"):
            value = conf.get(field) or ""
            if not isinstance(value, str):
                raise ValueError("服务商 '{}' 的 {} 必须是字符串".format(name, field))
            entry[field] = value.strip()
        key = conf.get("api_key") or ""
        if not isinstance(key, str):
            raise ValueError("服务商 '{}' 的 api_key 必须是字符串".format(name))
        entry["api_key"] = key or old.get(name, {}).get("api_key", "")
        providers[name] = entry
    default = str(data.get("default") or "").strip()
    if default not in providers:
        raise ValueError("默认服务商 '{}' 不在 providers 中".format(default))
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"default": default, "providers": providers}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def list_skills():
    if not os.path.isdir(SKILLS_DIR):
        return []
    return sorted(n for n in os.listdir(SKILLS_DIR)
                  if os.path.isfile(os.path.join(SKILLS_DIR, n, "SKILL.md"))
                  and skill_manager.is_enabled(n))


def _read_json_file(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except ValueError as e:
        raise ValueError("系统提示词文件 JSON 无法解析（{}）: {}".format(path, e))


def load_system_prompts():
    """Built-in system.default.json merged with the user's system.json; user entries win."""
    return {**_read_json_file(DEFAULT_SYSTEM_PROMPTS_FILE), **_read_json_file(SYSTEM_PROMPTS_FILE)}


def prompts_public():
    return {"prompts": load_system_prompts(),
            "builtin": _read_json_file(DEFAULT_SYSTEM_PROMPTS_FILE),
            "user": sorted(_read_json_file(SYSTEM_PROMPTS_FILE))}


def save_prompts(data):
    """Apply dialog edits: overrides write the user file (a built-in name becomes a user
    override), delete removes user-file entries only — built-in presets cannot be deleted."""
    overrides = data.get("overrides")
    deletions = data.get("delete", [])
    if not isinstance(overrides, dict):
        raise ValueError("overrides 必须是对象")
    if not isinstance(deletions, list):
        raise ValueError("delete 必须是数组")
    user = _read_json_file(SYSTEM_PROMPTS_FILE)
    for name in deletions:
        if not isinstance(name, str) or name not in user:
            raise ValueError("预设 '{}' 不在用户文件中，无法删除（内置预设不可删）".format(name))
        del user[name]
    seen = set()
    for name, text in overrides.items():
        name = name.strip()
        if not name:
            raise ValueError("预设名称不能为空")
        if name in seen:
            raise ValueError("预设名称重复: " + name)
        if not isinstance(text, str):
            raise ValueError("预设 '{}' 的内容必须是字符串".format(name))
        seen.add(name)
        user[name] = text
    save_system_prompts(user)


def save_system_prompts(prompts):
    tmp = SYSTEM_PROMPTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prompts, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SYSTEM_PROMPTS_FILE)


# GET /ComfyUI_Agent/prompts -> 内置+用户系统提示词的合并视图（前端选择预设时实时取用，不依赖页面加载时的快照）
try:
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.get("/ComfyUI_Agent/prompts")
    async def _system_prompts_handler(request):
        return web.json_response(prompts_public())

    @PromptServer.instance.routes.post("/ComfyUI_Agent/prompts")
    async def _post_prompts_handler(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求体不是合法 JSON"}, status=400)
        try:
            save_prompts(data)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(prompts_public())

    @PromptServer.instance.routes.get("/ComfyUI_Agent/config")
    async def _get_config_handler(request):
        return web.json_response(public_config())

    @PromptServer.instance.routes.post("/ComfyUI_Agent/config")
    async def _post_config_handler(request):
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "请求体不是合法 JSON"}, status=400)
        try:
            save_config(data)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(public_config())
except Exception:
    pass


class SystemPromptSelector(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        prompts = load_system_prompts()
        return io.Schema(
            node_id="SystemPromptSelector",
            display_name="System Prompt Selector",
            category="Agent",
            description="Pick one of the system prompts merged from the built-in system.default.json and the user's "
                        "system.json (same name: user wins), or switch to "
                        + MANUAL_PROMPT + " and write your own. The text box mirrors the selection live and its content is the output.",
            inputs=[
                io.Combo.Input("preset", options=[MANUAL_PROMPT] + sorted(prompts), default=MANUAL_PROMPT, display_name="预设",
                               tooltip="系统提示词预设，来自内置 system.default.json 与用户 system.json 的合并视图，同名以用户侧为准。选择后下方文本框自动带出全文；选 "
                                       + MANUAL_PROMPT + " 则清空文本框自由输入。新增预设后下拉框自动更新，无需刷新页面。"),
                io.String.Input("text", multiline=True, default="", display_name="文本",
                                tooltip="当前生效的系统提示词全文，也是本节点的输出内容。"),
            ],
            outputs=[io.String.Output(display_name="text")],
        )

    @classmethod
    def execute(cls, preset=MANUAL_PROMPT, text=""):
        # API-format callers may omit the text and just name a preset
        if not text.strip() and preset != MANUAL_PROMPT:
            text = load_system_prompts().get(preset, text)
        return io.NodeOutput(text)


def load_skill(name):
    # callers must pass a name taken from list_skills(); anything else is rejected
    root = os.path.realpath(os.path.join(SKILLS_DIR, name))
    if os.path.commonpath([root, os.path.realpath(SKILLS_DIR)]) != os.path.realpath(SKILLS_DIR):
        raise ValueError("未知技能: " + name)
    if not skill_manager.is_enabled(name):
        raise ValueError("技能已停用: " + name)
    with open(os.path.join(root, "SKILL.md"), "r", encoding="utf-8") as f:
        parts = [f.read()]
    ref = os.path.join(root, "references")
    if os.path.isdir(ref):
        for base, dirs, files in os.walk(ref):
            dirs.sort()
            for fname in sorted(files):
                if fname.lower().endswith((".md", ".txt")):
                    path = os.path.join(base, fname)
                    rel = os.path.relpath(path, root).replace("\\", "/")
                    with open(path, "r", encoding="utf-8") as f:
                        parts.append("=== reference: {} ===\n{}".format(rel, f.read()))
    return "\n\n".join(parts)


def skill_catalog():
    return "\n".join("- {}: {}".format(s, skill_description(s)) for s in list_skills())


LOAD_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": "加载一个已安装技能的完整说明文档（SKILL.md 与参考文件）。仅当用户请求与该技能相关时才调用，"
                       "名称必须来自系统消息中的技能清单，同一技能不要重复加载。",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "技能名称，来自技能清单"}},
            "required": ["name"],
        },
    },
}


def script_skill_boundary(skill_name):
    return ("【运行环境说明】本技能附带可执行脚本与文件，你可以使用 read/write/edit/bash 四个工具。"
            "所有操作的默认根目录是：{}（read/write/edit 的相对路径与 bash 的当前目录都基于它）。"
            "你的技能位于该目录下的 skills/{}/（含 SKILL.md、scripts/、reference/）。"
            "需要查找或执行文件时，先在上述目录内查找（例如 ls skills/{}/scripts、python skills/{}/scripts/xxx.py），"
            "不要从系统根目录（/）开始查找。禁止输出 DSML 等协议标记；完成后给出最终成果文本。"
            "部分工具执行前需要用户批准；若某次执行被用户拒绝，不要反复重试同一操作，"
            "直接说明情况或改用其它方案。").format(FOLDER, skill_name, skill_name, skill_name)


class LLMChat(io.ComfyNode):
    # conversation_id -> past [user, assistant, ...] messages, kept as plain text
    conversations = {}

    @classmethod
    def define_schema(cls):
        config = load_config()
        providers = config.get("providers", {})
        return io.Schema(
            node_id="AgentChat",
            display_name="Agent Chat",
            category="Agent",
            description="Chat with an OpenAI-compatible LLM, e.g. DeepSeek, Zhipu, Kimi or a local Ollama/LM Studio. "
                        "The reply is returned as text for prompts; with an image input a VLM sees it. "
                        "Conversations are keyed by conversation_id and kept in server RAM until restart. "
                        "The node is an output node, so it runs on every queue.",
            inputs=[
                io.Boolean.Input("new_conversation", default=False, optional=True, display_name="新对话",
                                 tooltip="Run once to start a new conversation: a fresh conversation_id is generated and filled in, and this switch resets to false afterwards."),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True, default="", optional=True, display_name="提示词",
                                tooltip="This round's instruction or question."),
                io.String.Input("system_prompt", multiline=True, dynamic_prompts=True, default="", optional=True, display_name="系统提示词",
                                tooltip="Sent once as the system message, e.g. 'You are a prompt engineer. Rewrite the user's idea as one detailed English prompt.'"),
                io.Combo.Input("provider", options=list(providers) or ["(无)"], default=config.get("default", ""), optional=True, display_name="服务商",
                               tooltip="服务供应商，选项来自 config.json 的 providers 各条目的名字；base_url、api_key、model 全部由该条目决定。修改 config.json 后刷新页面可更新列表。",
                               extra_dict={"llm_providers": {k: {"base_url": v.get("base_url", ""), "model": v.get("model", "")}
                                                             for k, v in providers.items()}}),
                io.Combo.Input("permission", options=["询问", "总是允许", "总是拒绝"], default="询问", optional=True,
                               display_name="工具权限",
                               tooltip="bash/write/edit 等改动型工具的执行策略：询问=每次弹窗确认（可选允许/对话期间都允许/拒绝）；总是允许=不再询问；总是拒绝=一律不执行。read 不受影响。"),
                io.Float.Input("temperature", default=0.7, min=0.0, max=2.0, step=0.05, optional=True, display_name="温度",
                               tooltip="Lower is more deterministic, higher is more creative."),
                io.Autogrow.Input("images", optional=True,
                                  template=io.Autogrow.TemplateNames(input=io.Image.Input("image"),
                                                                     names=["image{}".format(i) for i in range(1, 13)], min=0),
                                  tooltip="可选图片输入。image1 连接后自动出现 image2，最多 12 张。"),
                io.Autogrow.Input("videos", optional=True,
                                  template=io.Autogrow.TemplateNames(input=io.Video.Input("video"),
                                                                     names=["video{}".format(i) for i in range(1, 6)], min=0),
                                  tooltip="可选视频输入。video1 连接后自动出现 video2，最多 5 个；每个视频均匀取 4 帧作为图片发送（模型需支持视觉）。"),
                io.Autogrow.Input("audios", optional=True,
                                  template=io.Autogrow.TemplateNames(input=io.Audio.Input("audio"),
                                                                     names=["audio{}".format(i) for i in range(1, 6)], min=0),
                                  tooltip="可选音频输入。audio1 连接后自动出现 audio2，最多 5 个；以 WAV 编码发送（模型需支持音频）。"),
                io.String.Input("conversation_id", default="", optional=True,
                                tooltip="Internal conversation key, managed by the node and hidden in the UI. It is still a normal input, so API-format prompts may set it to share one conversation across nodes."),
            ],
            outputs=[io.String.Output(display_name="text")],
            hidden=[io.Hidden.unique_id],
            is_output_node=True,
        )

    @classmethod
    def execute(cls, prompt, temperature=0.7, new_conversation=False,
                system_prompt="", conversation_id="", provider=None, permission="询问",
                images=None, videos=None, audios=None, unique_id=None):
        config = load_config()
        providers = config.get("providers", {})
        if not providers:
            raise ValueError("config.json 需要 providers 多供应商配置，格式见 README.md")
        pname = provider or config.get("default", "")
        if pname not in providers:
            raise ValueError("未知供应商 '{}'，可用: {}（修改 config.json 后刷新页面可更新列表）".format(
                pname, ", ".join(sorted(providers))))
        conf = providers[pname]
        endpoint = conf.get("base_url", "")
        name = conf.get("model", "")
        api_key = os.environ.get("COMFYUI_AGENT_API_KEY") or conf.get("api_key", "")
        if not endpoint or not name:
            raise ValueError("供应商 '{}' 的 base_url / model 未配置完整".format(pname))

        if new_conversation:
            conversation_id = "c" + uuid.uuid4().hex[:12]
        elif not conversation_id:
            conversation_id = unique_id
        # pop + reinsert keeps actively used conversations out of eviction
        history = cls.conversations.pop(conversation_id, [])

        def status_cb(text):
            try:
                from server import PromptServer
                PromptServer.instance.send_sync("ComfyUI_Agent_status",
                                                {"conversation": conversation_id, "text": text})
                print("[ComfyUI_Agent:status] sent conv={} text={}".format(conversation_id, text[:50]), flush=True)
            except Exception as e:
                print("[ComfyUI_Agent:status] send FAILED: {}".format(e), flush=True)

        media = []
        for prefix, group in (("image", images), ("video", videos), ("audio", audios)):
            values = group or {}
            names = sorted((n for n in values if values[n] is not None),
                           key=lambda n: int(n[len(prefix):]) if n[len(prefix):].isdigit() else 999)
            media += [(name, values[name]) for name in names]
        if len(media) > MAX_MEDIA_INPUTS:
            raise ValueError("媒体输入共 {} 个，超过上限 {}（image 最多 12、video/audio 各最多 5）".format(
                len(media), MAX_MEDIA_INPUTS))

        messages = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        skills = list_skills()
        if skills:
            messages.append({"role": "system", "content": (
                "【已安装技能清单】当前环境有如下技能，可用 load_skill 工具按需加载其完整说明：\n"
                + skill_catalog()
                + "\n仅当用户请求与某技能相关时才加载并遵循其方法论；与技能无关的请求请勿加载，直接回答。")})
        messages += history
        content = [{"type": "text", "text": prompt}]
        for slot, value in media:
            if slot.startswith("image"):
                for frame in value:
                    content.append({"type": "image_url", "image_url": {"url": image_to_data_url(frame)}})
            elif slot.startswith("video"):
                for url in frames_to_data_urls(value.get_components().images):
                    content.append({"type": "image_url", "image_url": {"url": url}})
            else:
                content.append(audio_to_content_part(value))
        messages.append({"role": "user", "content": content})

        if not skills:
            reply = chat(endpoint, api_key, name, messages, temperature, status_cb=status_cb)
            status_cb("")
        else:
            base_len = len(messages)
            toolbox = llm_tools.ToolBox(workspace=FOLDER)
            script_skill_loaded = False

            def executor(tool_name, args):
                # load_skill 只读永远放行；read 永远放行；bash/write/edit 按权限模式走
                nonlocal script_skill_loaded
                if tool_name == "load_skill":
                    skill_name = str(args.get("name", "")).strip()
                    if skill_name not in skills:
                        return "未知技能 '{}'：请使用技能清单中列出的名称。".format(skill_name)
                    status_cb("加载技能 " + skill_name)
                    text = load_skill(skill_name)
                    if os.path.isdir(os.path.join(SKILLS_DIR, skill_name, "scripts")):
                        script_skill_loaded = True
                        return script_skill_boundary(skill_name) + "\n\n" + text
                    return text + "\n\n（本技能没有可执行脚本，请直接在正文中完成全部步骤并给出最终成果。）"

                if tool_name == "read":
                    return toolbox.execute(tool_name, args)
                if permission == "总是拒绝":
                    return "用户设置了拒绝执行工具，本次未执行。"
                if permission == "询问" and conversation_id not in APPROVED_CONVERSATIONS:
                    detail = args.get("command") or args.get("path") or tool_name
                    decision = llm_tools.request_approval(conversation_id, tool_name, str(detail))
                    if decision == "allow_conversation":
                        APPROVED_CONVERSATIONS.add(conversation_id)
                    elif decision != "allow":
                        return "用户拒绝了本次工具执行。请勿重复尝试同一操作；如可以，请改用其它方案或直接给出最终回答。"
                return toolbox.execute(tool_name, args)

            def tools_for_round(round_no):
                return [LOAD_SKILL_TOOL] + (llm_tools.ToolBox.TOOLS if script_skill_loaded else [])

            try:
                reply = chat_with_tools(endpoint, api_key, name, messages, temperature,
                                        tools=tools_for_round, tool_executor=executor,
                                        max_rounds=64, status_cb=status_cb)
            except RuntimeError as e:
                text = str(e).lower()
                if "tool" in text or "function" in text:
                    status_cb("服务商不支持工具调用，按无工具模式继续")
                    reply = chat(endpoint, api_key, name, messages[:base_len], temperature, status_cb=status_cb)
                else:
                    raise
            status_cb("")

        history = history + [{"role": "user", "content": prompt},
                             {"role": "assistant", "content": reply}]
        cls.conversations[conversation_id] = history
        if len(cls.conversations) > MAX_CONVERSATIONS:
            for old in list(cls.conversations)[:-MAX_CONVERSATIONS]:
                del cls.conversations[old]
        return io.NodeOutput(reply, ui={"llm_reply": [reply], "llm_context": [len(history)], "llm_conv": [conversation_id]})


class LLMBridgeExtension(ComfyExtension):
    @override
    async def get_node_list(self):
        return [LLMChat, SystemPromptSelector]


async def comfy_entrypoint():
    return LLMBridgeExtension()
