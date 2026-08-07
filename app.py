"""
AniNote 应用入口 — 系统托盘、全局热键、Bangumi 新番同步、自启管理。
"""

import sys
import os
import json
import winreg
import requests
import ctypes
import threading
import subprocess
import datetime
from ctypes import wintypes

from PySide6.QtCore import Qt, QObject, Signal, QAbstractNativeEventFilter, QTimer, QMetaObject
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QMessageBox,
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QProgressBar,
)
from PySide6.QtGui import QIcon, QAction

import main as note_app
import control_panel as cp_app
import updater


# ==========================================
#  Bangumi 数据引擎
# ==========================================

class BangumiUpdater(QObject):
    """跨线程信号中转器，用于将网络请求结果推回主线程。"""
    update_signal = Signal(str)


bgm_updater = BangumiUpdater()


def fetch_bangumi_data(uid, proxy_str=""):
    """从 Bangumi API 拉取用户的在追新番日历。

    Args:
        uid: Bangumi 用户 ID。
        proxy_str: 代理地址，格式如 \"127.0.0.1:7890\"，留空表示直连。

    Returns:
        dict: 按星期索引的番剧名列表，{0~6: [name, ...]}；
              失败时返回错误描述字符串。
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

        # 获取「在看」列表
        url = f"https://api.bgm.tv/v0/users/{uid}/collections?subject_type=2&type=3&limit=100"
        res_coll = requests.get(url, headers=headers, proxies=proxies, timeout=15)

        if res_coll.status_code != 200:
            snippet = res_coll.text[:150].replace('<', '&lt;').replace('>', '&gt;')
            return (
                f"获取在看列表被拒！<br>"
                f"状态码: {res_coll.status_code}<br>"
                f"服务器返回: {snippet}"
            )

        watching_ids = {item['subject_id'] for item in res_coll.json().get('data', [])}
        if not watching_ids:
            return {}

        # 获取日历
        res_cal = requests.get(
            "https://api.bgm.tv/calendar",
            headers=headers, proxies=proxies, timeout=15
        )
        if res_cal.status_code != 200:
            return f"获取新番日历被拒！<br>状态码: {res_cal.status_code}"

        calendar_data = res_cal.json()
        schedule = {i: [] for i in range(7)}

        for day_data in calendar_data:
            weekday_idx = day_data['weekday']['id'] - 1
            for item in day_data.get('items', []):
                if item['id'] in watching_ids:
                    name = item.get('name_cn') or item.get('name') or "未知番剧"
                    schedule[weekday_idx].append(name)

        return schedule
    except Exception as e:
        return (
            f"底层网络异常！<br>"
            f"请检查代理端口是否填写正确。<br>"
            f"错误详情: {str(e)}"
        )


def generate_schedule_html(schedule):
    """将番剧日程字典渲染为 HTML 表格。

    Args:
        schedule: fetch_bangumi_data 的返回值（dict / str / None）。

    Returns:
        str: 可直接 setHtml 的 HTML 片段。
    """
    if schedule is None:
        return "<p style='color:#e74c3c; padding:10px;'>❌ 拉取数据失败，请检查 UID 是否正确或网络连接。</p>"
    if isinstance(schedule, str):
        return f"<p style='color:#e74c3c; padding:15px; font-size: 14px; line-height: 1.5;'><b>⚠️ 诊断信息：</b><br>{schedule}</p>"
    if not schedule:
        return "<p style='color:#666; padding:10px;'>你目前在 Bangumi 上还没有标记「在看」的番剧哦~</p>"

    today_idx = datetime.datetime.now().weekday()
    yesterday_idx = (today_idx - 1) % 7
    tomorrow_idx = (today_idx + 1) % 7

    yesterday_list = schedule.get(yesterday_idx, [])
    today_list = schedule.get(today_idx, [])
    tomorrow_list = schedule.get(tomorrow_idx, [])

    max_rows = max(len(yesterday_list), len(today_list), len(tomorrow_list))
    if max_rows == 0:
        return "<p style='color:#666; padding:10px;'>最近三天都没有你追的番剧更新，好好休息一下吧~</p>"

    html = f"""
    <p style='font-size: 14px; color: #666; text-align: center; margin-bottom: 15px; margin-top: 5px;'>
        信息来源：<a href='https://bgm.tv/calendar' style='color: #0078D7; text-decoration: none;'>Bangumi</a>
    </p>
    <table style='width: 100%; margin: 0 auto; border-collapse: collapse;
                  font-size: 15px; table-layout: fixed;'>
        <tr style='background-color: #E6F2FF; border-bottom: 2px solid #b3d7ff;'>
            <th style='padding: 10px 8px; text-align: center; width: 33.33%; color: #666;'>昨日更新</th>
            <th style='padding: 10px 8px; text-align: center; width: 33.33%; color: #e67e22;'>今日更新</th>
            <th style='padding: 10px 8px; text-align: center; width: 33.33%; color: #666;'>明日更新</th>
        </tr>
    """
    for i in range(max_rows):
        y_name = yesterday_list[i] if i < len(yesterday_list) else ""
        t_name = today_list[i] if i < len(today_list) else ""
        m_name = tomorrow_list[i] if i < len(tomorrow_list) else ""

        html += f"""
            <tr style='border-bottom: 1px dashed #ccc;'>
                <td style='padding: 8px; text-align: center; color: #555;
                           word-break: break-all; overflow-wrap: break-word;'>{y_name}</td>
                <td style='padding: 8px; text-align: center; color: #d35400; font-weight: bold;
                           word-break: break-all; overflow-wrap: break-word;'>{t_name}</td>
                <td style='padding: 8px; text-align: center; color: #555;
                           word-break: break-all; overflow-wrap: break-word;'>{m_name}</td>
            </tr>
        """
    html += "</table>"
    return html


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

    # 静默 libpng iCCP 色彩配置警告（外部 PNG 常见，无害但噪音大）
    from PySide6.QtCore import qInstallMessageHandler, QtMsgType

    def _msg_handler(mode, context, message):
        if "libpng" in message and "iCCP" in message:
            return
        print(f"{message}", file=sys.stderr)

    qInstallMessageHandler(_msg_handler)

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
        <p><b>2. 右键菜单</b>: 在便签上右键，可以锁定、置顶、隐藏、导出 Word 文档。</p>
        <p><b>3. 待办事项</b>: 点击顶部工具栏的待办按钮，插入可勾选的待办方块。</p>
        <p><b>4. 富文本编辑</b>: 粗体、斜体、下划线、字号、颜色、背景色，随心搭配。</p>
        <p><b>5. 截图与插图</b>: 工具栏剪刀按钮区域截图，图片按钮从文件插入，图片自动存入便签专属文件夹。</p>
        <p><b>6. 全局快捷键</b>: <b>Alt+M</b> 新建 | <b>Alt+N</b> 隐藏/显示 | <b>Alt+C</b> 控制台（可在控制面板中自定义）。</p>
        <p><b>7. 便签专属快捷键</b>: 点击工具栏齿轮图标，可为单个便签绑定独立快捷键，快速呼出。</p>
        <p><b>8. 控制面板</b>: 系统托盘右键或便签右键可打开控制台，集中管理便签墙、设置个性化选项。</p>
        <p><b>9. 新番信息</b>: 在控制面板中绑定 Bangumi UID，自动拉取追番日历（大陆网络环境需自备代理）。</p>
        <p><b>10. 事务追踪器</b>: 控制面板中新建事务追踪，支持自由打卡/周期循环/倒计时三种模式，可拖拽排序。</p>
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

    def apply_bangumi_html(html):
        """将生成的新番 HTML 写入/更新 Bangumi 便签。"""
        target_note = None
        for note in note_app.ACTIVE_NOTES:
            if note.note_id == "bangumi_schedule":
                target_note = note
                break

        if not target_note:
            target_note = note_app.AniNoteWindow(note_id="bangumi_schedule")
            target_note.resize(550, 300)
            target_note.show()

        now = datetime.datetime.now()
        weekdays = [
            "星期一", "星期二", "星期三",
            "星期四", "星期五", "星期六", "星期日",
        ]
        title_str = f"{now.month}月{now.day}日 {weekdays[now.weekday()]}新番更新"
        target_note.header.title_edit.setText(title_str)
        target_note.text_edit.setHtml(html)
        target_note.save_data()
        panel.refresh_notes_wall()

    bgm_updater.update_signal.connect(apply_bangumi_html)

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

            # 发送加载状态提示
            bgm_updater.update_signal.emit(
                "<div style='text-align:center; color:#999; margin-top:20px;'>"
                "<h3>⏳</h3>"
                "<p>正在跨次元连接 Bangumi...<br>拉取最新番剧数据</p></div>"
            )

            schedule = fetch_bangumi_data(uid, proxy_str)
            html_content = generate_schedule_html(schedule)
            bgm_updater.update_signal.emit(html_content)

            # 记录本次同步日期
            config["last_bangumi_sync"] = today_str
            note_app.save_config(config)

        threading.Thread(target=task, daemon=True).start()

    # 启动时执行一次同步（日期未变则跳过 API 请求）
    trigger_bangumi_sync(cfg)

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

    panel.request_open_note.connect(open_note_by_id)
    panel.request_new_note.connect(note_app.create_global_new_note)
    panel.request_new_habit.connect(note_app.create_global_new_habit)
    panel.request_delete_note.connect(delete_note_by_id)
    panel.request_set_top.connect(set_note_top)
    panel.request_export_note.connect(export_note_by_id)
    panel.request_set_note_hotkey.connect(_open_note_hotkey_by_id)
    panel.request_check_update.connect(lambda: check_update_flow(manual=True))

    # ==========================================
    #  自动更新
    # ==========================================

    def show_update_dialog(info):
        """弹出「发现新版本」对话框，返回 True=用户选择立即更新。"""
        dlg = QDialog()
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
        # 跨线程信号桥：下载线程 → 主线程 UI
        class _UpdateBridge(QObject):
            finished_ok = Signal()
            failed = Signal(str)
            cancelled = Signal()
        bridge = _UpdateBridge()
        bridge.failed.connect(lambda msg: QMessageBox.warning(None, "更新失败", msg))
        bridge.finished_ok.connect(dlg.accept)
        bridge.cancelled.connect(dlg.reject)

        # 下载进度对话框
        dlg = QDialog()
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

        dlg.show()
        dlg.setWindowModality(Qt.WindowModal)
        app.processEvents()

        def worker():
            download_dir = updater.temp_download_dir()
            zip_path = os.path.join(download_dir, f"AniNote-v{info['latest_version']}.zip")

            def progress_cb(received, total):
                if cancel_flag["cancelled"]:
                    return
                pct = int(received * 100 / total) if total else 0
                QMetaObject.invokeMethod(
                    bar, "setValue", Qt.QueuedConnection, pct
                )
                QMetaObject.invokeMethod(
                    status, "setText", Qt.QueuedConnection,
                    f"已下载 {received // 1024} KB / {total // 1024} KB"
                    if total else f"已下载 {received // 1024} KB"
                )

            ok = updater.download_update(info["zip_url"], zip_path,
                                         progress_cb=progress_cb,
                                         proxy_str=note_app.load_config().get("api_proxy", ""),
                                         sha256=info.get("sha256", ""))
            if cancel_flag["cancelled"]:
                bridge.cancelled.emit()
                return
            if not ok:
                bridge.failed.emit(
                    "下载失败，请检查网络连接后重试。\n\n"
                    "大陆网络环境下建议在「控制面板 → API 代理地址」"
                    "中填写本地代理端口后再试。"
                )
                return
            # 写入更新标记（供新版本展示日志）并生成替换脚本
            updater.mark_updated(note_app.VERSION, info["latest_version"], info.get("notes", ""))
            bat_path = updater.write_update_script(updater.app_base_dir(), zip_path)
            bridge.finished_ok.emit()
            QTimer.singleShot(200, lambda: (
                [n.save_data() for n in note_app.ACTIVE_NOTES],
                tray_icon.hide(),
                subprocess.Popen([bat_path], shell=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW),
                app.quit(),
            ))

        threading.Thread(target=worker, daemon=True).start()
        if dlg.exec() != QDialog.Accepted and not cancel_flag["cancelled"]:
            return
        # 更新脚本已启动，主程序即将退出

    def check_update_flow(manual=False):
        """检查更新；manual=True 时无论是否有新版都给出反馈。"""
        proxy = note_app.load_config().get("api_proxy", "")
        info = updater.check_for_update(proxy_str=proxy)
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
        if show_update_dialog(info):
            perform_update(info)

    def check_update_auto():
        cfg = note_app.load_config()
        if not cfg.get("auto_update", True):
            return
        check_update_flow(manual=False)

    def show_updated_log():
        """更新完成后首次启动，展示本次更新日志。"""
        mark = updater.read_update_mark()
        if not mark:
            return
        dlg = QDialog()
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
