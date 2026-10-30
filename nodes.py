import json
import os
import uuid

from comfy_api.latest import ComfyExtension, io
from typing_extensions import override

from .llm_client import audio_to_content_part, chat, chat_with_tools, frames_to_data_urls, image_to_data_url
from . import tools as llm_tools

FOLDER = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(FOLDER, "config.json")
SKILLS_DIR = os.path.join(FOLDER, "skills")
SYSTEM_PROMPTS_FILE = os.path.join(FOLDER, "system.json")
NO_SKILL = "(无)"
MANUAL_PROMPT = "(手工输入)"

MAX_MEDIA_INPUTS = 15
# conversation ids approved for the rest of the session (对话期间都允许)
APPROVED_CONVERSATIONS = set()

# conversations kept in RAM; oldest-unused is dropped beyond this many
MAX_CONVERSATIONS = 100

SKILL_BOUNDARY = (
    "【运行环境说明】你运行在一个纯聊天环境中：没有工具、文件系统和脚本执行能力。"
    "禁止输出任何工具调用或协议标记（例如 DSML、bash 命令、代码执行块）。"
    "说明书中提到的脚本、工具或文件操作步骤一律跳过，所需的计算与查找直接在正文中完成，"
    "并按照说明书的方法论直接产出最终成果文本。"
    "You run in a plain chat environment with no tools, filesystem, or script execution. "
    "Never emit tool calls or protocol markers (DSML, bash, code-exec blocks); skip any "
    "script/tool steps and produce the final deliverable text directly."
)


def load_config():
    if not os.path.isfile(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def list_skills():
    if not os.path.isdir(SKILLS_DIR):
        return []
    return sorted(n for n in os.listdir(SKILLS_DIR)
                  if os.path.isfile(os.path.join(SKILLS_DIR, n, "SKILL.md")))


def load_system_prompts():
    if not os.path.isfile(SYSTEM_PROMPTS_FILE):
        return {}
    with open(SYSTEM_PROMPTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# GET /ComfyUI_Agent/prompts -> system.json 当前内容（前端选择预设时实时取用，不依赖页面加载时的快照）
try:
    from server import PromptServer
    from aiohttp import web

    @PromptServer.instance.routes.get("/ComfyUI_Agent/prompts")
    async def _system_prompts_handler(request):
        return web.json_response(load_system_prompts())
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
            description="Pick one of the system prompts defined in custom_nodes/ComfyUI_Agent/system.json, or switch to "
                        + MANUAL_PROMPT + " and write your own. The text box mirrors the selection live and its content is the output.",
            inputs=[
                io.Combo.Input("preset", options=[MANUAL_PROMPT] + sorted(prompts), default=MANUAL_PROMPT, display_name="预设",
                               tooltip="系统提示词预设，来自 system.json 的各个条目名称。选择后下方文本框自动带出全文；选 "
                                       + MANUAL_PROMPT + " 则清空文本框自由输入。新增预设后刷新页面可更新列表。"),
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
    with open(os.path.join(root, "SKILL.md"), "r", encoding="utf-8") as f:
        parts = [f.read()]
    ref = os.path.join(root, "reference")
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
                io.Combo.Input("skill", options=[NO_SKILL] + list_skills(), default=NO_SKILL, optional=True, display_name="技能",
                               tooltip="Load a skill package from custom_nodes/ComfyUI_Agent/skills/ as guidance context: SKILL.md plus reference/*.md|.txt are injected into the system message. Scripts are never executed; a no-tools notice is added so the model produces the deliverable inline. New folders appear after a page refresh."),
                io.Combo.Input("provider", options=list(providers) or [NO_SKILL], default=config.get("default", ""), optional=True, display_name="服务商",
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
                system_prompt="", conversation_id="", skill=NO_SKILL, provider=None, permission="询问",
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

        skill_text = ""
        agent = None
        if skill and skill != NO_SKILL:
            if skill not in list_skills():
                raise ValueError("未知技能 '{}': custom_nodes/ComfyUI_Agent/skills/ 下没有这个技能文件夹，刷新页面可更新列表".format(skill))
            skill_text = load_skill(skill)
            skill_root = os.path.join(SKILLS_DIR, skill)
            if os.path.isdir(os.path.join(skill_root, "scripts")):
                agent = llm_tools.ToolBox(workspace=FOLDER)

        media = []
        for prefix, group in (("image", images), ("video", videos), ("audio", audios)):
            values = group or {}
            names = sorted((n for n in values if values[n] is not None),
                           key=lambda n: int(n[len(prefix):]) if n[len(prefix):].isdigit() else 999)
            media += [(name, values[name]) for name in names]
        if len(media) > MAX_MEDIA_INPUTS:
            raise ValueError("媒体输入共 {} 个，超过上限 {}（image 最多 12、video/audio 各最多 5）".format(
                len(media), MAX_MEDIA_INPUTS))

        def status_cb(text):
            try:
                from server import PromptServer
                PromptServer.instance.send_sync("ComfyUI_Agent_status",
                                                {"conversation": conversation_id, "text": text})
                print("[ComfyUI_Agent:status] sent conv={} text={}".format(conversation_id, text[:50]), flush=True)
            except Exception as e:
                print("[ComfyUI_Agent:status] send FAILED: {}".format(e), flush=True)

        messages = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        if skill_text:
            if agent is not None:
                boundary = ("【运行环境说明】本技能附带可执行脚本与文件，你可以使用 read/write/edit/bash 四个工具。"
                            "所有操作的默认根目录是：{}（read/write/edit 的相对路径与 bash 的当前目录都基于它）。"
                            "你的技能位于该目录下的 skills/{}/（含 SKILL.md、scripts/、reference/）。"
                            "需要查找或执行文件时，先在上述目录内查找（例如 ls skills/{}/scripts、python skills/{}/scripts/xxx.py），"
                            "不要从系统根目录（/）开始查找。禁止输出 DSML 等协议标记；完成后给出最终成果文本。"
                            "部分工具执行前需要用户批准；若某次执行被用户拒绝，不要反复重试同一操作，"
                            "直接说明情况或改用其它方案。").format(
                                FOLDER, skill, skill, skill)
            else:
                boundary = SKILL_BOUNDARY
            messages.append({"role": "system", "content": boundary + "\n\n" + skill_text})
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

        def gated_executor(tool_name, args):
            # read 永远放行；bash/write/edit 按权限模式走
            if tool_name == "read":
                return agent.execute(tool_name, args)
            if permission == "总是拒绝":
                return "用户设置了拒绝执行工具，本次未执行。"
            if permission == "询问" and conversation_id not in APPROVED_CONVERSATIONS:
                detail = args.get("command") or args.get("path") or tool_name
                decision = llm_tools.request_approval(conversation_id, tool_name, str(detail))
                if decision == "allow_conversation":
                    APPROVED_CONVERSATIONS.add(conversation_id)
                elif decision != "allow":
                    return "用户拒绝了本次工具执行。请勿重复尝试同一操作；如可以，请改用其它方案或直接给出最终回答。"
            return agent.execute(tool_name, args)

        reply = chat(endpoint, api_key, name, messages, temperature, status_cb=status_cb) if agent is None else \
            chat_with_tools(endpoint, api_key, name, messages, temperature,
                            tools=llm_tools.ToolBox.TOOLS, tool_executor=gated_executor,
                            max_rounds=64, status_cb=status_cb)
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
