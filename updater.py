# -*- coding: utf-8 -*-
"""
AniNote 自动更新模块 — 版本检查、下载、生成替换脚本。

工作流程:
1. check_for_update(): 请求 GitHub Releases API 获取最新版本信息
2. compare_versions(): 语义化版本号对比（支持 3.4 / v3.5 / 3.11）
3. download_update(): 流式下载 release 附件 zip，带进度回调
4. write_update_script(): 生成 update.bat，主程序退出后替换文件并重启

替换策略（Windows exe 运行中无法覆盖）:
- 主程序下载 zip 到临时目录，写入 .last_version 标记（用于更新后展示日志）
- 生成 update.bat 并后台启动，主程序退出
- bat 等待进程结束 → 删除旧 _internal → 解压 zip → 复制覆盖 → 启动新版本 → 自删
- 全程保留 notes_data/ 与 aninote_config.json（用户数据）
"""

import os
import re
import sys
import json
import tempfile
import requests

GITHUB_REPO = "TurboHunter-CN/AniNote"
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
VERSION_MARK_NAME = ".last_version.json"   # 存于程序目录，更新后展示日志用

# GitHub REST API 规范请求头：
# - User-Agent 必填（官方推荐 "AppName/Version (contact)" 格式）
# - Accept 使用官方推荐媒体类型
# - X-GitHub-Api-Version 显式声明 API 版本
API_HEADERS = {
    "User-Agent": "AniNote-Updater/3.4 (https://github.com/TurboHunter-CN/AniNote)",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def app_base_dir():
    """程序所在目录（exe 同级；开发模式为源码目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def version_mark_path():
    return os.path.join(app_base_dir(), VERSION_MARK_NAME)


def compare_versions(a, b):
    """语义化版本号对比，返回 1(a 新) / 0(相同) / -1(b 新)。

    支持 3.4、v3.5、3.11、3.11.1 等格式；逐段转整数比较，
    避免字符串比较导致 3.11 < 3.4 的错误。
    """
    def parse(v):
        v = str(v).strip().lstrip("vV")
        parts = []
        for p in v.split("."):
            num = "".join(ch for ch in p if ch.isdigit())
            parts.append(int(num) if num else 0)
        return parts

    pa, pb = parse(a), parse(b)
    for x, y in zip(pa, pb):
        if x != y:
            return 1 if x > y else -1
    if len(pa) != len(pb):
        return 1 if len(pa) > len(pb) else -1
    return 0


def _proxy_config(proxy_str=""):
    """将用户填写的代理（如 127.0.0.1:7890）转换为 requests 代理字典；空则返回 None。"""
    proxy_str = (proxy_str or "").strip()
    if not proxy_str:
        return None
    if "://" not in proxy_str:
        proxy_str = f"http://{proxy_str}"
    return {"http": proxy_str, "https": proxy_str}


def check_for_update(timeout=10, proxy_str=""):
    """查询 GitHub 最新 release。

    Args:
        timeout: 请求超时秒数。
        proxy_str: 可选代理，如 "127.0.0.1:7890"（大陆网络建议配置）。

    Returns:
        dict 或 None:
        {
            "latest_version": "3.5",
            "notes": "更新日志 Markdown 文本",
            "zip_url": "下载地址",
            "zip_name": "附件文件名",
            "published_at": "发布时间",
        }
        网络失败 / 无 zip 附件时返回 None。
    """
    try:
        resp = requests.get(API_URL, timeout=timeout, headers=API_HEADERS,
                            proxies=_proxy_config(proxy_str))
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    tag = str(data.get("tag_name", "")).lstrip("vV")
    if not tag:
        return None

    assets = data.get("assets", []) or []
    zip_asset = next((a for a in assets if str(a.get("name", "")).lower().endswith(".zip")), None)
    if not zip_asset:
        return None

    return {
        "latest_version": tag,
        "notes": data.get("body", "") or "",
        "zip_url": zip_asset.get("browser_download_url", ""),
        "zip_name": zip_asset.get("name", ""),
        "published_at": data.get("published_at", ""),
    }


def download_update(zip_url, dest_path, progress_cb=None, timeout=30, proxy_str=""):
    """流式下载 zip 到 dest_path，逐块写入并回调进度。

    progress_cb(received_bytes, total_bytes)
    proxy_str: 可选代理，如 "127.0.0.1:7890"。
    Returns:
        True 成功 / False 失败（网络中断、非 200、写盘失败）
    """
    try:
        with requests.get(zip_url, stream=True, timeout=timeout, headers=API_HEADERS,
                          proxies=_proxy_config(proxy_str)) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0) or 0)
            received = 0
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    f.write(chunk)
                    received += len(chunk)
                    if progress_cb:
                        progress_cb(received, total)
        return True
    except Exception:
        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
        except OSError:
            pass
        return False


def write_update_script(base_dir, zip_path):
    """生成 update.bat 并返回其路径。

    bat 执行流程（全部隐藏窗口）:
      1. 终止 app.exe
      2. 等待 2 秒确保文件句柄释放
      3. 删除旧的 _internal 目录（避免遗留旧依赖）
      4. PowerShell 解压 zip 到临时目录
      5. 复制覆盖程序文件（跳过 notes_data / aninote_config.json）
      6. 清理临时文件
      7. 启动新版本 app.exe
      8. 自删 bat

    base_dir: 程序所在目录（exe 同级）
    zip_path: 已下载的 zip 绝对路径
    """
    exe_name = os.path.basename(sys.executable) if getattr(sys, "frozen", False) else "app.exe"
    bat_path = os.path.join(base_dir, "update.bat")
    tmp_dir = os.path.join(base_dir, "_update_tmp")
    zip_name = os.path.basename(zip_path)
    zip_abs = zip_path.replace("/", "\\")
    tmp_abs = tmp_dir.replace("/", "\\")
    dir_abs = base_dir.replace("/", "\\")

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        f'taskkill /f /im {exe_name} >nul 2>&1',
        "timeout /t 2 /nobreak >nul",
        f'cd /d "{dir_abs}"',
        # 删除旧 _internal（PyInstaller one-dir 依赖目录）
        f'if exist "_internal" rmdir /s /q "_internal"',
        # 解压新版本
        f'powershell -NoProfile -Command "Expand-Archive -Path \'{zip_abs}\' -DestinationPath \'{tmp_abs}\' -Force"',
        # 若 zip 内含唯一顶层文件夹则进入该层，再复制覆盖（强制跳过用户数据）
        f'powershell -NoProfile -Command "$src=\'{tmp_abs}\'; $items=@(Get-ChildItem $src -Force); if($items.Count -eq 1 -and $items[0].PSIsContainer){{$src=$items[0].FullName}}; Copy-Item -Path (Join-Path $src \'*\') -Destination \'{dir_abs}\' -Recurse -Force -Exclude \'notes_data\',\'aninote_config.json\'"',
        # 清理
        f'del /f /q "{zip_abs}" >nul 2>&1',
        f'rmdir /s /q "{tmp_abs}" >nul 2>&1',
        # 启动新版本
        f'start "" "{dir_abs}\\{exe_name}"',
        # 自删
        'del /f /q "%~f0" >nul 2>&1',
    ]
    with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\r\n".join(lines))
    return bat_path


def mark_updated(from_version, to_version, notes):
    """写入版本更新标记，供新版本启动时展示更新日志。"""
    try:
        mark = {"from": from_version, "to": to_version, "notes": notes}
        with open(version_mark_path(), "w", encoding="utf-8") as f:
            json.dump(mark, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def read_update_mark():
    """读取并删除版本更新标记；无标记时返回 None。"""
    path = version_mark_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            mark = json.load(f)
        os.remove(path)
        return mark
    except (json.JSONDecodeError, OSError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def temp_download_dir():
    """创建并返回临时下载目录（持久到程序目录，避免 %TEMP% 权限问题）。"""
    d = os.path.join(app_base_dir(), "_update_download")
    os.makedirs(d, exist_ok=True)
    return d
