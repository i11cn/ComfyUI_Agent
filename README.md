# ComfyUI_Agent

「智能体对话」（`AgentChat`）节点：通过任何 OpenAI 兼容 API（DeepSeek、智谱、Kimi、OpenAI、本地 Ollama / LM Studio / vLLM 等）与大模型对话，回复以 `STRING` 输出，可直接接入 `CLIP Text Encode` 等文本输入。带技能（Skill）系统、Agent 工具循环与权限审批。

## 配置

`config.json` 的 `providers` 配置任意数量的服务商；节点界面用「服务商」下拉框选择，base_url / api_key / model 全部由所选条目决定，界面不再手工输入：

```json
{
  "default": "deepseek",
  "providers": {
    "deepseek": {
      "base_url": "https://api.deepseek.com/v1",
      "api_key": "sk-...",
      "model": "deepseek-flash"
    },
    "zhipu": {
      "base_url": "https://open.bigmodel.cn/api/paas/v4",
      "api_key": "xxxx",
      "model": "glm-4.6"
    }
  }
}
```

- 修改 config.json 后刷新页面即可更新下拉框。
- 环境变量 `COMFYUI_AGENT_API_KEY` 覆盖所有已配置的密钥；密钥不是节点输入，不会存进工作流。

## 节点输入（自上而下）

- **新对话**：开关。打开后运行，提交瞬间即生成新会话 id 并自动复位为 false（同种子控件的策略），不会因开关复位产生多余的重复调用。
- **提示词**：本轮的指令或问题。
- **系统提示词**：可选，通常接「系统提示词选择器」节点。
- **技能**：可选，来自 `skills/<name>/`——`SKILL.md` 与 `reference/*.md|.txt` 注入系统消息；技能文件夹含 `scripts/` 时自动进入 Agent 工具模式（见下）。刷新页面更新列表。
- **服务商**：下拉框，见上。
- **工具权限**：bash / write / edit 的执行策略——**询问**（默认，执行前弹窗：允许 / 对话期间都允许 / 拒绝；超时 300 秒视为拒绝）、**总是允许**、**总是拒绝**。`read` 不受影响。
- **温度**：0–2。
- **images / videos / audios**：动态输入组（连上 image1 后自动出现 image2，上限 12 / 5 / 5、总数 15）。视频每段均匀取 4 帧按图片发送；音频编码为 WAV（`input_audio`）；图片长边超过 1568px 自动等比缩小。目标模型需支持对应模态；媒体只在到达的那次运行中发送，对话记忆仅保留文本。
- **conversation_id**：隐藏的内部会话键，由节点自动铸造并管理；API 格式调用可显式设置，让多个节点共享同一对话。

## Agent 工具循环

选中含 `scripts/` 的技能后，模型可调用 `read` / `write` / `edit` / `bash` 四个工具，工作区为插件根目录（系统消息会告知该路径与技能位置，避免全盘查找）：bash 在 Windows 上自动定位 Git Bash，缺失时下载 MinGit；bash 中 `python` 即 ComfyUI 的 Python；Node.js 脚本经 fnm 运行，fnm 缺失时自动安装到 `.runtime/fnm`。最多 64 轮；脚本报错回传给模型自我修正；仅最终问答进入对话记忆。执行过程中节点状态行实时显示当前轮次与工具命令。

对话记忆保存在服务器内存（重启清零，最多 100 个会话）。节点为输出节点，可独立运行纯聊天；执行遵循 ComfyUI 的输入缓存：输入未变化时复用缓存的回复而不调用 API，改动任意输入即触发新调用。

## 系统提示词选择器

第二个节点（`SystemPromptSelector`）：下拉框列出 `system.json`（`{名称: 内容}`）的各条目，选中后下方文本框实时带出全文（内容从服务器实时读取，新增条目刷新页面更新下拉框即可）；「(手工输入)」清空文本框自由编辑。无输入，一个 `STRING` 输出，接「智能体对话」的「系统提示词」。

## 界面语言

节点标题与描述走 ComfyUI 官方 i18n：`locales/zh/nodeDefs.json`（服务端 `/i18n` 聚合）。输入标签在 schema 中直接使用中文 `display_name`。

## 测试

`tests/mock_server.py` 是无依赖的 OpenAI 模拟服务器：

```
python tests/mock_server.py   # http://127.0.0.1:8189/v1
```

config.json 内置 `mock` 服务商指向该地址，节点上选「服务商 = mock」即可无密钥跑通全流程。
