"""Skill management for the Agent: install from a local folder / zip file / zip URL /
git repo, enable/disable, delete and update. State lives in skills/.state.json;
disabled skills disappear from the catalog and cannot be loaded."""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

FOLDER = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(FOLDER, "skills")
STATE_FILE = os.path.join(SKILLS_DIR, ".state.json")

DOWNLOAD_TIMEOUT = 120
MAX_DOWNLOAD = 256 * 1024 * 1024  # 256MB 上限
GIT_TIMEOUT = 180

_routes_registered = False


def register_routes():
    """GET/POST /ComfyUI_Agent/skills* — 管理 REST 接口（导入时注册，同 tools.py 的理由）。"""
    global _routes_registered
    if _routes_registered:
        return
    try:
        import asyncio
        from aiohttp import web
        from server import PromptServer

        @PromptServer.instance.routes.get("/ComfyUI_Agent/skills")
        async def _list_skills_handler(request):
            return web.json_response(list_skills_info())

        @PromptServer.instance.routes.post("/ComfyUI_Agent/skills/install")
        async def _install_handler(request):
            data = await request.json()
            loop = asyncio.get_running_loop()
            try:
                names = await loop.run_in_executor(None, install_skill, data)
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                return web.json_response({"error": str(e)}, status=400)
            return web.json_response({"installed": names, **list_skills_info()})

        @PromptServer.instance.routes.post("/ComfyUI_Agent/skills/toggle")
        async def _toggle_handler(request):
            data = await request.json()
            try:
                set_enabled(str(data.get("name", "")), bool(data.get("enabled", True)))
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
            return web.json_response(list_skills_info())

        @PromptServer.instance.routes.post("/ComfyUI_Agent/skills/delete")
        async def _delete_handler(request):
            data = await request.json()
            try:
                delete_skill(str(data.get("name", "")))
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
            return web.json_response(list_skills_info())

        @PromptServer.instance.routes.post("/ComfyUI_Agent/skills/update")
        async def _update_handler(request):
            data = await request.json()
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, update_skill, str(data.get("name", "")))
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                return web.json_response({"error": str(e)}, status=400)
            return web.json_response(list_skills_info())

        _routes_registered = True
        print("[ComfyUI_Agent] skill routes ready: /ComfyUI_Agent/skills*", file=sys.stderr, flush=True)
    except Exception as e:
        print("[ComfyUI_Agent] skill route registration failed: {}".format(e), file=sys.stderr, flush=True)


def skill_description(name):
    """SKILL.md frontmatter 的 description，缺失则取正文开头（跳过标题行）。"""
    with open(os.path.join(SKILLS_DIR, name, "SKILL.md"), "r", encoding="utf-8") as f:
        text = f.read()
    body = text
    if text.startswith("---"):
        head, sep, rest = text[3:].partition("---")
        if sep:
            body = rest
            for line in head.splitlines():
                if line.lower().startswith("description:"):
                    return line.split(":", 1)[1].strip()
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return " ".join(lines)[:200]


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    os.makedirs(SKILLS_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def is_enabled(name):
    entry = _load_state().get(name)
    return bool(entry.get("enabled", True)) if isinstance(entry, dict) else True


def set_enabled(name, enabled):
    d = _skill_dir(name)
    state = _load_state()
    entry = state.get(name) if isinstance(state.get(name), dict) else {}
    entry["enabled"] = bool(enabled)
    state[name] = entry
    _save_state(state)
    return d


def _skill_dir(name):
    """校验技能名并返回其目录；名字必须是不带路径的单段目录名且真实存在。"""
    if not name or os.path.basename(name) != name or name in (".", ".."):
        raise ValueError("非法技能名: " + name)
    d = os.path.realpath(os.path.join(SKILLS_DIR, name))
    if os.path.commonpath([d, os.path.realpath(SKILLS_DIR)]) != os.path.realpath(SKILLS_DIR) or not os.path.isdir(d):
        raise ValueError("技能不存在: " + name)
    return d


def _installed_names():
    if not os.path.isdir(SKILLS_DIR):
        return []
    return sorted(n for n in os.listdir(SKILLS_DIR)
                  if os.path.isdir(os.path.join(SKILLS_DIR, n))
                  and os.path.isfile(os.path.join(SKILLS_DIR, n, "SKILL.md")))


def list_skills_info():
    state = _load_state()
    skills = []
    for name in _installed_names():
        entry = state.get(name) if isinstance(state.get(name), dict) else {}
        skills.append({
            "name": name,
            "description": skill_description(name),
            "enabled": entry.get("enabled", True),
            "has_scripts": os.path.isdir(os.path.join(SKILLS_DIR, name, "scripts")),
            "source": entry.get("source"),
        })
    return {"skills": skills}


def _safe_name(name):
    name = os.path.basename(os.path.normpath(name)).strip()
    if not name or name in (".", "..") or any(c in name for c in "/\\") or name.startswith("."):
        raise ValueError("非法技能目录名: " + name)
    return name


def _copy_tree(src, dest):
    # 技能目录不携带 .git（克隆源的仓库历史既无用也会让后续更新无法删除）
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))


