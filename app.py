"""
AniNote 应用入口 — 系统托盘、全局热键、Bangumi 新番同步、自启管理。
"""

import sys
import os
import json
import re
import winreg
import requests
import ctypes
import threading
import subprocess
import datetime
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QObject, Signal, QAbstractNativeEventFilter, QTimer
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QMessageBox,
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QProgressBar,
)
from PySide6.QtGui import QIcon, QAction


# 模块级：在 import 任何业务模块之前尽早静默 libpng iCCP 色彩配置警告
# （外部 PNG 常见，无害但噪音大）。libpng 警告经 Qt 消息系统，可被拦截；
# 另加 stderr 重定向兜底，覆盖 C 层直接写 fd2 的路径。
def _install_iccp_filter():
    from PySide6.QtCore import qInstallMessageHandler

    def _msg_handler(mode, context, message):
        m = str(message)
        if "libpng" in m or "iCCP" in m:
            return
        print(m, file=sys.stderr)

    qInstallMessageHandler(_msg_handler)


def _install_stderr_iccp_filter():
    """终极兜底：重定向 stderr（fd2），过滤含 libpng/iCCP 的行后透传其余输出。"""
    try:
        real_fd = os.dup(2)
        r_fd, w_fd = os.pipe()
        os.dup2(w_fd, 2)
        os.close(w_fd)

        def _pump():
            try:
                with os.fdopen(r_fd, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "iCCP" in line or "libpng" in line:
                            continue
                        try:
                            os.write(real_fd, line.encode("utf-8", errors="replace"))
                        except Exception:
                            pass
            except Exception:
                pass

        threading.Thread(target=_pump, daemon=True).start()
    except Exception:
        pass


_install_iccp_filter()
_install_stderr_iccp_filter()

import main as note_app
import control_panel as cp_app
import updater


# ==========================================
#  Bangumi 数据引擎
# ==========================================

class BangumiUpdater(QObject):
    """跨线程信号中转器，用于将网络请求结果推回主线程。

    payload 可为：dict（同步成功的番剧日程）/ "loading" / str（错误信息）。
    """
    update_signal = Signal(object)


bgm_updater = BangumiUpdater()


def _fetch_all_episodes(subject_ids, headers, proxies):
    """并发拉取多部番剧的剧集表（失败条目跳过，不阻断主流程）。

    Returns:
        {subject_id: {"total": int, "by_date": {"YYYYMMDD": ep, ...},
                      "list": [{id, ep, sort, name, airdate}, ...]}}
        by_date 的 key 为去横线日期（如 "20260818"），值为本季内集号 ep。
        list 为完整剧集表（供集数标记弹窗预缓存，避免右键时再请求）。
        ⚠️ 用 ep 而非 sort：ep_status 是"本季内看到第几集"（ep 基准），
        而 sort 是绝对编号（跨季番剧可能从 13 起），两者基准不一致会误判自动灰。
    """
    def fetch_one(sid):
        try:
            r = requests.get(
                "https://api.bgm.tv/v0/episodes",
                params={"subject_id": sid, "limit": 100},
                headers=headers, proxies=proxies, timeout=15,
            )
            if r.status_code != 200:
                return sid, None
            j = r.json()
            data = j.get("data", []) or []
            total = int(j.get("total") or len(data) or 0)
            by_date = {}
            ep_list = []
            for ep in data:
                ad = str(ep.get("airdate") or "").replace("-", "")
                if ad:
                    # 优先 ep（本季内序号）；个别条目 ep 缺失时回退 sort
                    by_date[ad] = int(ep.get("ep") or ep.get("sort") or 0)
                ep_list.append({
                    "id": ep.get("id"),
                    "ep": ep.get("ep"),
                    "sort": ep.get("sort"),
                    "name": ep.get("name_cn") or ep.get("name") or "",
                    "airdate": ep.get("airdate") or "",
                })
            return sid, {"total": total, "by_date": by_date, "list": ep_list}
        except Exception:
            return sid, None

    out = {}
    if not subject_ids:
        return out
    with ThreadPoolExecutor(max_workers=6) as ex:
        for sid, res in ex.map(fetch_one, subject_ids):
            if res:
                out[sid] = res
    return out


def fetch_bangumi_data(uid, proxy_str=""):
    """从 Bangumi API 拉取用户的在追新番日历。

    Args:
        uid: Bangumi 用户 ID。
        proxy_str: 代理地址，格式如 \"127.0.0.1:7890\"，留空表示直连。

    Returns:
        dict: 按星期索引的番剧列表，{0~6: [{"name", "subject_id", "ep_status",
              "total_eps", "episodes"}, ...]}；失败时返回错误描述字符串。
              "episodes" 为该番剧的 {日期YYYYMMDD: 集号sort} 映射（用于自动灰判定）。
    """
    try:
        headers = {
            'User-Agent': f'HunterHasCome/AniNote/{note_app.VERSION} (https://github.com/TurboHunter-CN/AniNote)',
            'X-Contact':  'Bilibili: https://space.bilibili.com/499162799',
        }
        proxies = None
        if proxy_str:
            clean_proxy = proxy_str.replace("http://", "").replace("https://", "")
            proxies = {
                'http': f'http://{clean_proxy}',
                'https': f'http://{clean_proxy}',
            }

        # 获取「在看」列表（含 ep_status：用户标记看到第几集）
        url = f"https://api.bgm.tv/v0/users/{uid}/collections?subject_type=2&type=3&limit=100"
        res_coll = requests.get(url, headers=headers, proxies=proxies, timeout=15)

        if res_coll.status_code != 200:
            snippet = res_coll.text[:150].replace('<', '&lt;').replace('>', '&gt;')
            return (
                f"获取在看列表被拒！<br>"
                f"状态码: {res_coll.status_code}<br>"
                f"服务器返回: {snippet}"
            )

        watching = {item['subject_id']: item for item in res_coll.json().get('data', [])}
        if not watching:
            return {}

        # 并发拉取在看番剧的剧集表：补全总集数 + 生成"日期→集号"映射（失败条目跳过）
        eps_maps = _fetch_all_episodes(list(watching.keys()), headers, proxies)

        # 获取日历
        res_cal = requests.get(
            "https://api.bgm.tv/calendar",
            headers=headers, proxies=proxies, timeout=15
        )
        if res_cal.status_code != 200:
            return f"获取新番日历被拒！<br>状态码: {res_cal.status_code}"

        calendar_data = res_cal.json()
        schedule = {i: [] for i in range(7)}
        seen_ids = set()  # 已出现在当前季度日历的在看番剧

        for day_data in calendar_data:
            weekday_idx = day_data['weekday']['id'] - 1
            for item in day_data.get('items', []):
                coll = watching.get(item['id'])
                if coll is None:
                    continue
                seen_ids.add(item['id'])
                name = item.get('name_cn') or item.get('name') or "未知番剧"
                em = eps_maps.get(item['id']) or {}
                # 总集数：剧集表 total 优先（条目 eps 字段可能为空），否则回退 eps 字段
                total_eps = em.get("total") or coll.get('subject', {}).get('eps') or 0
                schedule[weekday_idx].append({
                    "name": name,
                    "subject_id": item['id'],
                    "ep_status": int(coll.get('ep_status', 0) or 0),
                    "total_eps": int(total_eps),
                    "episodes": em.get("by_date") or {},
                    "episode_list": em.get("list") or [],   # 预缓存剧集表（集数弹窗免请求）
                })

        # 跨季度半年番：在看但不在当前季度日历（Bangumi 日历只含当季，如《黄泉使者》）。
        # 按最近一集播出日（无剧集表时回退首播日期）的星期归类追加，保证仍在更新的旧番可见。
        for sid, coll in watching.items():
            if sid in seen_ids:
                continue
            em = eps_maps.get(sid) or {}
            by_date = em.get("by_date") or {}
            weekday = None
            if by_date:
                try:
                    weekday = datetime.datetime.strptime(max(by_date), "%Y%m%d").weekday()
                except Exception:
                    weekday = None
            if weekday is None:
                d0 = str(coll.get('subject', {}).get('date') or "")
                try:
                    weekday = datetime.datetime.strptime(d0, "%Y-%m-%d").weekday()
                except Exception:
                    continue  # 无法确定星期几，跳过该条目
            name = coll.get('subject', {}).get('name_cn') or coll.get('subject', {}).get('name') or "未知番剧"
            schedule[weekday].append({
                "name": name,
                "subject_id": sid,
                "ep_status": int(coll.get('ep_status', 0) or 0),
                "total_eps": int(em.get("total") or coll.get('subject', {}).get('eps') or 0),
                "episodes": by_date,
                "episode_list": em.get("list") or [],   # 预缓存剧集表（集数弹窗免请求）
            })

        return schedule
    except Exception as e:
        return (
            f"底层网络异常！<br>"
            f"请检查代理端口是否填写正确。<br>"
            f"错误详情: {str(e)}"
        )



# ==========================================
#  Windows 原生热键引擎
# ==========================================

class Win32HotkeyManager(QAbstractNativeEventFilter):
    """基于 Win32 RegisterHotKey 的全局热键管理器。

    绕过杀软/反作弊对 keyboard 库的拦截，直接向系统注册热键。
    """

    WM_HOTKEY = 0x0312

    VK_MAP = {
        'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45,
        'f': 0x46, 'g': 0x47, 'h': 0x48, 'i': 0x49, 'j': 0x4A,
        'k': 0x4B, 'l': 0x4C, 'm': 0x4D, 'n': 0x4E, 'o': 0x4F,
        'p': 0x50, 'q': 0x51, 'r': 0x52, 's': 0x53, 't': 0x54,
        'u': 0x55, 'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59,
        'z': 0x5A,
        '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34,
        '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39,
        'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74,
        'f6': 0x75, 'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79,
        'f11': 0x7A, 'f12': 0x7B,
    }

    MOD_MAP = {
        'alt': 0x0001,
        'ctrl': 0x0002,
        'shift': 0x0004,
        'win': 0x0008,
    }

    def __init__(self):
        super().__init__()
        self.hotkeys = {}
        self.hk_id_counter = 1
        self.is_paused = False
        self.master_hk_id = None

    def register(self, hotkey_str, callback):
        """注册一个全局热键。

        Args:
            hotkey_str: 键位描述，如 \"alt+n\"、\"ctrl+shift+f1\"。
            callback: 触发时调用的无参回调函数。
        """
        parts = hotkey_str.lower().split('+')
        modifiers = 0
        vk = 0
        for p in parts:
            p = p.strip()
            if p in self.MOD_MAP:
                modifiers |= self.MOD_MAP[p]
            elif p in self.VK_MAP:
                vk = self.VK_MAP[p]
        if vk == 0:
            return -1

        hk_id = self.hk_id_counter
        self.hk_id_counter += 1
        success = ctypes.windll.user32.RegisterHotKey(None, hk_id, modifiers, vk)
        if success:
            self.hotkeys[hk_id] = callback
            return hk_id
        return -1

    def clear_all(self):
        """注销所有已注册热键。"""
        for hk_id in list(self.hotkeys.keys()):
            ctypes.windll.user32.UnregisterHotKey(None, hk_id)
        self.hotkeys.clear()

    def unregister_by_id(self, hk_id):
        """注销单个热键。"""
        if hk_id in self.hotkeys:
            ctypes.windll.user32.UnregisterHotKey(None, hk_id)
            del self.hotkeys[hk_id]

    def nativeEventFilter(self, eventType, message):
        """拦截系统 WM_HOTKEY 消息并分发到对应回调。"""
        msg = ctypes.wintypes.MSG.from_address(message.__int__())
        if msg.message == self.WM_HOTKEY:
            hk_id = msg.wParam
            if hk_id in self.hotkeys:
                if self.is_paused and hk_id != self.master_hk_id:
                    return True, 0
                    
                self.hotkeys[hk_id]()
                return True, 0
        return False, 0


hotkey_manager = Win32HotkeyManager()


# ---------- 开机自启 ----------

def set_autostart(enable):
    """通过注册表 HKCU\\Run 设置/取消开机自启。

    根据运行环境自动选择启动命令：
    - 打包为 EXE：直接引用 exe 路径。
    - 源码运行：用 python.exe 执行当前脚本。
    """
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        if enable:
            if getattr(sys, 'frozen', False):
                cmd = f'"{os.path.abspath(sys.executable)}"'
            else:
                cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, "AniNote", 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, "AniNote")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"设置开机自启失败: {e}")


