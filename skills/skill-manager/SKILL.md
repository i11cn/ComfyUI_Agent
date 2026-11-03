---
name: skill-manager
description: 安装、启停、更新与删除技能：支持本地目录复制、本地 zip 解压、在线 zip 下载、Git 仓库克隆（含多技能仓库"克隆+复制"两步安装）。当用户想安装新技能、管理已有技能（启停/更新/删除）或询问技能来源与规则时使用。
---

# 技能安装与管理

让用户通过对话完成技能的安装与管理，不需要手工维护 `skills/` 目录。

## 基本约定

- 技能 = 含 `SKILL.md` 的文件夹；多技能仓库 = 根目录无 SKILL.md、多个子目录各含 SKILL.md。
- 所有机械操作用 `scripts/cli.py` 执行，它复用系统安装器的全部逻辑（路径校验、zip-slip 防护、重名检测、状态与缓存维护），**不要手工重造这些步骤**：

```
python skills/skill-manager/scripts/cli.py list
python skills/skill-manager/scripts/cli.py install --type local|zip|zip_url|git --source <路径或地址>
python skills/skill-manager/scripts/cli.py update --name <技能名>
python skills/skill-manager/scripts/cli.py toggle --name <技能名> --enabled true|false
python skills/skill-manager/scripts/cli.py delete --name <技能名>
```

- 每条命令输出一行 JSON：成功含 `installed` / `skills` 字段；失败含 `{"error": "..."}`，把 error 原样转告用户。
- 安装或更新成功后告知用户：新内容立即生效（模型下一轮运行即可加载），也可打开节点上的「⚙ 技能」弹窗查看清单。

## 安装流程（按来源）

### 本地目录复制（--type local）

1. 确认目录存在（`ls` 检查）。
2. 源根目录含 `SKILL.md` = 单技能，整体拷入 `skills/<目录名>/`。
3. 源根目录无 `SKILL.md`、但多个子目录各含 `SKILL.md` = 多技能包，全部导入。
4. 目标 `skills/<名字>/` 已存在 → cli 会报"已存在"，此时询问用户：先删除旧的，还是放弃。

### 本地 zip 解压（--type zip）

- cli 内部先解压到临时目录再按上面规则导入，解压时逐条校验条目路径不得逃出解压目录（zip-slip 防护），非法 zip 直接报错。
- 你只需确认 zip 文件存在，然后执行 install 命令。

### 在线 zip 下载（--type zip_url）

- 来源必须是 http(s) 直链；cli 内部下载（120 秒超时、256MB 上限）后走与本地 zip 相同的解压导入流程。
- 下载失败或超限时如实转告用户。

### Git 仓库克隆（--type git）

- **单技能仓库**（根目录含 SKILL.md）：直接 `git clone --depth 1` 到 `skills/<仓库名>/`，`.git` 保留在里面。之后更新走原地增量拉取（见"更新"）。
- **多技能仓库**（根目录无 SKILL.md）：**克隆和复制两步**——
  1. 克隆仓库本体到 `skills/.repos/<sha1(url)前12位>/`（含 .git）；
  2. 把每个含 SKILL.md 的子目录拷贝到 `skills/<子目录名>/`。
- 克隆固定 `--depth 1` 且 `GIT_TERMINAL_PROMPT=0`：私有仓库会直接报错而不是挂起等待输入。
- 若克隆后既没有根 SKILL.md 也没有任何含 SKILL.md 的子目录，如实告知用户该仓库不是技能仓库。

## .state.json 规则

- 位置 `skills/.state.json`，纯 JSON 对象：`{"技能名": {"enabled": true, "source": {...}}}`。
- **没有条目 = 启用**；`enabled: false` 的技能从模型可见清单消失，且 `load_skill` 会直接拒绝加载。
- `source` 记录安装来源，供"更新"使用：
  - `{"type": "local", "path": "..."}` / `{"type": "zip", "path": "..."}` / `{"type": "zip_url", "url": "..."}`
  - 单技能 Git 仓库：`{"type": "git", "url": "...", "mode": "inplace"}`
  - 多技能 Git 仓库：`{"type": "git", "url": "...", "cache": ".repos/<哈希>"}`
- 该文件必须由程序读写（cli 内部已做原子替换），**不要用文本拼接的方式手改**——改坏 JSON 会丢失全部技能的启停与来源状态。
- 这是用户本地数据：不要把文件全文展示或提交到仓库。

## .repos 目录规则

- 位置 `skills/.repos/<sha1(url)前12位>/`，保存多技能 Git 仓库的完整克隆（含 .git）；技能本体是子目录的拷贝（不含 .git）。
- 仓库根目录没有 SKILL.md，所以系统扫描技能清单时会自动忽略它。
- **更新**：先在缓存目录 `git fetch --depth 1` + `reset --hard FETCH_HEAD`，再把对应子目录重新拷贝到 `skills/<名字>/`。
- **清理**：删除引用某缓存的最后一个技能时，缓存目录随之删除（cli 已内置）；`.repos` 里不应有长期无人引用的残留。

## 更新细则

- **Git 单技能（技能目录内有 .git）**：原地增量——先 `git status` 检查，若存在本地改动则**拒绝更新**并告知用户先备份或还原（绝不能静默覆盖用户的修改）；干净则 `git fetch --depth 1` + `reset --hard FETCH_HEAD`。cli 已内置这套检查。
- **Git 多技能（缓存型）**：先增量拉取缓存仓库，再重拷对应子目录。
- **local / zip**：按记录的 path 重新复制/解压；原路径已不存在时如实告知用户。
- **没有 source 记录的技能**（例如手工放入的）无法更新，如实说明。

## 停用 / 启用 / 删除

- 停用：`toggle --enabled false`；启用：`toggle --enabled true`。
- **删除前必须先向用户确认一次**（目录删除不可恢复），确认后再执行 `delete`。
- 删除会同时清除技能目录与 .state.json 里的对应条目；引用 `.repos` 缓存的最后一个技能被删除时，缓存目录一并清理。

## 安全与边界

- 一切文件操作限制在 `skills/`、`.repos` 缓存与明确的临时目录内，绝不越界写其它路径。
- 来源地址只接受 http(s) 与 git@ 形式；对来路不明的仓库/直链，安装前向用户说明风险。
- 技能名不允许包含路径分隔符、不允许以 `.` 开头；与现有技能重名时停止并询问。