def _rmtree(path):
    # Windows 下 git 目录里的对象文件是只读的，直接删会被拒
    def onerror(func, p, exc):
        os.chmod(p, 0o777)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def _discover_skill_dirs(root):
    """递归找出含 SKILL.md 的技能目录（跳过 .git），返回 {绝对目录: 技能名}。
    同名目录出现在不同位置时报错——无法以同一个名字同时安装。"""
    found = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        if "SKILL.md" not in files:
            continue
        path = os.path.abspath(base)
        name = os.path.basename(os.path.normpath(base))
        if name in found.values():
            raise ValueError("发现重名技能目录（{}）：无法同时安装 {} 与 {}".format(
                name, next(p for p, n in found.items() if n == name), os.path.relpath(path, root)))
        found[path] = name
    return found


def _ingest_source_dir(src_dir, source_meta):
    """把一个源目录装进 skills/：根含 SKILL.md 为单技能（忽略子目录）；
    否则递归发现各深度含 SKILL.md 的技能目录（含 .git 的目录跳过）。"""
    if os.path.isfile(os.path.join(src_dir, "SKILL.md")):
        return [_install_single(src_dir, source_meta)]
    installed = []
    for d, name in sorted(_discover_skill_dirs(src_dir).items()):
        installed.append(_install_single(d, source_meta, name))
    if not installed:
        raise ValueError("来源里没有找到任何包含 SKILL.md 的技能目录")
    return installed


def _install_single(src_dir, source_meta, name=None):
    name = _safe_name(name or os.path.basename(os.path.normpath(src_dir)))
    dest = os.path.join(SKILLS_DIR, name)
    if os.path.exists(dest):
        raise ValueError("技能已存在: {}（先删除或改名后再安装）".format(name))
    _copy_tree(src_dir, dest)
    state = _load_state()
    state[name] = {"enabled": True, "source": source_meta}
    _save_state(state)
    return name


def install_skill(data):
    """按 data.type 安装：local=文件夹路径 / zip=zip文件路径 / zip_url=zip下载地址 / git=Git仓库地址。"""
    kind = str(data.get("type", "")).strip()
    source = str(data.get("source", "")).strip()
    if not source:
        raise ValueError("安装来源不能为空")
    if kind == "local":
        src = os.path.realpath(source)
        if not os.path.isdir(src):
            raise ValueError("本地目录不存在: " + source)
        return _ingest_source_dir(src, {"type": "local", "path": source})
    if kind == "zip":
        if not os.path.isfile(source):
            raise ValueError("zip 文件不存在: " + source)
        with tempfile.TemporaryDirectory() as td:
            _safe_extract(source, td)
            return _ingest_source_dir(td, {"type": "zip", "path": source})
    if kind == "zip_url":
        if not source.lower().startswith(("http://", "https://")):
            raise ValueError("zip 直链必须是 http(s) 地址")
        with tempfile.TemporaryDirectory() as td:
            zip_path = os.path.join(td, "skill.zip")
            _download(source, zip_path)
            _safe_extract(zip_path, td)
            return _ingest_source_dir(td, {"type": "zip_url", "url": source})
    if kind == "git":
        if not source.lower().startswith(("http://", "https://", "git@", "file://")):
            raise ValueError("Git 来源应是仓库地址（http/https/git@）")
        # 地址可以是仓库根，也可以是仓库内的子路径：逐级向上探测真正的仓库根，
        # 整个仓库只克隆一次进 .repos 缓存（键 = 仓库根地址的哈希），同仓库共享；
        # 带子路径时只导入该子路径下的技能。
        root_url, subpath = _resolve_repo_root(source)
        cache_rel = os.path.join(".repos", _url_hash(root_url))
        cache = os.path.join(SKILLS_DIR, cache_rel)
        if os.path.isdir(os.path.join(cache, ".git")):
            _git_update_cache(cache_rel)  # 同仓库已装过其它技能：复用缓存，增量拉取
        else:
            td = tempfile.mkdtemp()
            try:
                clone_into = os.path.join(td, "repo")
                _git_clone(root_url, clone_into)
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                if os.path.exists(cache):
                    _rmtree(cache)
                shutil.move(clone_into, cache)
            finally:
                shutil.rmtree(td, ignore_errors=True)
        discovered_all = _discover_skill_dirs(cache)
        if subpath:
            prefix = os.path.normpath(os.path.join(cache, subpath))
            discovered = {}
            for p, n in discovered_all.items():
                try:
                    if os.path.commonpath([p, prefix]) == prefix:
                        discovered[p] = n
                except ValueError:
                    pass
            if not discovered:
                raise ValueError("指定子路径下没有找到技能: " + subpath)
        else:
            discovered = discovered_all
        # 单技能仓库（根含 SKILL.md 且仓库里只有这一个技能）：整体挪为技能目录，原地增量更新
        if not subpath and len(discovered) == 1 and os.path.isfile(os.path.join(cache, "SKILL.md")):
            name = _url_repo_name(root_url)
            dest = os.path.join(SKILLS_DIR, name)
            if os.path.exists(dest):
                raise ValueError("技能已存在: {}（先删除或改名后再安装）".format(name))
            shutil.move(cache, dest)
            state = _load_state()
            state[name] = {"enabled": True, "source": {"type": "git", "url": root_url, "mode": "inplace"}}
            _save_state(state)
            return [name]
        installed = []
        for d, name in sorted(discovered.items()):
            installed.append(_install_single(d, {"type": "git", "url": root_url, "cache": cache_rel}, name))
        if not installed:
            raise ValueError("来源里没有找到任何包含 SKILL.md 的技能目录")
        return installed
    raise ValueError("未知安装类型: " + kind)