# ---------- 主入口 ----------

def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.installNativeEventFilter(hotkey_manager)

    if getattr(sys, 'frozen', False):
        BASE_DIR = sys._MEIPASS          # PyInstaller 临时解压目录，--add-data 文件在这里
    else:
        BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
    icon_path = os.path.join(BASE_DIR, 'Newicon.ico')
    tray_icon = QSystemTrayIcon(QIcon(icon_path), app)
    tray_icon.setToolTip(f"AniNote v{note_app.VERSION}")

    cfg = note_app.load_config()
    set_autostart(cfg["autostart"])

    # 路径容灾：确保存储目录可写；失败时回退到程序同级目录。
    save_dir = cfg.get("save_dir", "default")
    if save_dir == "default":
        save_dir = note_app.SAVE_DIR

    try:
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
    except Exception as e:
        print(f"检测到非法路径或权限不足，触发自愈机制: {e}")
        save_dir = os.path.join(BASE_DIR, "notes_data")
        cfg["save_dir"] = "default"
        note_app.save_config(cfg)
        try:
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
        except Exception:
            pass

    # 将验证后的实际路径同步给核心引擎
    note_app.SAVE_DIR = save_dir

    def _find_note_file_path(note_id):
        """在 SAVE_DIR 中按 note_id 查找 data.json 路径（兼容文件夹模式）。"""
        for item in os.listdir(note_app.SAVE_DIR):
            item_path = os.path.join(note_app.SAVE_DIR, item)
            data_file = os.path.join(item_path, "data.json") if os.path.isdir(item_path) else None
            if data_file and os.path.exists(data_file):
                try:
                    with open(data_file, 'r', encoding='utf-8') as fh:
                        if json.load(fh).get("note_id") == note_id:
                            return data_file
                except (json.JSONDecodeError, OSError):
                    pass
        return None

    # 便签独立快捷键 —— 必须在加载便签之前连接信号
    _per_note_hk = {}  # note_id -> hotkey_str
    _shared_hk = {}    # hotkey_str -> (hk_id, [note_ids])

    def _on_register_note_hotkey(note_id, hotkey_str):
        """注册便签独立快捷键。同一组合键只注册一次，触发时广播到所有绑定的便签。"""
        _on_unregister_note_hotkey(note_id)
        if not hotkey_str.strip():
            return
        _per_note_hk[note_id] = hotkey_str
        key = hotkey_str.lower().strip()
        if key in _shared_hk:
            _shared_hk[key][1].append(note_id)
        else:
            def show_notes():
                for nid in _shared_hk.get(key, (0, []))[1]:
                    for note in note_app.ACTIVE_NOTES:
                        if note.note_id == nid:
                            if note.isHidden():
                                note.show()
                            note.raise_()
                            note.activateWindow()
                            break
                    else:
                        open_note_by_id(nid)
            hk_id = hotkey_manager.register(hotkey_str, show_notes)
            if hk_id >= 0:
                _shared_hk[key] = (hk_id, [note_id])

    def _on_unregister_note_hotkey(note_id):
        """注销便签独立快捷键。"""
        hotkey_str = _per_note_hk.pop(note_id, None)
        if not hotkey_str:
            return
        key = hotkey_str.lower().strip()
        if key in _shared_hk:
            hk_id, note_ids = _shared_hk[key]
            if note_id in note_ids:
                note_ids.remove(note_id)
            if not note_ids:
                hotkey_manager.unregister_by_id(hk_id)
                del _shared_hk[key]

    def _on_check_hotkey_conflict(hotkey_str, callback):
        """检查快捷键是否被其他便签占用。callback(conflict_names_list)"""
        conflicts = []
        key = hotkey_str.lower().strip()
        for nid, hk in _per_note_hk.items():
            if hk.lower().strip() != key:
                continue
            fpath = _find_note_file_path(nid)
            if fpath:
                try:
                    with open(fpath, 'r', encoding='utf-8') as fh:
                        jdata = json.load(fh)
                    conflicts.append(jdata.get("title", ""))
                except:
                    pass
        callback(conflicts)

    note_app.global_signaler.register_note_hotkey.connect(_on_register_note_hotkey)
    note_app.global_signaler.unregister_note_hotkey.connect(_on_unregister_note_hotkey)
    note_app.global_signaler.check_hotkey_conflict.connect(_on_check_hotkey_conflict)

    # 迁移旧版单文件到文件夹
    note_app.migrate_legacy_notes()

    # 收集所有便签（文件夹内的 data.json）
    files = []
    for d in os.listdir(save_dir):
        dpath = os.path.join(save_dir, d)
        if os.path.isdir(dpath) and os.path.exists(os.path.join(dpath, "data.json")):
            files.append(f"{d}/data.json")

    # 首次运行：创建欢迎便签
    if cfg.get("is_first_run", True):
        note = note_app.AniNoteWindow()
        note.resize(550, 500)
        note.header.title_edit.setText(f"欢迎使用 AniNote v{note_app.VERSION}！")
        note.text_edit.setHtml("""
        <p><span style="font-size:18px;font-weight:600;">你好，欢迎来到属于你的桌面便签！</span></p>
        <br>
        <p>这里有一份快速上手指南，看完就可以把它删掉：</p>
        <br>
        <p><b>1. 移动与缩放</b>: 按住右上角 ⋮⋮ 区域拖动便签，拖拽任意边角可自由缩放。</p>
        <p><b>2. 右键菜单</b>: 在便签上右键，可以锁定、置顶、隐藏、导出文档。</p>
        <p><b>3. 待办事项</b>: 点击顶部工具栏的待办按钮，插入可勾选的待办方块。</p>
        <p><b>4. 富文本编辑</b>: 粗体、斜体、下划线、字号、颜色、背景色，随心搭配。</p>
        <p><b>5. Markdown 便签</b>: 点击工具栏「Md」按钮，便签切换为左源码右实时渲染的 Markdown 模式，支持标题、列表、任务勾选、表格、代码块、引用等完整语法；右键「隐藏源码」可收起编辑区只保留渲染效果（便签宽度减半），锁定后自动只展示渲染。</p>
        <p><b>6. 截图与插图</b>: 工具栏剪刀按钮区域截图，图片按钮从文件插入，图片自动存入便签专属文件夹。</p>
        <p><b>7. 导出文件</b>: 工具栏下载按钮一键导出——普通便签导出 Word 文档 (.doc)，Markdown 便签导出 .md 文件；控制面板便签墙右键也可导出。</p>
        <p><b>8. 全局快捷键</b>: <b>Alt+M</b> 新建 | <b>Alt+N</b> 隐藏/显示 | <b>Alt+C</b> 控制台（可在控制面板中自定义）。</p>
        <p><b>9. 便签专属快捷键</b>: 点击工具栏齿轮图标，可为单个便签绑定独立快捷键，快速呼出。</p>
        <p><b>10. 控制面板</b>: 系统托盘右键或便签右键可打开控制台，集中管理便签墙、设置个性化选项（含「默认 Markdown 模式」开关，开启后新建便签默认进入 Markdown 模式）。</p>
        <p><b>11. 新番信息</b>: 控制面板一键授权 Bangumi，自动拉取追番日历（周循环滑动窗口、今天高亮、集数徽标）。点击番剧名可标记看过/取消（双向同步 Bangumi）；右键番剧可「在 Bangumi 打开」或打开集数标记窗口逐集勾选/一键全部看过；顶部「只看未看」过滤未看条目（大陆网络环境需自备代理）。</p>
        <p><b>12. 事务追踪器</b>: 控制面板中新建事务追踪，支持自由打卡/周期循环/倒计时三种模式，可拖拽排序。</p>
        """)
        note.save_data()
        note.show()
        cfg["is_first_run"] = False
        note_app.save_config(cfg)
    else:
        for f in files:
            fpath = os.path.join(save_dir, f)
            try:
                with open(fpath, 'r', encoding='utf-8') as fh:
                    data = json.load(fh)
                note_id = data.get("note_id", f.replace('.json', ''))
            except (json.JSONDecodeError, OSError):
                note_id = f.replace('.json', '')
            if note_id.startswith("habit_"):
                note = note_app.HabitTrackerWindow(note_id=note_id)
            elif note_id == "bangumi_schedule":
                note = note_app.BangumiScheduleWindow(note_id=note_id)
            else:
                note = note_app.AniNoteWindow(note_id=note_id)
            if getattr(note, 'is_hidden', False):
                note.hide()
            else:
                note.show()
                note.raise_()
                note.activateWindow()

    # 控制面板
    panel = cp_app.ControlPanel()
    
    def show_and_focus_panel():
        panel.refresh_notes_wall()
        if panel.isMinimized():
            panel.showNormal()
        panel.show()
        panel.raise_()
        panel.activateWindow()
        
        try:
            hwnd = int(panel.winId())
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    note_app.global_signaler.open_panel_signal.connect(show_and_focus_panel)
    
    panel.refresh_notes_wall()

    # ==========================================
    #  Bangumi 同步流程
    # ==========================================

    def apply_bangumi_data(payload):
        """将拉取结果写入/更新 Bangumi 便签（原生网格渲染）。

        payload: dict = 同步成功的番剧日程；"loading" = 加载中；str = 错误信息。
        """
        target_note = None
        for note in note_app.ACTIVE_NOTES:
            if note.note_id == "bangumi_schedule":
                # 旧版本可能残留普通便签窗口（无 _apply_schedule），需重建
                if hasattr(note, "_apply_schedule"):
                    target_note = note
                break

        if not target_note:
            # 网格/工具栏/格式面板隐藏由 BangumiScheduleWindow 自身管理
            target_note = note_app.BangumiScheduleWindow()
            target_note.resize(550, 300)
            target_note.show()

        if isinstance(payload, dict):
            target_note._apply_schedule(payload)
        elif isinstance(payload, str):
            if payload == "loading":
                target_note._show_loading()
            else:
                target_note._show_error(payload)
        target_note.save_data()
        panel.refresh_notes_wall()

    bgm_updater.update_signal.connect(apply_bangumi_data)

    def trigger_bangumi_sync(config, delay=5, force=False):
        """启动后台线程拉取 Bangumi 数据，避免阻塞 UI。

        Args:
            config: 当前配置字典。
            delay: 启动延迟（秒），避免应用启动瞬间抢占资源。
            force: True = 手动刷新/设置变更，忽略日期限制；False = 仅在日期变更时自动刷新。

        每日仅自动请求一次 API，手动刷新不受限。
        """
        if not config.get("enable_bangumi", False) or not config.get("bangumi_uid", "").strip():
            for note in note_app.ACTIVE_NOTES:
                if note.note_id == "bangumi_schedule":
                    note.delete_note(confirm=False)
                    break
            return

        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        last_sync = config.get("last_bangumi_sync", "")

        # 非强制且日期未变 → 只显示缓存，不请求 API
        if not force and last_sync == today_str:
            return

        def task():
            import time
            if delay > 0:
                time.sleep(delay)
            uid = config.get("bangumi_uid", "").strip()
            proxy_str = config.get("api_proxy", "").strip()

            # 发送加载状态提示（窗口原生网格显示）
            bgm_updater.update_signal.emit("loading")

            schedule = fetch_bangumi_data(uid, proxy_str)
            # schedule 为 dict = 成功；str = 错误诊断信息
            bgm_updater.update_signal.emit(schedule)

            # 记录本次同步日期
            config["last_bangumi_sync"] = today_str
            note_app.save_config(config)

        threading.Thread(target=task, daemon=True).start()

    # 启动时执行一次同步（日期未变则跳过 API 请求）；延迟 7 秒避开启动高峰
    trigger_bangumi_sync(cfg, delay=7)

    # ==========================================
    #  信号接线
    # ==========================================

    note_app.global_signaler.note_updated_signal.connect(panel.refresh_notes_wall)
    note_app.global_signaler.toggle_signal.connect(note_app.toggle_all_notes)
    note_app.global_signaler.new_note_signal.connect(
        lambda: (note_app.create_global_new_note(), panel.refresh_notes_wall())
    )
    note_app.global_signaler.show_all_signal.connect(note_app.show_all_notes)
    # force_sync 信号连接到同步函数（放在接线处而不是 apply_new_settings 内，避免重复绑定）
    note_app.global_signaler.force_sync_bangumi_signal.connect(
        lambda: trigger_bangumi_sync(note_app.load_config(), delay=0, force=True)
    )

    # 便签独立快捷键管理
    def open_note_by_id(note_id):
        """按 ID 显示便签；如果未实例化则创建。"""
        for note in note_app.ACTIVE_NOTES:
            if note.note_id == note_id:
                note.is_hidden = False
                note.save_data()
                note.show()
                note.activateWindow()
                return
        new_note = (note_app.HabitTrackerWindow(note_id=note_id)
                    if note_id.startswith("habit_")
                    else note_app.AniNoteWindow(note_id=note_id))
        new_note.is_hidden = False
        new_note.show()
        new_note.activateWindow()

    def delete_note_by_id(nid):
        """按 ID 删除便签（含磁盘文件）。"""
        for note in note_app.ACTIVE_NOTES:
            if note.note_id == nid:
                note.delete_note(confirm=False)
                return
        # 便签未实例化，按 note_id 查找并删除
        fpath = _find_note_file_path(nid)
        if fpath:
            import shutil
            shutil.rmtree(os.path.dirname(fpath))
        panel.refresh_notes_wall()

    def set_note_top(nid, is_top):
        """设置指定便签的置顶状态。"""
        for note in note_app.ACTIVE_NOTES:
            if note.note_id == nid:
                note.is_always_on_top = is_top
                note.save_data()
                note.hide()
                note.apply_window_states()
                note.show()
                panel.refresh_notes_wall()
                return

        # 便签未实例化，按 note_id 查找并更新
        fpath = _find_note_file_path(nid)
        if fpath:
            try:
                with open(fpath, "r", encoding="utf-8") as fi:
                    data = json.load(fi)
                data["is_always_on_top"] = is_top
                with open(fpath, "w", encoding="utf-8") as fo:
                    json.dump(data, fo, ensure_ascii=False, indent=4)
                panel.refresh_notes_wall()
            except Exception:
                pass

    def _open_note_hotkey_by_id(note_id):
        """通过控制面板触发便签快捷键设置。"""
        for note in note_app.ACTIVE_NOTES:
            if note.note_id == note_id:
                note._open_note_hotkey_dialog()
                return
        # 便签未实例化，先创建再打开
        new_note = note_app.AniNoteWindow(note_id=note_id)
        new_note.show()
        new_note._open_note_hotkey_dialog()

    def export_note_by_id(nid):
        """将指定便签导出为 Word 文档（.doc），保留全部富文本格式。

        实际上是一个完整的 HTML 文件以 .doc 扩展名保存，
        Word / WPS 可直接打开，加粗/斜体/颜色/字号/待办项全部保留。
        """
        try:
            file_path = _find_note_file_path(nid)
            if not file_path:
                QMessageBox.warning(panel, "导出失败", "未��到该便签的数据文件。")
                return

            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            title = data.get("title", "未命名便签").strip()
            html_content = data.get("html_content", "")

            if not html_content.strip():
                QMessageBox.information(panel, "导出", "该便签没有文字内容，跳过导出。")
                return

            # 包装为完整 HTML，Word/WPS 用文档模式打开
            full_doc = f"""<html xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:w="urn:schemas-microsoft-com:office:word"
 xmlns="http://www.w3.org/TR/REC-html40">
<head><meta charset="utf-8"><title>{title}</title></head>
<body>{html_content}</body></html>"""

            # 解析导出目录
            cfg = note_app.load_config()
            export_raw = cfg.get("export_dir", "default")
            if export_raw == "default" or not export_raw:
                export_dir = note_app.EXPORT_DIR
            else:
                export_dir = export_raw

            os.makedirs(export_dir, exist_ok=True)

            trans = str.maketrans({
                '/': '／', '\\': '＼', ':': '：',
                '*': '＊', '?': '？', '"': '＂',
                '<': '＜', '>': '＞', '|': '｜',
            })
            safe_title = title.translate(trans)
            doc_path = os.path.join(export_dir, f"{safe_title}.doc")

            with open(doc_path, "w", encoding="utf-8") as f:
                f.write(full_doc)
            QMessageBox.information(panel, "导出成功", f"已导出至：\n{doc_path}")
        except Exception as e:
            QMessageBox.warning(panel, "导出失败", str(e))

    def export_note_by_id_md(nid):
        """将指定便签导出为 Markdown (.md) 文件。

        MD 便签直接导出源码 content_md；普通便签将富文本 HTML 转为
        Markdown（标题/列表/粗体/待办/图片尽力保真）。图片相对路径
        转为 file:// 绝对路径，保证在任意 Markdown 阅读器中可显示。
        """
        try:
            file_path = _find_note_file_path(nid)
            if not file_path:
                QMessageBox.warning(panel, "导出失败", "未找到该便签的数据文件。")
                return

            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            title = data.get("title", "未命名便签").strip()
            note_dir = os.path.dirname(file_path).replace('\\', '/')

            if data.get("markdown"):
                md_text = data.get("content_md", "")
            else:
                from PySide6.QtGui import QTextDocument
                from markdown_conv import doc_to_markdown
                doc = QTextDocument()
                doc.setHtml(data.get("html_content", ""))
                md_text = doc_to_markdown(doc, note_dir)

            if not md_text.strip():
                QMessageBox.information(panel, "导出", "该便签没有文字内容，跳过导出。")
                return

            # 图片相对路径 → file:// 绝对（阅读器打开 .md 时仍可显示图片）
            def _abs_src(m):
                rel = m.group(2)
                if rel.startswith(("http://", "https://", "file:", "data:")):
                    return m.group(0)
                return f"![{m.group(1)}](file:///{note_dir}/{rel})"

            md_text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _abs_src, md_text)

            # 解析导出目录
            cfg = note_app.load_config()
            export_raw = cfg.get("export_dir", "default")
            if export_raw == "default" or not export_raw:
                export_dir = note_app.EXPORT_DIR
            else:
                export_dir = export_raw

            os.makedirs(export_dir, exist_ok=True)

            trans = str.maketrans({
                '/': '／', '\\': '＼', ':': '：',
                '*': '＊', '?': '？', '"': '＂',
                '<': '＜', '>': '＞', '|': '｜',
            })
            safe_title = title.translate(trans)
            md_path = os.path.join(export_dir, f"{safe_title}.md")

            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md_text)
            QMessageBox.information(panel, "导出成功", f"已导出至：\n{md_path}")
        except Exception as e:
            QMessageBox.warning(panel, "导出失败", str(e))

    panel.request_open_note.connect(open_note_by_id)
    panel.request_new_note.connect(note_app.create_global_new_note)
    panel.request_new_habit.connect(note_app.create_global_new_habit)
    panel.request_delete_note.connect(delete_note_by_id)
    panel.request_set_top.connect(set_note_top)
    panel.request_export_note.connect(export_note_by_id)
    panel.request_export_md_note.connect(export_note_by_id_md)
    panel.request_set_note_hotkey.connect(_open_note_hotkey_by_id)
    panel.request_check_update.connect(lambda: check_update_flow(manual=True))

    # ==========================================
    #  自动更新
    # ==========================================

    def show_update_dialog(info):
        """弹出「发现新版本」对话框，返回 True=用户选择立即更新。"""
        dlg = QDialog(panel)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setWindowTitle("发现新版本")
        dlg.setFixedSize(520, 460)
        dlg.setStyleSheet("QDialog { background: #FAFAFA; }")
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        header = QLabel(
            f"<span style='font-size:16px;font-weight:600;color:#1A1A1A;'>"
            f"发现新版本 v{info['latest_version']}</span>"
            f"<span style='font-size:13px;color:#999;margin-left:10px;'>"
            f"当前 v{note_app.VERSION}</span>"
        )
        layout.addWidget(header)

        notes = QPlainTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(info.get("notes", "（本次更新没有附更新日志）"))
        notes.setStyleSheet(
            "QPlainTextEdit { border: 1px solid #E0E0E0; border-radius: 8px;"
            " background: #FFFFFF; font-size: 13px; padding: 10px; }"
        )
        layout.addWidget(notes, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_later = QPushButton("稍后")
        btn_later.setStyleSheet(
            "QPushButton { padding: 8px 20px; border: 1px solid #D0D0D0;"
            " border-radius: 8px; background: #FFFFFF; font-size: 13px; color: #555; }"
            " QPushButton:hover { background: #F0F0F0; }"
        )
        btn_later.clicked.connect(dlg.reject)
        btn_row.addWidget(btn_later)

        btn_ignore = QPushButton("忽略此版本")
        btn_ignore.setStyleSheet(
            "QPushButton { padding: 8px 20px; border: none; border-radius: 8px;"
            " background: transparent; font-size: 13px; color: #999; }"
            " QPushButton:hover { color: #555; }"
        )
        btn_ignore.clicked.connect(lambda: (_ignore_version(info["latest_version"]), dlg.reject()))
        btn_row.addWidget(btn_ignore)

        btn_update = QPushButton("立即更新")
        btn_update.setStyleSheet(
            "QPushButton { padding: 8px 28px; border: none; border-radius: 8px;"
            " background: #1A73E8; font-size: 13px; color: #FFFFFF; font-weight: 600; }"
            " QPushButton:hover { background: #1765CC; }"
        )
        btn_update.clicked.connect(dlg.accept)
        btn_row.addStretch()
        btn_row.addWidget(btn_update)
        layout.addLayout(btn_row)

        return dlg.exec() == QDialog.Accepted

    def _ignore_version(version):
        cfg = note_app.load_config()
        cfg["ignored_version"] = version
        note_app.save_config(cfg)

    def perform_update(info):
        """下载新版本并触发替换。"""
        # 下载进度对话框
        dlg = QDialog(panel)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setWindowTitle("正在下载更新")
        dlg.setFixedSize(420, 150)
        dlg.setStyleSheet("QDialog { background: #FAFAFA; }")
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        lbl = QLabel(f"正在下载 v{info['latest_version']}…")
        lbl.setStyleSheet("font-size: 14px; color: #333;")
        layout.addWidget(lbl)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setStyleSheet(
            "QProgressBar { border: none; border-radius: 5px; background: #E0E0E0;"
            " height: 10px; text-align: center; font-size: 10px; color: transparent; }"
            " QProgressBar::chunk { background: #1A73E8; border-radius: 5px; }"
        )
        layout.addWidget(bar)
        status = QLabel("连接服务器中…")
        status.setStyleSheet("font-size: 12px; color: #999;")
        layout.addWidget(status)
        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet(
            "QPushButton { padding: 6px 18px; border: 1px solid #D0D0D0; border-radius: 6px;"
            " background: #FFFFFF; font-size: 12px; color: #555; }"
            " QPushButton:hover { background: #F0F0F0; }"
        )
        cancel_btn.clicked.connect(dlg.reject)
        layout.addWidget(cancel_btn, 0, Qt.AlignRight)
        cancel_flag = {"cancelled": False}
        cancel_btn.clicked.connect(lambda: cancel_flag.__setitem__("cancelled", True))

        # 跨线程信号桥：下载线程 → 主线程 UI（须在 dlg 创建之后连接）
        class _UpdateBridge(QObject):
            download_ready = Signal(str)   # 携带 update.bat 路径
            failed = Signal(str)
            cancelled = Signal()
            progress = Signal(int, int)    # (received, total)
        bridge = _UpdateBridge()

        def _on_failed(msg):
            QMessageBox.warning(None, "更新失败", msg)
            dlg.reject()   # 关闭下载进度对话框

        def _update_progress(received, total):
            if cancel_flag["cancelled"]:
                return
            pct = int(received * 100 / total) if total else 0
            bar.setValue(pct)
            if total:
                status.setText(f"已下载 {received // 1024} KB / {total // 1024} KB")
            else:
                status.setText(f"已下载 {received // 1024} KB")

        def _on_download_ready(bat_path):
            dlg.accept()
            # 主线程中调度收尾：保存便签 → 隐藏托盘 → 启动替换脚本 → 退出
            QTimer.singleShot(200, lambda: (
                [n.save_data() for n in note_app.ACTIVE_NOTES],
                tray_icon.hide(),
                subprocess.Popen([bat_path], shell=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW),
                app.quit(),
            ))

        bridge.failed.connect(_on_failed)
        bridge.cancelled.connect(dlg.reject)
        bridge.progress.connect(_update_progress)
        bridge.download_ready.connect(_on_download_ready)

        def worker():
            download_dir = updater.temp_download_dir()
            zip_path = os.path.join(download_dir, f"AniNote-v{info['latest_version']}.zip")

            def progress_cb(received, total):
                if not cancel_flag["cancelled"]:
                    bridge.progress.emit(received, total)

            ok, fail_reason = updater.download_update(
                info["zip_url"], zip_path,
                progress_cb=progress_cb,
                proxy_str=note_app.load_config().get("api_proxy", ""),
                sha256=info.get("sha256", ""))
            if cancel_flag["cancelled"]:
                bridge.cancelled.emit()
                return
            if not ok:
                if fail_reason == "checksum":
                    bridge.failed.emit(
                        "文件下载完成但校验失败（文件可能被篡改，或版本清单中的"
                        "校验值不正确）。\n\n请稍后重试，或联系作者检查版本清单。"
                    )
                else:
                    log_path = os.path.join(os.path.dirname(zip_path), "download_error.log")
                    bridge.failed.emit(
                        "下载失败，请检查网络连接后重试。\n\n"
                        "大陆网络环境下建议在「控制面板 → API 代理地址」"
                        "中填写本地代理端口后再试。\n\n"
                        f"详细原因见：{log_path}"
                    )
                return
            # 写入更新标记（供新版本展示日志）并生成替换脚本
            updater.mark_updated(note_app.VERSION, info["latest_version"], info.get("notes", ""))
            bat_path = updater.write_update_script(updater.app_base_dir(), zip_path)
            bridge.download_ready.emit(bat_path)

        threading.Thread(target=worker, daemon=True).start()
        if dlg.exec() != QDialog.Accepted and not cancel_flag["cancelled"]:
            return
        # 更新脚本已启动，主程序即将退出

    # 更新检查并发控制
    # - token：手动检查时递增，进行中/排队中的旧检查结果作废（打断自动检查）
    # - busy：一次只允许一个检查在网络上跑
    # - dialog_open：更新弹窗已打开时忽略新检查，避免重复弹窗
    check_ctl = {"token": 0, "busy": False, "dialog_open": False, "timer": None,
                 "manual": False}

    class _CheckBridge(QObject):
        """后台检查线程 → 主线程的结果信号桥（跨线程自动 Queued 执行）。"""
        done = Signal(object, int)   # (info, token)

    def _on_check_done(info, token):
        check_ctl["busy"] = False
        if token != check_ctl["token"]:
            return  # 已被更新的手动检查作废
        manual = check_ctl["manual"]
        if check_ctl["dialog_open"]:
            return
        if info is None:
            if manual:
                QMessageBox.information(
                    None, "检查更新",
                    "无法获取版本信息，请检查网络或代理设置后重试。"
                )
            return
        if updater.compare_versions(info["latest_version"], note_app.VERSION) <= 0:
            if manual:
                QMessageBox.information(None, "检查更新", f"当前已是最新版本 v{note_app.VERSION}。")
            return
        cfg = note_app.load_config()
        if info["latest_version"] == cfg.get("ignored_version", ""):
            return
        check_ctl["dialog_open"] = True
        try:
            if show_update_dialog(info):
                perform_update(info)
        finally:
            check_ctl["dialog_open"] = False

    check_bridge = _CheckBridge()
    check_bridge.done.connect(_on_check_done)

    def check_update_flow(manual=False):
        """发起一次更新检查（网络请求在后台线程，不阻塞 UI）。

        manual=True（用户点击）：作废任何进行中的自动检查，立即重新检查。
        Returns: True=已发起检查；False=未发起（已有弹窗打开等）。
        """
        if check_ctl["dialog_open"]:
            return False
        if manual:
            # 手动检查打断自动：取消重试定时器 + 作废进行中检查的结果
            check_ctl["token"] += 1
            if check_ctl["timer"]:
                check_ctl["timer"].stop()
                check_ctl["timer"] = None
        elif check_ctl["busy"]:
            return False
        token = check_ctl["token"]
        check_ctl["busy"] = True
        check_ctl["manual"] = manual
        proxy = note_app.load_config().get("api_proxy", "")

        def worker():
            info = updater.check_for_update(proxy_str=proxy)
            check_bridge.done.emit(info, token)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def check_update_auto(retries=2):
        """启动后自动检查更新；失败则延迟 60 秒重试（最多 retries 次），
        应对启动初期网络/代理尚未就绪的情况。"""
        cfg = note_app.load_config()
        if not cfg.get("auto_update", True):
            return
        ok = check_update_flow(manual=False)
        if not ok and retries > 0:
            t = QTimer()
            t.setSingleShot(True)
            t.timeout.connect(lambda: check_update_auto(retries - 1))
            t.start(60000)
            check_ctl["timer"] = t

    def show_updated_log():
        """更新完成后首次启动，展示本次更新日志。"""
        mark = updater.read_update_mark()
        if not mark:
            return
        dlg = QDialog(panel)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setWindowTitle("更新完成")
        dlg.setFixedSize(520, 440)
        dlg.setStyleSheet("QDialog { background: #FAFAFA; }")
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        header = QLabel(
            f"<span style='font-size:16px;font-weight:600;color:#1A1A1A;'>"
            f"已更新到 v{mark.get('to', '')}</span>"
            f"<span style='font-size:13px;color:#999;margin-left:10px;'>"
            f"从 v{mark.get('from', '')} 升级</span>"
        )
        layout.addWidget(header)
        notes = QPlainTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(mark.get("notes", "") or "（本次更新没有附更新日志）")
        notes.setStyleSheet(
            "QPlainTextEdit { border: 1px solid #E0E0E0; border-radius: 8px;"
            " background: #FFFFFF; font-size: 13px; padding: 10px; }"
        )
        layout.addWidget(notes, 1)
        ok_btn = QPushButton("知道了")
        ok_btn.setStyleSheet(
            "QPushButton { padding: 8px 30px; border: none; border-radius: 8px;"
            " background: #1A73E8; font-size: 13px; color: #FFFFFF; font-weight: 600; }"
            " QPushButton:hover { background: #1765CC; }"
        )
        ok_btn.clicked.connect(dlg.accept)
        layout.addWidget(ok_btn, 0, Qt.AlignRight)
        dlg.exec()

    # ==========================================
    #  系统托盘菜单
    # ==========================================

    menu = QMenu()
    menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WA_TranslucentBackground)
    menu.setStyleSheet(
        "QMenu {"
        " background-color: #FAFAFA;"
        " border: 1px solid #E0E0E0;"
        " border-radius: 10px;"
        " padding: 6px;"
        " font-family: 'Microsoft YaHei';"
        " }"
        " QMenu::item {"
        " padding: 7px 24px;"
        " border-radius: 6px;"
        " margin: 1px 3px;"
        " color: #333333;"
        " font-size: 13px;"
        " }"
        " QMenu::item:selected {"
        " background-color: #E8F0FE;"
        " color: #1A73E8;"
        " }"
        " QMenu::separator {"
        " height: 1px;"
        " background: #E8E8E8;"
        " margin: 4px 12px;"
        " }"
    )

    action_new = QAction(
        f"新建便签 ({cfg['new_hotkey'].upper()})", app
    )
    action_new.triggered.connect(lambda: note_app.global_signaler.new_note_signal.emit())

    action_toggle = QAction(
        f"隐藏/显示所有便签 ({cfg['toggle_hotkey'].upper()})", app
    )
    action_toggle.triggered.connect(lambda: note_app.global_signaler.toggle_signal.emit())

    action_panel = QAction(
        f"打开总控制台 ({cfg.get('panel_hotkey', 'alt+c').upper()})", app
    )
    action_panel.triggered.connect(
        show_and_focus_panel)

    action_check_update = QAction("检查更新", app)
    action_check_update.triggered.connect(lambda: check_update_flow(manual=True))

    action_exit = QAction("彻底退出 AniNote", app)
    action_exit.triggered.connect(lambda: (
        [note.save_data() for note in note_app.ACTIVE_NOTES],
        tray_icon.hide(),
        app.quit(),
    ))

    menu.addAction(action_new)
    menu.addAction(action_toggle)
    menu.addSeparator()
    menu.addAction(action_panel)
    menu.addSeparator()
    menu.addAction(action_check_update)
    menu.addSeparator()
    menu.addAction(action_exit)
    tray_icon.setContextMenu(menu)

    def on_tray_activated(reason):
        """托盘图标双击：切换控制面板显示。"""
        if reason == QSystemTrayIcon.DoubleClick:
            if panel.isVisible() and panel.isActiveWindow():
                panel.hide()
            else:
                show_and_focus_panel()

    tray_icon.activated.connect(on_tray_activated)
    tray_icon.show()

    # ==========================================
    #  热键与设置绑定
    # ==========================================

    def bind_hotkeys(config):
        """根据配置重新注册全局热键。"""
        hotkey_manager.clear_all()
        hotkey_manager.is_paused = False # 重新绑定时重置为非禁用状态
        hotkey_manager.master_hk_id = None
        
        try:
            hk_toggle = config["toggle_hotkey"].strip()
            hk_new = config["new_hotkey"].strip()
            hk_show_all = config.get("show_all_hotkey", "alt+shift+n").strip()
            hk_disable = config.get("disable_all_hotkey", "ctrl+shift+a").strip()
            hk_panel = config.get("panel_hotkey", "alt+c").strip()

            if hk_toggle:
                hotkey_manager.register(hk_toggle, lambda: note_app.global_signaler.toggle_signal.emit())
            if hk_new:
                hotkey_manager.register(hk_new, lambda: note_app.global_signaler.new_note_signal.emit())
            if hk_show_all:
                hotkey_manager.register(hk_show_all, lambda: note_app.global_signaler.show_all_signal.emit())
            if hk_panel:
                hotkey_manager.register(hk_panel, show_and_focus_panel)
            #注册主控开关
            if hk_disable:
                def toggle_pause():
                    hotkey_manager.is_paused = not hotkey_manager.is_paused
                    # 动态改变提示语
                    state_msg = "已临时禁用" if hotkey_manager.is_paused else "已恢复"
                    # 调用系统托盘弹出气泡通知，显示 2000 毫秒
                    tray_icon.showMessage("AniNote 快捷键", f"全局快捷键{state_msg}", QSystemTrayIcon.Information, 2000)

                # 注册前先记录当前的 ID 分配，赋予它免疫拦截的特权
                hotkey_manager.master_hk_id = hotkey_manager.hk_id_counter
                hotkey_manager.register(hk_disable, toggle_pause)

        except Exception as e:
            print(f"安全热键注册失败: {e}")
    bind_hotkeys(cfg)

    def apply_new_settings(new_cfg):
        """设置保存后应用所有变更。"""
        set_autostart(new_cfg["autostart"])
        bind_hotkeys(new_cfg)

        # 更新所有便签的字体
        for note in note_app.ACTIVE_NOTES:
            note.text_edit.setStyleSheet(
                f"QTextEdit {{ border: none; background: transparent; font-size: 18px; "
                f"font-family: '{new_cfg['font_family']}'; color: #333333; }}"
            )

        action_new.setText(f"新建便签 ({new_cfg['new_hotkey'].upper()})")
        action_toggle.setText(
            f"隐藏/显示所有便签 ({new_cfg['toggle_hotkey'].upper()})"
        )
        action_panel.setText(
            f"打开总控制台 ({new_cfg.get('panel_hotkey', 'alt+c').upper()})"
        )

        # 立即触发一次同步，延迟为 0（用户主动操作）
        trigger_bangumi_sync(new_cfg, delay=0, force=True)
        note_app.global_signaler.config_changed_signal.emit()

    panel.settings_changed.connect(apply_new_settings)

    # 更新完成后首次启动：展示更新日志
    show_updated_log()

    # 延迟自动检查更新（等界面就绪，避免启动卡顿）
    QTimer.singleShot(5000, check_update_auto)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
