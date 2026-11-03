"""技能管理命令行：复用插件安装器（skill_manager）的全部逻辑。

所有命令输出一行 JSON；失败输出 {"error": "..."} 并以非零码退出。
在插件根目录下用 ComfyUI 的 Python 执行：
    python skills/skill-manager/scripts/cli.py <子命令> ...
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # scripts → skill-manager → skills → 插件根
sys.path.insert(0, ROOT)

from skill_manager import (delete_skill, install_skill, list_skills_info,
                           set_enabled, update_skill)


def build_parser():
    p = argparse.ArgumentParser(prog="cli.py", description="技能安装与管理")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出全部技能及来源/启停状态")
    install = sub.add_parser("install", help="安装技能")
    install.add_argument("--type", required=True,
                         choices=["local", "zip", "zip_url", "git"],
                         help="来源类型：本地文件夹 / zip 文件 / zip 下载直链 / Git 仓库")
    install.add_argument("--source", required=True, help="路径或地址")
    update = sub.add_parser("update", help="按记录的来源更新技能")
    update.add_argument("--name", required=True)
    toggle = sub.add_parser("toggle", help="停用/启用技能")
    toggle.add_argument("--name", required=True)
    toggle.add_argument("--enabled", required=True, choices=["true", "false"])
    delete = sub.add_parser("delete", help="删除技能（目录不可恢复，先与用户确认）")
    delete.add_argument("--name", required=True)
    return p


def main():
    args = build_parser().parse_args()
    if args.cmd == "list":
        out = list_skills_info()
    elif args.cmd == "install":
        names = install_skill({"type": args.type, "source": args.source})
        out = {"installed": names, **list_skills_info()}
    elif args.cmd == "update":
        update_skill(args.name)
        out = {"updated": args.name, **list_skills_info()}
    elif args.cmd == "toggle":
        set_enabled(args.name, args.enabled == "true")
        out = {"toggled": args.name, **list_skills_info()}
    else:  # delete
        delete_skill(args.name)
        out = {"deleted": args.name, **list_skills_info()}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