def _git_candidates(url):
    """从用户地址生成候选仓库根：原始地址在前，逐级向上剥路径（至少保留 2 段）。"""
    url = url.rstrip("/")
    candidates = [url]
    if "://" in url:
        scheme, rest = url.split("://", 1)
    else:  # git@host:owner/repo/...
        scheme, rest = url.split(":", 1)
    segments = rest.split("/")
    for i in range(len(segments) - 1, 1, -1):
        candidates.append(scheme + "://" + "/".join(segments[:i]) if "://" in url
                          else scheme + ":" + "/".join(segments[:i]))
    return candidates


def _resolve_repo_root(url):
    """探测可克隆的仓库根（子路径无法直接克隆）：返回 (仓库根地址, 相对子路径)。"""
    for candidate in _git_candidates(url):
        probe = _run_git(["ls-remote", "--heads", candidate])
        if probe.returncode == 0:
            subpath = url[len(candidate):].lstrip("/") if url.startswith(candidate) else ""
            return candidate.rstrip("/"), subpath
    raise ValueError("无法从地址定位 Git 仓库（已逐级向上尝试）。请确认地址指向一个可访问的 Git 仓库或其内部路径")


def _url_repo_name(url):
    base = url.rstrip("/")
    if "@" in base and "://" not in base:  # git@host:owner/repo.git 形态
        base = base.split(":", 1)[1]
    name = os.path.basename(base)
    if name.endswith(".git"):
        name = name[:-4]
    return _safe_name(name)


def _url_hash(url):
    import hashlib
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _run_git(args, cwd=None):
    git = _find_git()
    if not git:
        raise ValueError("未找到 git；请安装 Git for Windows 后重试")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run([git] + args, cwd=cwd, capture_output=True, text=True,
                          timeout=GIT_TIMEOUT, env=env)


def _git_update_in_place(skill_dir):
    """原地增量更新一个带 .git 的技能：检查本地改动 → fetch → reset 到远端最新。"""
    dirty = _run_git(["status", "--porcelain"], cwd=skill_dir)
    if dirty.returncode != 0:
        raise ValueError("git status 失败: {}".format((dirty.stderr or "")[:200]))
    if dirty.stdout.strip():
        raise ValueError("技能目录存在本地改动（git status 非空），更新会覆盖它们；请先备份或还原后再更新")
    fetched = _run_git(["fetch", "--depth", "1", "origin"], cwd=skill_dir)
    if fetched.returncode != 0:
        raise ValueError("git fetch 失败: {}".format((fetched.stderr or fetched.stdout or "")[:300]))
    reset = _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=skill_dir)
    if reset.returncode != 0:
        raise ValueError("git reset 失败: {}".format((reset.stderr or reset.stdout or "")[:300]))


def _git_update_cache(cache_rel):
    cache = os.path.join(SKILLS_DIR, cache_rel)
    if not os.path.isdir(os.path.join(cache, ".git")):
        raise ValueError("技能仓库缓存缺失: " + cache_rel)
    fetched = _run_git(["fetch", "--depth", "1", "origin"], cwd=cache)
    if fetched.returncode != 0:
        raise ValueError("git fetch 失败: {}".format((fetched.stderr or fetched.stdout or "")[:300]))
    reset = _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=cache)
    if reset.returncode != 0:
        raise ValueError("git reset 失败: {}".format((reset.stderr or reset.stdout or "")[:300]))
    return cache


