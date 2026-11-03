"""Tool execution for the Agent Loop: read/write/edit files and run bash commands,
plus the Python (ComfyUI venv) and Node.js (fnm-managed) script runners.

Everything the model may touch lives under the ComfyUI_Agent folder;
bash runs with that folder as cwd. Mutating tools go through the approval
dialog (request_approval) unless the conversation or mode allows them."""
import base64
import io
import json
import os
import platform
import shutil
import subprocess
import threading
import urllib.request
import uuid
import zipfile

RUNTIME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".runtime")
FNM_DIR = os.path.join(RUNTIME_DIR, "fnm")
NODE_VERSION = "22"
FNM_PLATFORM = {"Windows": "fnm-windows.zip", "Linux": "fnm-linux.zip",
                "Darwin": "fnm-macos.zip"}[platform.system()]
FNM_URL = "https://github.com/Schniz/fnm/releases/latest/download/" + FNM_PLATFORM
MINGIT_URL = "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.2/MinGit-2.47.1-64-bit.zip"
DOWNLOAD_TIMEOUT = 180
APPROVAL_TIMEOUT = 300

_pending_approvals = {}
_routes_registered = False


def register_approval_route():
    """POST /ComfyUI_Agent/approve {id, decision} resolves a pending approval."""
    global _routes_registered
    if _routes_registered:
        return
    try:
        from aiohttp import web
        from server import PromptServer

        @PromptServer.instance.routes.post("/ComfyUI_Agent/approve")
        async def _approve_handler(request):
            try:
                data = await request.json()
                entry = _pending_approvals.get(str(data.get("id", "")))
                if entry is not None:
                    entry["decision"] = str(data.get("decision", "deny"))
                    entry["event"].set()
            except Exception:
                pass
            return web.json_response({"ok": True})

        _routes_registered = True
        print("[ComfyUI_Agent] approval route ready: POST /ComfyUI_Agent/approve", flush=True)
    except Exception as e:
        print("[ComfyUI_Agent] approval route registration failed: {}".format(e), flush=True)


# 模块导入时即注册（自定义节点加载先于路由表物化；运行中懒注册的路由不会生效）
register_approval_route()


def request_approval(conversation, tool, detail):
    """弹窗询问用户（阻塞直到选择或超时）：返回 'allow' / 'allow_conversation' / 'deny'。"""
    register_approval_route()
    approval_id = uuid.uuid4().hex
    entry = {"event": threading.Event(), "decision": "deny"}
    _pending_approvals[approval_id] = entry
    try:
        from server import PromptServer
        PromptServer.instance.send_sync("ComfyUI_Agent_confirm", {
            "id": approval_id,
            "conversation": conversation,
            "tool": tool,
            "detail": str(detail)[:300],
        })
    except Exception as e:
        print("[ComfyUI_Agent] approval dialog push failed: {}".format(e), flush=True)
    answered = entry["event"].wait(timeout=APPROVAL_TIMEOUT)
    decision = entry["decision"] if answered else "deny"
    _pending_approvals.pop(approval_id, None)
    print("[ComfyUI_Agent] approval {}: {} {} ({})".format(
        approval_id[:8], decision, tool, str(detail)[:80]), flush=True)
    return decision


def _download(url, target_path):
    print("[ComfyUI_Agent:tools] downloading {} -> {}".format(url, target_path), flush=True)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    tmp = target_path + ".part"
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    os.replace(tmp, target_path)


def _download_and_extract_zip(url, dest_dir):
    marker = os.path.join(dest_dir, ".installed")
    if os.path.isfile(marker):
        return
    archive = os.path.join(dest_dir, os.path.basename(url))
    _download(url, archive)
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(dest_dir)
    open(marker, "w").close()


def _resolve_path(root, path):
    """Resolve a model-provided path against the workspace root, refusing escapes."""
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    real = os.path.realpath(path)
    if os.path.commonpath([real, os.path.realpath(root)]) != os.path.realpath(root):
        raise ValueError("路径越出工作区: " + path)
    return real


def _find_bash():
    found = shutil.which("bash")
    if found:
        return found
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        os.path.join(RUNTIME_DIR, "MinGit", "bin", "bash.exe"),
        "/usr/bin/bash", "/bin/bash",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def ensure_bash():
    bash = _find_bash()
    if bash:
        return bash
    if platform.system() == "Windows":
        _download_and_extract_zip(MINGIT_URL, os.path.join(RUNTIME_DIR, "MinGit"))
        bash = _find_bash()
        if bash:
            return bash
    raise RuntimeError("未找到 bash，且自动安装失败；请安装 Git for Windows 或手动放置 bash.exe 到 " + RUNTIME_DIR)


