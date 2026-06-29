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
import datetime
from ctypes import wintypes

from PySide6.QtCore import Qt, QObject, Signal, QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QMessageBox
from PySide6.QtGui import QIcon, QAction

import main as note_app
import control_panel as cp_app


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
            return

        hk_id = self.hk_id_counter
        self.hk_id_counter += 1
        success = ctypes.windll.user32.RegisterHotKey(None, hk_id, modifiers, vk)
        if success:
            self.hotkeys[hk_id] = callback

    def clear_all(self):
        """注销所有已注册热键。"""
        for hk_id in self.hotkeys.keys():
            ctypes.windll.user32.UnregisterHotKey(None, hk_id)
        self.hotkeys.clear()

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
    files = [f for f in os.listdir(save_dir) if f.endswith('.json')]

    # 首次运行：创建欢迎便签
    if cfg.get("is_first_run", True):
        note = note_app.AniNoteWindow()
        note.resize(550, 500)
        note.header.title_edit.setText(f"🎉 欢迎使用 AniNote v{note_app.VERSION}！")
        note.text_edit.setHtml("""
        <p><b>👋 你好，欢迎来到属于你的桌面便签！</b></p>
        <p>这里有一份快速上手指南，看完就可以把它删掉：</p>
        <br>
        <p>1. <b>移动与排版</b>: 按住右上角的 <span style="color: #aaa;"><b>⋮⋮</b></span> 及其右侧区域可以拖动；拖动右下角可以缩放。</p>
        <p>2. <b>右键菜单</b>: 在便签上右键，可以【锁定】或【隐藏】。锁定状态下无法对便签进行操作</p>
        <p>3. <b>待办事项</b>: 点击顶部的 ☑，试试看点击下面这个方块：</p>
        <p>☐ 这是一个待办事项，点我就可以打勾</p>
        <p>4. <b>快捷呼唤</b>: 随时按 <b>Alt+M</b> 新建，按 <b>Alt+N</b> 隐藏（快捷键可更改）。</p>
        <p>5. <b>皮肤更换</b>: 暂未上线，在遥远的未来或许有更改成二次元风格的功能。</p>
        <p>6. <b>控制面板</b>: 系统任务栏里或者便签右键可以打开控制面板，探索更多功能。</p>
        <p>7. <b>新番信息</b>: 控制面板中开启并绑定Bangumi UID即可监测自己账户的新番更新, 在大陆网络环境需自备梯子</p>
        <p>8. <b>事务追踪器</b>: 控制面板中右上角“新建事务追踪”即可创建该便签</p>
        """)
        note.show()
        note.save_data()
        cfg["is_first_run"] = False
        note_app.save_config(cfg)
    else:
        for f in files:
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

    def open_note_by_id(note_id):
        """按 ID 显示便签；如果未实例化则创建。"""
        for note in note_app.ACTIVE_NOTES:
            if note.note_id == note_id:
                note.show()
                note.activateWindow()
                return
        new_note = (note_app.HabitTrackerWindow(note_id=note_id)
                    if note_id.startswith("habit_")
                    else note_app.AniNoteWindow(note_id=note_id))
        new_note.show()
        new_note.activateWindow()

    def delete_note_by_id(nid):
        """按 ID 删除便签（含磁盘文件）。"""
        for note in note_app.ACTIVE_NOTES:
            if note.note_id == nid:
                note.delete_note(confirm=False)
                return
        # 便签未实例化，直接删磁盘文件
        file_path = os.path.join(note_app.SAVE_DIR, f"{nid}.json")
        if os.path.exists(file_path):
            os.remove(file_path)
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

        # 便签未实例化，直接修改磁盘文件
        file_path = os.path.join(note_app.SAVE_DIR, f"{nid}.json")
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["is_always_on_top"] = is_top
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                panel.refresh_notes_wall()
            except Exception:
                pass

    def export_note_by_id(nid):
        """将指定便签导出为 Word 文档（.doc），保留全部富文本格式。

        实际上是一个完整的 HTML 文件以 .doc 扩展名保存，
        Word / WPS 可直接打开，加粗/斜体/颜色/字号/待办项全部保留。
        """
        try:
            file_path = os.path.join(note_app.SAVE_DIR, f"{nid}.json")
            if not os.path.exists(file_path):
                QMessageBox.warning(panel, "导出失败", "未找到该便签的数据文件。")
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

    # ==========================================
    #  系统托盘菜单
    # ==========================================

    menu = QMenu()
    menu.setAttribute(Qt.WA_TranslucentBackground)
    menu.setStyleSheet(
        "QMenu {"
        " background-color: #FFFFFF;"
        " border: 1px solid #DDDDDD;"
        " border-radius: 6px;"
        " padding: 3px;"
        " font-family: 'Microsoft YaHei';"
        " }"
        " QMenu::item {"
        " padding: 5px 22px;"
        " border-radius: 3px;"
        " margin: 1px 2px;"
        " color: #333333;"
        " font-size: 13px;"
        " }"
        " QMenu::item:selected {"
        " background-color: #E8F4FD;"
        " color: #0078D7;"
        " }"
        " QMenu::separator {"
        " height: 1px;"
        " background: #EEEEEE;"
        " margin: 2px 8px;"
        " }"
    )

    action_new = QAction(
        f"➕ 新建便签 ({cfg['new_hotkey'].upper()})", app
    )
    action_new.triggered.connect(lambda: note_app.global_signaler.new_note_signal.emit())

    action_toggle = QAction(
        f"👁️‍🗨️ 隐藏/显示所有便签 ({cfg['toggle_hotkey'].upper()})", app
    )
    action_toggle.triggered.connect(lambda: note_app.global_signaler.toggle_signal.emit())

    action_panel = QAction("⚙️ 打开总控制台", app)
    action_panel.triggered.connect(
        show_and_focus_panel)

    action_exit = QAction("❌ 彻底退出 AniNote", app)
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

            if hk_toggle:
                hotkey_manager.register(hk_toggle, lambda: note_app.global_signaler.toggle_signal.emit())
            if hk_new:
                hotkey_manager.register(hk_new, lambda: note_app.global_signaler.new_note_signal.emit())
            if hk_show_all:
                hotkey_manager.register(hk_show_all, lambda: note_app.global_signaler.show_all_signal.emit())
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

        action_new.setText(f"➕ 新建便签 ({new_cfg['new_hotkey'].upper()})")
        action_toggle.setText(
            f"👁️‍🗨️ 隐藏/显示所有便签 ({new_cfg['toggle_hotkey'].upper()})"
        )

        # 立即触发一次同步，延迟为 0（用户主动操作）
        trigger_bangumi_sync(new_cfg, delay=0, force=True)
        note_app.global_signaler.config_changed_signal.emit()

    panel.settings_changed.connect(apply_new_settings)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