def update_skill(name):
    """git 来源：原地增量拉取（.git 保留在技能里）或刷新仓库缓存后再拷贝；
    其它来源：按记录的来源重新拉取并整体替换技能目录。"""
    d = _skill_dir(name)
    source = _load_state().get(name, {}).get("source")
    if not source:
        raise ValueError("技能 '{}' 没有来源信息，无法更新".format(name))
    kind = source.get("type")
    if kind == "git":
        if os.path.isdir(os.path.join(d, ".git")):
            _git_update_in_place(d)
            return
        if source.get("cache"):
            cache = _git_update_cache(source["cache"])
            discovered = _discover_skill_dirs(cache)
            picked = next((p for p, n in discovered.items() if n == name), None)
            if picked is None:
                raise ValueError("来源里没有找到 SKILL.md，无法更新")
            _rmtree(d)
            _copy_tree(picked, d)
            return
        raise ValueError("该技能的 Git 仓库元数据缺失，请删除后重新安装")
    with tempfile.TemporaryDirectory() as td:
        if kind == "zip_url":
            zip_path = os.path.join(td, "skill.zip")
            _download(source["url"], zip_path)
            _safe_extract(zip_path, td)
        elif kind == "zip":
            if not os.path.isfile(source.get("path", "")):
                raise ValueError("原 zip 文件已不存在: " + str(source.get("path")))
            _safe_extract(source["path"], td)
        elif kind == "local":
            src = os.path.realpath(source.get("path", ""))
            if not os.path.isdir(src):
                raise ValueError("原本地目录已不存在: " + str(source.get("path")))
            td = src  # 直接以本地目录为源
        else:
            raise ValueError("未知来源类型: " + str(kind))
        # 优先找同名子目录，否则接受根目录单技能
        candidates = [td] if os.path.isfile(os.path.join(td, "SKILL.md")) else []
        for entry in sorted(os.listdir(td)):
            sub = os.path.join(td, entry)
            if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "SKILL.md")):
                candidates.append(sub)
        picked = next((c for c in candidates if os.path.basename(os.path.normpath(c)) == name), None)
        if picked is None and candidates:
            picked = candidates[0]
        if picked is None:
            raise ValueError("来源里没有找到 SKILL.md，无法更新")
        _rmtree(d)
        _copy_tree(picked, d)
    state = _load_state()
    entry = state.get(name) if isinstance(state.get(name), dict) else {"enabled": True}
    state[name] = entry
    _save_state(state)


def delete_skill(name):
    d = _skill_dir(name)
    _rmtree(d)
    state = _load_state()
    entry = state.pop(name, None)
    # 删掉的技能若持有仓库缓存且没有其它技能引用，缓存一并清掉
    if isinstance(entry, dict) and entry.get("source", {}).get("cache"):
        cache_rel = entry["source"]["cache"]
        still_used = any(
            isinstance(v, dict) and v.get("source", {}).get("cache") == cache_rel
            for v in state.values()
        )
        if not still_used:
            cache = os.path.join(SKILLS_DIR, cache_rel)
            if os.path.isdir(cache):
                _rmtree(cache)
    _save_state(state)


def _download(url, target_path):
    print("[ComfyUI_Agent:skills] downloading {} -> {}".format(url, target_path), file=sys.stderr, flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "ComfyUI_Agent"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as r, open(target_path, "wb") as f:
        total = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise ValueError("下载超过 256MB 上限，已中止")
            f.write(chunk)


def _safe_extract(zip_path, dest):
    """逐条解压并防 zip-slip：条目路径解析后必须仍落在 dest 内。"""
    dest = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.filename.endswith("/"):
                continue
            target = os.path.realpath(os.path.join(dest, info.filename))
            try:
                inside = os.path.commonpath([target, dest]) == dest
            except ValueError:  # 跨盘符等
                inside = False
            if not inside:
                raise ValueError("zip 内含非法路径: " + info.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)


def _find_git():
    found = shutil.which("git")
    if found:
        return found
    candidates = [os.path.join(FOLDER, ".runtime", "MinGit", "cmd", "git.exe"),
                  os.path.join(FOLDER, ".runtime", "MinGit", "bin", "git.exe")]
    candidates += glob.glob(os.path.join(FOLDER, ".runtime", "MinGit", "*", "cmd", "git.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _git_clone(url, dest):
    git = _find_git()
    if not git:
        raise ValueError("未找到 git；请安装 Git for Windows 或通过「⚙ 服务商」流程触发 MinGit 安装后重试")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # 私有仓库直接失败，不挂起等待输入
    print("[ComfyUI_Agent:skills] git clone --depth 1 {} -> {}".format(url, dest), file=sys.stderr, flush=True)
    proc = subprocess.run([git, "clone", "--depth", "1", url, dest],
                          capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env)
    if proc.returncode != 0:
        raise ValueError("git clone 失败: {}".format((proc.stderr or proc.stdout or "")[:300]))


register_routes()