def _fnm_exe():
    exe = os.path.join(FNM_DIR, "fnm.exe" if platform.system() == "Windows" else "fnm")
    if os.path.isfile(exe):
        return exe
    which = shutil.which("fnm")
    if which:
        return which
    _download_and_extract_zip(FNM_URL, FNM_DIR)
    if not os.path.isfile(exe):
        raise RuntimeError("fnm 下载解压后未找到可执行文件: " + FNM_DIR)
    return exe


def _fnm_env():
    env = dict(os.environ)
    env["FNM_DIR"] = FNM_DIR
    return env


def _run(cmd, cwd, timeout, env=None):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=env, shell=False)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    result = ""
    if out:
        result += out + "\n"
    if err:
        result += "[stderr] " + err
    result += "[exit {}]".format(proc.returncode)
    return result


def ensure_node():
    """Make sure fnm + the pinned Node.js version are available; returns (fnm_exe, node_version)."""
    fnm = _fnm_exe()
    env = _fnm_env()
    listed = subprocess.run([fnm, "list"], capture_output=True, text=True, env=env, timeout=120)
    have = NODE_VERSION in (listed.stdout or "")
    if not have:
        print("[ComfyUI_Agent:tools] fnm install Node.js {} (FNM_DIR={})".format(NODE_VERSION, FNM_DIR), flush=True)
        subprocess.run([fnm, "install", NODE_VERSION], capture_output=True, text=True,
                       env=env, timeout=600, check=True)
    return fnm, NODE_VERSION


def run_python(script_path, args=None, cwd=None, timeout=180):
    cmd = [_python_exe(), script_path] + list(args or [])
    return _run(cmd, cwd, timeout)


def run_node(script_path, args=None, cwd=None, timeout=180):
    fnm, version = ensure_node()
    cmd = [fnm, "exec", "--using=" + version, "node", script_path] + list(args or [])
    return _run(cmd, cwd, timeout, env=_fnm_env())


def _python_exe():
    import sys
    return sys.executable


class ToolBox:
    """The four Agent Loop tools, scoped to a workspace folder (the skill's folder)."""

    TOOLS = [
        {"type": "function", "function": {"name": "read", "description": "读取工作区内一个文本文件的全部内容。",
         "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "相对工作区的文件路径"}},
                        "required": ["path"]}}},
        {"type": "function", "function": {"name": "write", "description": "创建或覆盖工作区内一个文本文件。",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"]}}},
        {"type": "function", "function": {"name": "edit", "description": "把文件中的一段旧文本精确替换为新文本。",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                         "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
        {"type": "function", "function": {"name": "bash", "description": "在工作区目录执行一条 bash 命令（Windows 上为 Git Bash），"
                                          "返回 stdout/stderr 与退出码。环境里 python 指向 ComfyUI 的 Python，node 也可用。",
         "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    ]

    def __init__(self, workspace):
        self.workspace = workspace
        self._bash = None

    def execute(self, name, args):
        if name == "read":
            return self.read(args["path"])
        if name == "write":
            return self.write(args["path"], args["content"])
        if name == "edit":
            return self.edit(args["path"], args["old_text"], args["new_text"])
        if name == "bash":
            return self.bash(args["command"])
        raise ValueError("未知工具: " + name)

    def read(self, path):
        with open(_resolve_path(self.workspace, path), "r", encoding="utf-8") as f:
            return f.read()

    def write(self, path, content):
        path = _resolve_path(self.workspace, path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return "written {} ({} chars)".format(path, len(content))

    def edit(self, path, old_text, new_text):
        path = _resolve_path(self.workspace, path)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if old_text not in content:
            raise ValueError("old_text 未在文件中找到")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.replace(old_text, new_text, 1))
        return "edited " + path

    def bash(self, command):
        if self._bash is None:
            self._bash = ensure_bash()
        env = dict(os.environ)
        python_dir = os.path.dirname(_python_exe())
        env["PATH"] = python_dir + os.pathsep + env.get("PATH", "")
        try:
            fnm, version = ensure_node()
            env["FNM_DIR"] = FNM_DIR
        except Exception:
            pass
        return _run([self._bash, "-c", command], self.workspace, timeout=300, env=env)
