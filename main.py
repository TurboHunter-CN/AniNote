"""
AniNote 核心引擎 — 便签窗口、富文本编辑、配置管理。

提供可拖拽、可缩放的无边框便签窗口，支持富文本编辑、待办事项切换、
全局热键通信以及 JSON 持久化存储。
"""

import sys
import json
import os
import re
import shutil
import time
import threading
import urllib.parse
import uuid
import webbrowser
import datetime as datetime_module

VERSION = "5.0.1"

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QFrame, QMenu, QGraphicsDropShadowEffect, QPushButton,
    QColorDialog, QMessageBox, QSizePolicy, QToolTip,
    QLineEdit, QTextEdit, QTextBrowser, QLabel, QDialog, QSlider, QStackedWidget,
    QFontComboBox, QSpinBox, QScrollArea, QGridLayout, QRadioButton, QDateEdit,
    QFileDialog, QDialogButtonBox, QCheckBox, QPlainTextEdit, QSplitter,
    QTimeEdit, QComboBox, QAbstractSpinBox,
)
from PySide6.QtCore import (
    Qt, QObject, Signal, QTimer, QDate, QTime, QEvent, QRect, QPoint, QSize,
    QPropertyAnimation, QEasingCurve, QVariantAnimation,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QCursor, QTextCursor, QDesktopServices, QPixmap, QImage,
    QPainter, QPen, QBrush, QSyntaxHighlighter, QTextCharFormat, QPolygon,
    QIcon,
)
from PySide6.QtSvg import QSvgRenderer

from icons import icon, set_icon_font
import note_stacks as stacks   # 便签集（便签夹）：折叠便签吸附成一条
import fonts as fonts_mod      # 字体工具：统一字体族与真实字面（字重）解析

# ---------- 路径初始化 ----------

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SAVE_DIR = os.path.join(BASE_DIR, "notes_data")
CONFIG_FILE = os.path.join(BASE_DIR, "aninote_config.json")


def app_window_icon():
    """返回应用窗口图标（Newicon）。

    兼容源码运行与 PyInstaller 打包：frozen 时 ico 被打进 _MEIPASS（--add-data），
    exe 同目录反而不一定有；源码运行时在项目根目录。
    """
    if getattr(sys, 'frozen', False):
        p = os.path.join(sys._MEIPASS, 'Newicon.ico')
    else:
        p = os.path.join(BASE_DIR, 'Newicon.ico')
    if os.path.exists(p):
        return QIcon(p)
    return QIcon()


ACTIVE_NOTES = []
_TOGGLE_HIDDEN_NOTES = set()   # 记录被「全局隐藏」操作隐藏的便签 ID，用于恢复时只显示这些

# ---------- 便签动画参数（便签显示/隐藏淡入淡出 + 折叠展开收起）----------
ANIM_FADE_IN_MS = 150      # 显示时的淡入时长
ANIM_FADE_OUT_MS = 130     # 隐藏时的淡出时长
ANIM_COLLAPSE_MS = 170     # 折叠 / 展开的窗口高度动画时长
_COLLAPSED_MIN_HEIGHT = 40  # 折叠条的最小高度兜底
_COLLAPSED_V_MARGIN = 3     # 折叠条窗口 / 卡片的上下留白（越小，夹内相邻两条贴得越紧）
_PEEK_H = stacks.PEEK_H      # 悬停"抽出"时多露出的高度（含一行预览）
_PEEK_LBL_H = 16             # 悬停预览行高度
_QWIDGETSIZE_MAX = 16777215  # Qt 默认最大尺寸（展开时恢复最大高度限制用）

DEFAULT_CONFIG = {
    "toggle_hotkey": "alt+n",
    "new_hotkey": "alt+m",
    "panel_hotkey": "alt+c",
    "show_all_hotkey": "alt+shift+n",
    "disable_all_hotkey": "ctrl+shift+a",
    "font_family": fonts_mod.DEFAULT_FONT_FAMILY,
    "autostart": True,
    "skin": "极简模式",
    "is_first_run": True,
    "save_dir": "default",          # "default" 表示使用程序同级目录，保证便携性
    "bangumi_uid": "",
    "enable_bangumi": False,
    "api_proxy": "",
    "last_bangumi_sync": "",        # 上次同步日期（YYYY-MM-DD），用于每日仅自动刷新一次
    "export_dir": "default",        # "default" 表示使用程序同级目录下的「导出的便签文本」文件夹
    "auto_update": True,            # 启动时自动检查 GitHub 新版本
    "ignored_version": "",          # 用户选择"忽略此版本"时记录，不再提示
    "stack_names": {},              # 便签夹自定义名称：{stack_id: 名称}
}


# 历史默认字体：旧版本写进配置文件的字体名。
# 5.0.0 起默认字体改为 Noto Sans SC（思源黑体同源，自带 7 档真实字重），
# 但老用户配置里存着这些旧值，会覆盖新默认值导致看不到变化 —— 需做一次静默迁移。
_LEGACY_FONT_FAMILIES = {"Microsoft YaHei", "微软雅黑", "Microsoft YaHei UI", "微软雅黑 Light", "Microsoft YaHei Light"}


def _migrate_config(cfg):
    """配置迁移：把历史默认字体平滑升级到新默认字体。

    只迁移「恰好等于旧默认值」的情况 —— 用户手动选过的其他字体一律尊重，不动。
    """
    fam = cfg.get("font_family")
    if fam in _LEGACY_FONT_FAMILIES:
        cfg["font_family"] = DEFAULT_CONFIG["font_family"]
    return cfg


def load_config():
    """加载配置文件，缺失字段自动回退到默认值。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return _migrate_config({**DEFAULT_CONFIG, **json.load(f)})
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    """将配置字典写入 JSON 文件。"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)


# 注入配置持久化回调：bangumi_oauth 刷新 token 成功后自动落盘
import bangumi_oauth as _bangumi_oauth
_bangumi_oauth.set_config_saver(save_config)


# 启动时立即解析存储目录：若为 "default"，则固定解析为 BASE_DIR 下的 notes_data。
_init_cfg = load_config()
_raw_dir = _init_cfg.get("save_dir", "default")
if _raw_dir == "default" or not _raw_dir:
    SAVE_DIR = os.path.join(BASE_DIR, "notes_data")
else:
    SAVE_DIR = _raw_dir

# 导出目录解析：默认在程序同级目录创建「导出的便签文本」
_export_raw = _init_cfg.get("export_dir", "default")
if _export_raw == "default" or not _export_raw:
    EXPORT_DIR = os.path.join(BASE_DIR, "导出的便签文本")
else:
    EXPORT_DIR = _export_raw

# 文件名安全转换表（Windows 非法字符 → 全角，用于导出文件名）
_SAFE_TRANS = str.maketrans({
    '/': '／', '\\': '＼', ':': '：',
    '*': '＊', '?': '？', '"': '＂',
    '<': '＜', '>': '＞', '|': '｜',
})


# ---------- 全局信号中枢 ----------

class GlobalSignaler(QObject):
    """应用级信号中转站，解耦模块间的调用关系。"""
    toggle_signal = Signal()
    new_note_signal = Signal()
    show_all_signal = Signal()
    note_updated_signal = Signal()
    open_panel_signal = Signal()
    config_changed_signal = Signal()
    # 便签独立快捷键
    register_note_hotkey = Signal(str, str)   # note_id, hotkey_str
    unregister_note_hotkey = Signal(str)      # note_id
    check_hotkey_conflict = Signal(str, object)  # hotkey_str, callback(list_of_names)
    force_sync_bangumi_signal = Signal()
    # 日程表提醒（title, 详情）→ app.py 收到后用系统托盘通知
    schedule_remind_signal = Signal(str, str)


global_signaler = GlobalSignaler()


# ---------- 工具函数 ----------

def get_new_note_title():
    """扫描所有现存便签，返回一个新的自动编号标题。"""
    max_num = 0
    for note in ACTIVE_NOTES:
        title = note.header.title_edit.text()
        if title.startswith("未命名便签(") and title.endswith(")"):
            num_str = title[6:-1]
            if num_str.isdigit():
                max_num = max(max_num, int(num_str))

    if os.path.exists(SAVE_DIR):
        for filename in os.listdir(SAVE_DIR):
            if filename.endswith('.json'):
                try:
                    with open(os.path.join(SAVE_DIR, filename), 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        title = data.get("title", "")
                        if title.startswith("未命名便签(") and title.endswith(")"):
                            num_str = title[6:-1]
                            if num_str.isdigit():
                                max_num = max(max_num, int(num_str))
                except Exception as e:
                    print(f"[AniNote] 解析便签文件 {filename} 失败: {e}")
    return f"未命名便签({max_num + 1})"


def migrate_legacy_notes():
    """v3.x 迁移：将旧版单文件 .json 便签移入同名文件夹。"""
    if not os.path.exists(SAVE_DIR):
        return
    for fn in os.listdir(SAVE_DIR):
        if not fn.endswith('.json'):
            continue
        src = os.path.join(SAVE_DIR, fn)
        # 忽略已经是文件夹模式的（data.json）
        if os.path.basename(src) == "data.json":
            continue
        try:
            with open(src, 'r', encoding='utf-8') as f:
                data = json.load(f)
            title = data.get("title", fn.replace('.json', ''))
            folder_name = sanitize_filename(title)
            folder = os.path.join(SAVE_DIR, folder_name)
            # 如果目标文件夹已存在，追加序号
            if os.path.exists(folder):
                counter = 1
                while os.path.exists(os.path.join(SAVE_DIR, f"{folder_name}({counter})")):
                    counter += 1
                folder_name = f"{folder_name}({counter})"
                folder = os.path.join(SAVE_DIR, folder_name)
            os.makedirs(folder, exist_ok=True)
            dest = os.path.join(folder, "data.json")
            os.rename(src, dest)
        except Exception:
            pass


def sanitize_filename(title):
    """将标题转为安全的文件名（去除非法字符，限制长度）。"""
    safe = re.sub(r'[\\/:*?"<>|]', '', title)  # 移除 Windows 非法字符
    safe = safe.strip().rstrip('.')
    if not safe:
        safe = "未命名便签"
    return safe[:80]  # 限制长度


def _cleanup_orphan_images(note_dir, html):
    """删除便签 HTML 中未引用的图片文件。"""
    if not os.path.isdir(note_dir):
        return
    if '<img ' not in html.lower():
        return  # 无图片，跳过扫描
    referenced = set()
    for m in re.finditer(r'src="([^"]+)"', html):
        src = m.group(1)
        # 跳过外部 URL
        if src.startswith(('http:', 'https:', 'file:', 'data:')):
            continue
        referenced.add(src)
    for f in os.listdir(note_dir):
        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tiff', '.svg', '.gif')):
            if f not in referenced:
                try:
                    os.remove(os.path.join(note_dir, f))
                except OSError:
                    pass


def make_save_path(title, exclude_path=None):
    """根据标题生成唯一的文件夹保存路径，返回 data.json 完整路径。"""
    base = sanitize_filename(title)
    folder = os.path.join(SAVE_DIR, base)
    path = os.path.join(folder, "data.json")
    if exclude_path and os.path.abspath(path) == os.path.abspath(exclude_path):
        return path
    if not os.path.exists(path):
        return path
    counter = 1
    while True:
        folder = os.path.join(SAVE_DIR, f"{base}({counter})")
        path = os.path.join(folder, "data.json")
        if exclude_path and os.path.abspath(path) == os.path.abspath(exclude_path):
            return path
        if not os.path.exists(path):
            return path
        counter += 1


# ---------- 富文本编辑器 ----------

class NoteTextEdit(QTextBrowser):
    """便签正文编辑器，支持内联待办事项点击切换。"""

    def __init__(self, parent_window):
        super().__init__(parent_window.bg_frame)
        self.parent_window = parent_window
        self.setReadOnly(False)
        # 禁止内部导航：QTextBrowser 默认点击链接会尝试加载目标 URL，
        # 远程网址加载失败会把内容清空 → 改为只发 anchorClicked 信号走系统浏览器
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(QDesktopServices.openUrl)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().setUndoRedoEnabled(True)

    def focusOutEvent(self, event):
        """失去焦点时自动保存。"""
        super().focusOutEvent(event)
        self.parent_window.save_data()
        global_signaler.note_updated_signal.emit()

    def mouseReleaseEvent(self, event):
        """处理待办事项勾选（仅限行首 ☐/☑ 字符）。"""
        super().mouseReleaseEvent(event)
        if self.parent_window.is_locked:
            return

        if event.button() == Qt.LeftButton:
            cursor = self.cursorForPosition(event.position().toPoint())
            block = cursor.block()
            text = block.text()

            if (text.startswith('☐') or text.startswith('☑')) and cursor.positionInBlock() <= 2:
                if len(text.strip()) <= 1:
                    return

                new_char = '☑' if text.startswith('☐') else '☐'
                edit_cursor = QTextCursor(block)
                edit_cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, 1)
                edit_cursor.insertText(new_char)

                edit_cursor.setPosition(block.position() + 1)
                edit_cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)

                text_fmt = edit_cursor.charFormat()
                text_fmt.setFontStrikeOut(new_char == '☑')
                text_fmt.setForeground(QColor("#999999") if new_char == '☑' else QColor("#333333"))
                edit_cursor.mergeCharFormat(text_fmt)
                self.parent_window.save_data()


# ---------- Markdown 便签（分栏编辑 + 实时渲染） ----------

class MarkdownHighlighter(QSyntaxHighlighter):
    """Markdown 源码轻量语法高亮：标题/行首标记/粗体/行内代码/代码块/引用/链接。"""

    def __init__(self, document):
        super().__init__(document)
        self._heading = QTextCharFormat()
        self._heading.setFontWeight(QFont.Bold)
        self._heading.setForeground(QColor("#0C447C"))
        self._mark = QTextCharFormat()
        self._mark.setForeground(QColor("#0078D7"))
        self._code_inline = QTextCharFormat()
        self._code_inline.setForeground(QColor("#B4005A"))
        self._code_block = QTextCharFormat()
        self._code_block.setBackground(QColor("#F1F3F4"))
        self._code_block.setForeground(QColor("#444444"))
        self._bold = QTextCharFormat()
        self._bold.setFontWeight(QFont.Bold)
        self._strike = QTextCharFormat()
        self._strike.setFontStrikeOut(True)
        self._strike.setForeground(QColor("#999999"))
        self._quote = QTextCharFormat()
        self._quote.setForeground(QColor("#185FA5"))
        self._link = QTextCharFormat()
        self._link.setForeground(QColor("#0078D7"))
        self._link.setFontUnderline(True)

    def highlightBlock(self, text):
        # 代码块（围栏状态机）
        if self.previousBlockState() == 1:
            if text.strip().startswith("```"):
                self.setCurrentBlockState(0)
                self.setFormat(0, len(text), self._code_block)
            else:
                self.setCurrentBlockState(1)
                self.setFormat(0, len(text), self._code_block)
            return
        if text.strip().startswith("```"):
            self.setCurrentBlockState(1)
            self.setFormat(0, len(text), self._code_block)
            return
        self.setCurrentBlockState(0)

        # 标题
        m = re.match(r"^(#{1,6})\s", text)
        if m:
            self.setFormat(0, len(m.group(1)), self._mark)
            self.setFormat(len(m.group(1)) + 1, len(text) - len(m.group(1)) - 1, self._heading)
        # 行首标记：列表 / 任务 / 引用
        m = re.match(r"^(\s*)([-*+]\s+\[[ xX]\]|[-*+]|\d+[.)]|>)", text)
        if m:
            self.setFormat(m.start(1), len(m.group(2)), self._mark)

        # 行内：粗体 / 行内代码 / 删除线 / 链接
        for mm in re.finditer(r"\*\*([^*]+)\*\*", text):
            self.setFormat(mm.start(), mm.end() - mm.start(), self._bold)
        for mm in re.finditer(r"`([^`]+)`", text):
            self.setFormat(mm.start(), mm.end() - mm.start(), self._code_inline)
        for mm in re.finditer(r"~~([^~]+)~~", text):
            self.setFormat(mm.start(), mm.end() - mm.start(), self._strike)
        for mm in re.finditer(r"\[([^\]]+)\]\(([^)\s]+)\)", text):
            self.setFormat(mm.start(), mm.end() - mm.start(), self._link)


class MdSourceEdit(QPlainTextEdit):
    """MD 源码编辑器：支持点击行首 [ ]/[x] 切换任务勾选。"""

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self.isReadOnly():
            cursor = self.cursorForPosition(event.position().toPoint())
            block = cursor.block()
            line = block.text()
            m = re.match(r"^(\s*[-*+]\s+\[)([ xX])(\])", line)
            if m and cursor.positionInBlock() <= m.end(2) and event.position().toPoint().x() >= 0:
                new_char = "x" if m.group(2) != "x" else " "
                edit_cur = QTextCursor(block)
                edit_cur.setPosition(block.position() + m.start(2))
                edit_cur.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, 1)
                edit_cur.insertText(new_char)
                return
        super().mouseReleaseEvent(event)


def setup_auto_hide_scrollbar(sb, visible_qss, hidden_qss, timeout_ms=1200):
    """滚动条自动隐藏：未滚动时应用 hidden_qss（透明），滚动时切 visible_qss，
    停止滚动 timeout 后切回隐藏。通过切换 QSS 实现（滚动条仍占位，内容不跳动）。

    sb: QScrollBar；timer 挂在 sb 上（测试可用 sb._auto_hide_timer 模拟超时）。
    """
    state = {"show": False}

    def _hide():
        if state["show"]:
            state["show"] = False
            sb.setStyleSheet(hidden_qss)

    def _show():
        state["show"] = True
        sb.setStyleSheet(visible_qss)
        timer.start()

    timer = QTimer(sb)
    timer.setSingleShot(True)
    timer.setInterval(timeout_ms)
    timer.timeout.connect(_hide)
    sb._auto_hide_timer = timer  # 测试钩子

    sb.valueChanged.connect(_show)
    sb.setStyleSheet(hidden_qss)


class MarkdownSplitEdit(QWidget):
    """Markdown 分栏编辑器：左源码（语法高亮）+ 右实时渲染（QTextBrowser）。

    - textChanged → 150ms 防抖 → markdown_conv.md_to_html → setHtml
    - 左右滚动条按比例互绑（guard 防循环）
    - set_markdown 装载时通过 _rendering 抑制误渲染
    - 隐藏源码 = 窗口宽度切半（视觉上像直接砍掉左半边源码），仅 MD 模式生效
    - 滚动条自动隐藏：未滚动时透明，滚动时显示，停止 1.2s 后隐藏
    """

    src_visibility_changed = Signal(bool)  # True=源码可见（供父窗口同步眼睛按钮）

    def __init__(self, parent_window, parent=None):
        super().__init__(parent)
        self.parent_window = parent_window
        self._rendering = False      # 程序性装载/渲染中，抑制脏标记
        self._syncing_scroll = False # 滚动互绑 guard
        self._dirty_md = False       # 源码是否被用户修改过（决定切回富文本用缓存还是重渲染）
        self._base_dir = ""          # 便签目录，用于解析相对路径图片
        self._locked = False         # 便签锁定（锁定只展示渲染，隐藏源码）
        self._user_hidden_src = False  # 用户手动隐藏源码
        self._saved_full_width = 0   # 隐藏源码前的全宽（恢复用）
        self._orig_min_width = 320   # 原最小宽（切半后恢复用）

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(3)
        self.splitter.setChildrenCollapsible(True)

        # 左：源码
        self.src = MdSourceEdit()
        # 源码区保留等宽字体（Consolas）在前，中文回退到界面字体
        self.src.setStyleSheet(
            "QPlainTextEdit { border: none; background: transparent;"
            f" font-family: Consolas, '{fonts_mod.resolve_family()}', monospace;"
            " font-size: 13px; color: #333333; }"
        )
        self.hl = MarkdownHighlighter(self.src.document())

        # 右：渲染
        self.preview = QTextBrowser()
        # 禁止内部导航：点击链接不清空内容，只发 anchorClicked 走系统浏览器
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.preview.anchorClicked.connect(QDesktopServices.openUrl)
        self.preview.setStyleSheet(
            "QTextBrowser { border: none; background: transparent;"
            f" font-family: '{fonts_mod.resolve_family()}';"
            " font-size: 13px; color: #333333; }"
        )

        # 细滚动条（对齐便签风格）；未滚动时透明隐藏，滚动时显示
        bar_qss = (
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            " QScrollBar::handle:vertical { background: #C0C0C0; border-radius: 3px; min-height: 20px; }"
            " QScrollBar::handle:vertical:hover { background: #A0A0A0; }"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            " QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
            " QScrollBar:horizontal { background: transparent; height: 6px; margin: 0; }"
            " QScrollBar::handle:horizontal { background: #C0C0C0; border-radius: 3px; min-width: 20px; }"
            " QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }"
        )
        bar_qss_hidden = (
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            " QScrollBar::handle:vertical { background: transparent; border-radius: 3px; min-height: 20px; }"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            " QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
            " QScrollBar:horizontal { background: transparent; height: 6px; margin: 0; }"
            " QScrollBar::handle:horizontal { background: transparent; border-radius: 3px; min-width: 20px; }"
            " QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }"
        )
        for _sb in (self.src.verticalScrollBar(), self.src.horizontalScrollBar(),
                    self.preview.verticalScrollBar(), self.preview.horizontalScrollBar()):
            setup_auto_hide_scrollbar(_sb, bar_qss, bar_qss_hidden)

        self.splitter.addWidget(self.src)
        self.splitter.addWidget(self.preview)
        self.splitter.setSizes([400, 400])
        layout.addWidget(self.splitter)

        # 防抖渲染
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(150)
        self._render_timer.timeout.connect(self._do_render)
        self.src.textChanged.connect(self._on_src_changed)

        # 滚动同步（按比例映射，guard 防循环）
        self.src.verticalScrollBar().valueChanged.connect(self._on_src_scroll)
        self.preview.verticalScrollBar().valueChanged.connect(self._on_preview_scroll)

        # 预览区双击图片 → 打开文件夹内原图（对齐普通便签"双击查看原图"）
        self.preview.viewport().installEventFilter(self)

    # ---------- 对外 API ----------

    def set_base_dir(self, d):
        self._base_dir = d or ""

    def set_markdown(self, md, base_dir=None):
        """程序性装载 Markdown 源码并立即渲染（不触发脏标记）。"""
        if base_dir is not None:
            self._base_dir = base_dir
        self._rendering = True
        self.src.setPlainText(md)
        self._rendering = False
        self._dirty_md = False
        self._do_render()

    def markdown_text(self):
        return self.src.toPlainText()

    def is_dirty(self):
        """源码自上次装载/清除后是否被用户修改。"""
        return self._dirty_md

    def clear_dirty(self):
        self._dirty_md = False

    def set_readonly(self, ro):
        self.src.setReadOnly(ro)
        if ro:
            self.src.setTextInteractionFlags(Qt.NoTextInteraction)
            self.src.clearFocus()
        else:
            self.src.setTextInteractionFlags(Qt.TextEditorInteraction)

    def set_locked(self, locked):
        """便签锁定：源码只读；MD 模式下隐藏源码（窗口切半）只展示渲染。"""
        self._locked = locked
        self.src.setReadOnly(locked)
        if locked:
            self.src.setTextInteractionFlags(Qt.NoTextInteraction)
            self.src.clearFocus()
        else:
            self.src.setTextInteractionFlags(Qt.TextEditorInteraction)
        self._apply_src_visible()

    def set_user_hidden_src(self, hidden):
        """用户手动隐藏/显示源码（工具栏眼睛按钮 / 右键菜单）。"""
        self._user_hidden_src = hidden
        self._apply_src_visible()

    def src_visible_state(self):
        """当前源码是否可见（考虑锁定与手动隐藏）。"""
        return not (self._locked or self._user_hidden_src)

    def _apply_src_visible(self):
        """按源码可见性更新界面：MD 模式隐藏源码 = 窗口宽度切半。

        视觉上就像直接砍掉左半边源码，渲染区占满剩余宽度；
        恢复时把记录的全宽还回去。非 MD 模式（普通便签）不切宽。
        """
        visible = self.src_visible_state()
        win = self.parent_window
        if win and getattr(getattr(win, 'editor_host', None), 'is_md', False):
            if visible:
                if self._saved_full_width:
                    w = max(self._saved_full_width, self._orig_min_width)
                    win.setMinimumWidth(self._orig_min_width)
                    win.resize(w, win.height())
                    self._saved_full_width = 0
            else:
                # 仅在首次进入隐藏状态时记录全宽并切半；锁定/重复调用不再二次切半
                if not self._saved_full_width:
                    self._orig_min_width = win.minimumWidth()
                    self._saved_full_width = win.width()
                    half = max(win.width() // 2, 200)  # 半宽临时下限，避免切到极小
                    win.setMinimumWidth(200)
                    win.resize(half, win.height())
        self.src.setVisible(visible)
        self.src_visibility_changed.emit(visible)

    def insert_image_syntax(self, rel_path):
        """在源码光标处插入 Markdown 图片语法并触发渲染。"""
        cur = self.src.textCursor()
        cur.insertText(f"![{os.path.basename(rel_path)}]({rel_path.replace(os.sep, '/')})")
        self.src.setFocus()

    def eventFilter(self, obj, event):
        """预览区双击图片：用系统默认看图程序打开文件夹内存储的原图。"""
        if obj is self.preview.viewport() and event.type() == QEvent.MouseButtonDblClick:
            cursor = self.preview.cursorForPosition(event.pos())
            fmt = cursor.charFormat()
            if fmt.isImageFormat():
                name = fmt.toImageFormat().name()
                if name:
                    win = self.parent_window
                    if win and hasattr(win, "_open_image_external"):
                        win._open_image_external(name)
                    return True
        return super().eventFilter(obj, event)

    # ---------- 内部 ----------

    def _on_src_changed(self):
        if not self._rendering:
            self._dirty_md = True
            self._render_timer.start()

    def _do_render(self):
        from markdown_conv import md_to_html
        base = self._base_dir
        # 兜底：base_dir 未设置（如新建 MD 便签尚未保存）时从父窗口 save_file 推断
        if not base:
            win = self.parent_window
            if win and getattr(win, "save_file", ""):
                base = os.path.dirname(win.save_file).replace("\\", "/")
                self._base_dir = base
        md = self.src.toPlainText()
        html = md_to_html(md, base)
        self._rendering = True
        self.preview.setHtml(html)
        self._rendering = False

    def _on_src_scroll(self, value):
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        sb_src = self.src.verticalScrollBar()
        sb_pv = self.preview.verticalScrollBar()
        ratio = value / max(sb_src.maximum(), 1)
        sb_pv.setValue(int(ratio * sb_pv.maximum()))
        self._syncing_scroll = False

    def _on_preview_scroll(self, value):
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        sb_src = self.src.verticalScrollBar()
        sb_pv = self.preview.verticalScrollBar()
        ratio = value / max(sb_pv.maximum(), 1)
        sb_src.setValue(int(ratio * sb_src.maximum()))
        self._syncing_scroll = False


class EditorHost(QWidget):
    """便签编辑器容器：普通富文本视图（NoteTextEdit）与 Markdown 分栏视图共存。

    切换 = hide/show，不销毁不重建（布局/焦点/滚动位置各自保留）。
    is_md 标记当前激活视图；保存时按模式双写 html_content / content_md。
    """

    def __init__(self, parent_window, rich_view):
        super().__init__(parent_window.bg_frame)
        self.parent_window = parent_window
        self.is_md = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.rich_view = rich_view          # NoteTextEdit（外部创建，addWidget 自动 reparent）
        self.md_view = MarkdownSplitEdit(parent_window)
        layout.addWidget(self.rich_view)
        layout.addWidget(self.md_view)
        self.md_view.hide()


# ---------- 标题栏 ----------

class HeaderBar(QWidget):
    """便签标题栏：标题输入框 + 拖拽点心 + 工具栏容器。"""

    # 标题样式。字号 / 字重 / 字体族全部交给 QFont（不写进 QSS）：
    # QSS 里的 font-family 优先级高于 setFont，会把用户在控制面板选的字体盖掉；
    # 且字号要支持长标题自动缩小、字重需要命中真实字面。
    TITLE_QSS = (
        "QLineEdit { border: none; background: transparent; color: #222;"
        " padding: 2px; }"
        " QLineEdit:focus { background: rgba(255, 255, 255, 0.5); border-radius: 4px; }"
    )
    TITLE_QSS_COMPACT = (
        "QLineEdit { border: none; background: transparent; color: #333333;"
        " padding: 1px 2px; }"
    )
    TITLE_PX = 18          # 常规标题字号
    TITLE_PX_MIN = 12      # 长标题自动缩小的下限（再小就影响阅读了）
    TITLE_PX_COMPACT = 14  # 折叠条标题字号
    TITLE_W_RATIO = 2 / 3  # 展开态：标题最多占「可伸缩宽度」的 2/3，其余留给拖动空白区
                           # （折叠态不受此限，标题吃满整条）

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.parent_window = parent_window
        self._is_dragging = False
        self._drag_pos = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 0)
        layout.setSpacing(2)

        self.title_layout = QHBoxLayout()
        self.title_layout.setSpacing(5)

        self.title_edit = QLineEdit(self)
        self.title_edit.setStyleSheet(self.TITLE_QSS)
        self._apply_title_font(self.TITLE_PX, bold=True)
        self.title_edit.setPlaceholderText("请输入便签标题...")
        self.title_edit.textChanged.connect(lambda: self.parent_window._mark_dirty())
        self.title_edit.textChanged.connect(self._update_title_tooltip)
        self.title_edit.textChanged.connect(self.refresh_title_font)
        self.title_edit.editingFinished.connect(lambda: global_signaler.note_updated_signal.emit())
        self.title_edit.editingFinished.connect(self.refresh_title_font)
        self.title_layout.addWidget(self.title_edit)

        self.drag_handle = QLabel("⋮")   # 单个点列：窄（原来两个点占 58px，挤掉标题宽度）
        self.drag_handle.setToolTip("按住此处或标题右侧空白处拖动便签")
        self.drag_handle.setStyleSheet("color: #aaa; font-weight: bold; font-size: 13px; padding: 0 1px;")
        self.drag_handle.setCursor(Qt.OpenHandCursor)
        self.title_layout.addStretch()   # 标题右侧这一截空白本身就是可拖动区
        self.title_layout.addWidget(self.drag_handle)
        layout.addLayout(self.title_layout)

        self.toolbar_container = QWidget()
        self.toolbar_layout = QHBoxLayout(self.toolbar_container)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(5)
        layout.addWidget(self.toolbar_container)

    # ---------- 标题字号自适应 ----------

    def _title_font(self, px, bold=True):
        """便签标题字体。

        统一走 fonts_mod.make_font：优先命中真实 Bold 字面，
        避免 Qt 合成粗体把笔画糊在一起（这是标题「发虚」的根源）。
        字体族取用户配置（用户可在控制面板换字体）。
        """
        family = None
        try:
            family = self.parent_window._ui_font_family()
        except Exception:
            family = None      # 窗口尚未初始化完成时，交给 resolve_family 兜底
        return fonts_mod.make_font(
            family, px=int(px), weight="bold" if bold else "regular"
        )

    def _apply_title_font(self, px, bold=True):
        self._title_px = int(px)
        self.title_edit.setFont(self._title_font(px, bold))

    # ---------- 折叠 / 展开之间的标题字号过渡 ----------

    def animate_title_font(self, from_px, to_px, ms):
        """展开时把标题字号从折叠态值平滑过渡到展开态值（字重即时生效）。

        直接一次性切到终值，会在动画刚开始时"字体突然变大变粗"；
        这里补一段与高度动画同长的短过渡。动画期间 `refresh_title_font` 让位，
        否则 resizeEvent 每帧都会把它按回终值，过渡就没了。
        """
        anim = getattr(self, '_title_font_anim', None)
        if anim is None:
            anim = QVariantAnimation(self)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.valueChanged.connect(self._on_title_font_step)
            anim.finished.connect(self._on_title_font_done)
            self._title_font_anim = anim
        anim.stop()
        self._title_anim_active = True
        self._apply_title_font(from_px, bold=True)   # 先回到起点，避免闪一帧大字号
        anim.setDuration(max(int(ms), 1))
        anim.setStartValue(float(from_px))
        anim.setEndValue(float(to_px))
        anim.start()

    def _on_title_font_step(self, value):
        self._apply_title_font(int(round(float(value))), bold=True)

    def _on_title_font_done(self):
        self._title_anim_active = False
        self.refresh_title_font()

    def stop_title_font_anim(self):
        """折叠 / 重排时立刻停掉字号过渡，别让它继续改样式。"""
        anim = getattr(self, '_title_font_anim', None)
        self._title_anim_active = False
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:
                self._title_font_anim = None

    def refresh_title_font(self):
        """按行宽给标题定宽，并在放不下时逐级缩小字号。

        宽度策略：标题只占「可伸缩宽度」的 TITLE_W_RATIO（约 2/3），剩下的一截
        连同点心一起留给拖动（否则标题铺满整行，标题区域点下去是进入编辑态，
        就没有地方能按住拖便签了）。用 setFixedWidth 钉死宽度——既保证了留给
        拖动的空白，也避免 QLineEdit 的 sizeHint 随字号变化造成"越缩越窄"的死循环。
        折叠态不干预（那里标题要占满整条）。
        """
        if getattr(self.parent_window, 'is_collapsed', False):
            return
        if getattr(self, '_title_anim_active', False):
            return          # 展开时的字号过渡动画期间由它接管
        try:
            row_w = self.width()
            if row_w < 80:
                return
            lay = self.title_layout
            reserve = 0
            for i in range(lay.count()):
                item = lay.itemAt(i)
                w = item.widget() if item is not None else None
                if w is None or w is self.title_edit or not w.isVisible():
                    continue
                reserve += w.sizeHint().width() + lay.spacing()
            m = self.layout().contentsMargins()
            flex = max(row_w - m.left() - m.right() - reserve, 60)
            cap = max(int(flex * self.TITLE_W_RATIO), 60)
            if self.title_edit.width() != cap or self.title_edit.minimumWidth() != cap:
                self.title_edit.setFixedWidth(cap)
            text = self.title_edit.text().strip()
            fit_w = max(cap - 12, 30)   # 减去输入框左右内边距
            px = self.TITLE_PX
            while px > self.TITLE_PX_MIN:
                if QFontMetrics(self._title_font(px)).horizontalAdvance(text) <= fit_w:
                    break
                px -= 1
            self._apply_title_font(px, bold=True)
        except RuntimeError:
            pass

    def resizeEvent(self, event):
        """宽度一变就重新适配标题字号（标题在布局就绪前被设置也不会漏掉）。"""
        super().resizeEvent(event)
        self.refresh_title_font()

    def set_title_compact(self, compact):
        """折叠条标题样式：字号放小、取消粗体（展开时还原常规样式与自适应字号）。"""
        if compact:
            self.stop_title_font_anim()
            self.title_edit.setStyleSheet(self.TITLE_QSS_COMPACT)
            self._apply_title_font(self.TITLE_PX_COMPACT, bold=False)
        else:
            self.title_edit.setStyleSheet(self.TITLE_QSS)
            self.refresh_title_font()

    def _update_title_tooltip(self):
        """标题框放不下时，悬停即可看到完整标题。"""
        self.title_edit.setToolTip(self.title_edit.text().strip())

    def _draggable(self):
        """可拖动条件：未锁定，或处于折叠态（折叠条本就靠拖动移动）。"""
        return (not self.parent_window.is_locked
                or getattr(self.parent_window, 'is_collapsed', False))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._draggable():
            self._is_dragging = True
            self._drag_pos = event.globalPosition().toPoint() - self.parent_window.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._is_dragging and self._draggable():
            self.parent_window.move(event.globalPosition().toPoint() - self._drag_pos)
            stacks.STACKS.on_drag_moved(self.parent_window)   # 叠内实时让位
            event.accept()

    def mouseReleaseEvent(self, event):
        self._is_dragging = False
        self.parent_window.save_data()
        stacks.STACKS.on_drag_released(self.parent_window)    # 吸附 / 排序 / 拆出
        st = stacks.STACKS.stack_of(self.parent_window)
        if st is not None:
            stacks.STACKS.clamp_header(st)     # 夹子被拖出屏幕时拉回来


# ---------- 现代风格通用对话框 ----------

class ResizeHandle(QWidget):
    """便签四角拖拽缩放控件。"""

    TOP_LEFT, TOP_RIGHT, BOTTOM_LEFT, BOTTOM_RIGHT = 0, 1, 2, 3

    def __init__(self, parent, anchor=BOTTOM_RIGHT, window=None):
        super().__init__(parent)
        self.parent_window = window or parent
        self._anchor = anchor
        self.setFixedSize(20, 20)
        cursors = [Qt.SizeFDiagCursor, Qt.SizeBDiagCursor, Qt.SizeBDiagCursor, Qt.SizeFDiagCursor]
        self.setCursor(cursors[anchor])
        bg_styles = [
            "background-color: rgba(0,0,0,0.05); border-top-left-radius: 12px;",
            "background-color: rgba(0,0,0,0.05); border-top-right-radius: 12px;",
            "background-color: rgba(0,0,0,0.05); border-bottom-left-radius: 12px;",
            "background-color: rgba(0,0,0,0.05); border-bottom-right-radius: 12px;",
        ]
        self.setStyleSheet(bg_styles[anchor])
        self._is_resizing = False
        self._start_pos = None
        self._start_geom = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.parent_window.is_locked:
            self._is_resizing = True
            self._start_pos = event.globalPosition().toPoint()
            self._start_geom = self.parent_window.geometry()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._is_resizing and not self.parent_window.is_locked:
            delta = event.globalPosition().toPoint() - self._start_pos
            g = QRect(self._start_geom)
            a = self._anchor
            if a in (self.TOP_LEFT, self.BOTTOM_LEFT):
                g.setLeft(min(g.right() - self.parent_window.minimumWidth(), g.left() + delta.x()))
            if a in (self.TOP_LEFT, self.TOP_RIGHT):
                g.setTop(min(g.bottom() - self.parent_window.minimumHeight(), g.top() + delta.y()))
            if a in (self.TOP_RIGHT, self.BOTTOM_RIGHT):
                g.setRight(max(g.left() + self.parent_window.minimumWidth(), g.right() + delta.x()))
            if a in (self.BOTTOM_LEFT, self.BOTTOM_RIGHT):
                g.setBottom(max(g.top() + self.parent_window.minimumHeight(), g.bottom() + delta.y()))
            self.parent_window.setGeometry(g)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._is_resizing = False
        self.parent_window.save_data()

class FormatPanel(QFrame):
    """内联二级格式化面板 (类似 Word 的 Ribbon 展开栏)"""
    def __init__(self, parent_window):
        super().__init__(parent_window.bg_frame)
        self.parent_window = parent_window
        self.setVisible(False) 
        
        # 半透明磨砂质感背景
        self.setStyleSheet("QFrame { background-color: rgba(255, 255, 255, 0.85); border-radius: 8px; margin: 0 5px; }")
        self.setFixedHeight(45)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent;")
        layout.addWidget(self.stack)
        
        self._init_font_page()
        self._init_text_color_page()
        self._init_bg_page()

    def _create_format_btn(self, text, tooltip, callback, style=""):
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.setFixedSize(26, 26)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{ border: none; border-radius: 5px; font-weight: bold; font-size: 14px; color: #444444; background-color: transparent; {style} }} 
            QPushButton:hover {{ background-color: rgba(0, 0, 0, 0.08); color: #000000; }}
            QPushButton:pressed {{ background-color: rgba(0, 0, 0, 0.15); }}
        """)
        btn.clicked.connect(callback)
        return btn

    def _init_font_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(5, 0, 5, 0)
        layout.setSpacing(8)
        
        component_style = (
            "QWidget { border: 1px solid #D1D1D1; border-radius: 6px;"
            " background-color: #FFFFFF; color: #333333;"
            f" font-family: '{fonts_mod.resolve_family()}'; font-size: 13px;"
            " padding-left: 5px; }"
            " QWidget:hover { border: 1px solid #0078D7; }"
            " QWidget:focus { border: 1px solid #0078D7; background-color: #FCFCFC; }"
        )

        # 1. 字体选择框美化
        self.font_combo = QFontComboBox()
        self.font_combo.setFontFilters(QFontComboBox.ScalableFonts) # 过滤远古报错字体
        self.font_combo.setFixedHeight(28)
        self.font_combo.setFixedWidth(180) 
        self.font_combo.setStyleSheet(component_style + """
            QFontComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 20px; border-left: none; }
            QFontComboBox::down-arrow { border-left: 4px solid transparent; border-right: 4px solid transparent; border-top: 5px solid #888888; }
            QFontComboBox QAbstractItemView { border: 1px solid #ccc; border-radius: 4px; background-color: white; selection-background-color: #E8F4FD; selection-color: #0078D7; }
            QFontComboBox QAbstractItemView QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }
            QFontComboBox QAbstractItemView QScrollBar::handle:vertical { background: #DCDCDC; border-radius: 3px; min-height: 20px; }
            QFontComboBox QAbstractItemView QScrollBar::handle:vertical:hover { background: #A9A9A9; }
            QFontComboBox QAbstractItemView QScrollBar::add-line:vertical, QFontComboBox QAbstractItemView QScrollBar::sub-line:vertical { height: 0px; }
        """)
        self.font_combo.wheelEvent = lambda event: event.ignore()
        self.font_combo.currentFontChanged.connect(lambda f: self.parent_window.change_font_family(f))
        layout.addWidget(self.font_combo)
        
        # 2. 字号选择框美化
        self.size_spin = QSpinBox()
        self.size_spin.setRange(8, 72)
        self.size_spin.setValue(18)
        self.size_spin.setSuffix(" px")
        self.size_spin.setFixedHeight(28)
        self.size_spin.setFixedWidth(70) 
        self.size_spin.setStyleSheet(component_style + """
            QSpinBox::up-button, QSpinBox::down-button { border: none; background: transparent; width: 16px; }
            QSpinBox::up-arrow { image: none; border-left: 3px solid transparent; border-right: 3px solid transparent; border-bottom: 4px solid #888; }
            QSpinBox::down-arrow { image: none; border-left: 3px solid transparent; border-right: 3px solid transparent; border-top: 4px solid #888; }
        """)
        self.size_spin.wheelEvent = lambda event: event.ignore()
        self.size_spin.valueChanged.connect(lambda v: self.parent_window.change_font_size(v))
        layout.addWidget(self.size_spin)
        
        # 3. 分隔线与 BIU 按钮
        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setStyleSheet("color: rgba(0, 0, 0, 0.1); margin: 8px 2px;")
        layout.addWidget(separator)
        
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)
        btn_layout.addWidget(self._create_format_btn("B", "加粗", self.parent_window.toggle_bold))
        btn_layout.addWidget(self._create_format_btn("I", "斜体", self.parent_window.toggle_italic, "font-style: italic; font-family: 'Georgia';"))
        btn_layout.addWidget(self._create_format_btn("U", "下划线", self.parent_window.toggle_underline, "text-decoration: underline;"))
        layout.addLayout(btn_layout)
        
        layout.addStretch()
        # 👑 就是这行关键代码丢了导致错位！现在安全补上：
        self.stack.addWidget(page) 

    def _init_text_color_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        
        colors = ["#333333", "#E81123", "#0078D7", "#107C10", "#D83B01", "#881798"]
        for c in colors:
            btn = QPushButton()
            btn.setFixedSize(24, 24)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {c}; border-radius: 12px;"
                f" border: 2px solid rgba(0,0,0,0.08); }}"
                " QPushButton:hover { border: 2px solid rgba(0,0,0,0.3); }"
            )
            btn.clicked.connect(lambda checked, color=c: self.parent_window.change_font_color_direct(color))
            layout.addWidget(btn)
        
        more_btn = self._create_format_btn(icon("palette"), "自定义颜色", self.parent_window.change_font_color, "font-weight: normal;")
        set_icon_font(more_btn, 14)
        layout.addWidget(more_btn)
        layout.addStretch()
        self.stack.addWidget(page) 

    def _init_bg_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        
        presets = [(255, 249, 196), (255, 204, 229), (204, 238, 255), (204, 255, 204), (230, 204, 255), (240, 240, 240)]
        for r, g, b in presets:
            btn = QPushButton()
            btn.setFixedSize(24, 24)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background-color: rgb({r}, {g}, {b}); border-radius: 12px;"
                f" border: 2px solid rgba(0,0,0,0.08); }}"
                " QPushButton:hover { border: 2px solid rgba(0,0,0,0.3); }"
            )
            btn.clicked.connect(lambda checked, c=(r,g,b): self.parent_window.change_bg_base_color(c))
            layout.addWidget(btn)
            
        custom_btn = self._create_format_btn(icon("palette"), "自定义背景色", self.parent_window.pick_custom_bg_color, "font-weight: normal;")
        set_icon_font(custom_btn, 14)
        layout.addWidget(custom_btn)
        
        layout.addWidget(QLabel(" 透明度:"))
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(15, 100)
        self.opacity_slider.setFixedWidth(80)
        self.opacity_slider.setCursor(Qt.PointingHandCursor)
        self.opacity_slider.setStyleSheet(
            "QSlider::groove:horizontal {"
            " height: 4px; background: #E0E0E0; border-radius: 2px; }"
            " QSlider::handle:horizontal {"
            " width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;"
            " background: #1A73E8; }"
            " QSlider::handle:horizontal:hover { background: #1765CC; }"
            " QSlider::sub-page:horizontal { background: #1A73E8; border-radius: 2px; }"
        )
        self.opacity_slider.valueChanged.connect(self.parent_window.change_bg_opacity)
        layout.addWidget(self.opacity_slider)
        
        layout.addStretch()
        self.stack.addWidget(page)
# ---------- 便签主窗口 ----------

class AniNoteWindow(QWidget):
    """桌面便签主窗口。

    支持富文本编辑、待办清单、锁定/置顶/隐藏、拖拽缩放、右键菜单，
    以及 Bangumi 新番特殊模式。
    """

    def __init__(self, note_id=None):
        super().__init__()

        if not os.path.exists(SAVE_DIR):
            os.makedirs(SAVE_DIR)

        # note_id 作为稳定的内部标识，始终使用 UUID
        # save_file 根据标题动态生成，文件名可能随标题变更而改变
        self.note_id = note_id if note_id else uuid.uuid4().hex
        self.save_file = None  # 首次 save_data() 时根据标题生成
        self._note_hotkey = ""  # 便签独立快捷键

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(320, 280)
        self.bg_color = [255, 249, 196, 242]

        self.is_locked = False
        self.is_always_on_top = True
        self._deleted = False   # 标记为已删除，防止 closeEvent 重新写盘

        # 便签折叠（收到一条，仅显示标题/底色/透明度）
        self.is_collapsed = False
        self._expanded_size = None           # 折叠前的窗口尺寸，展开时还原
        self._collapsed_prev_visible = {}    # 折叠前各内容控件的可见性快照
        self._saved_win_margins = None       # 折叠时临时收紧的窗口外边距
        self._saved_frame_margins = None     # 折叠时临时收紧的卡片内边距
        self._saved_min_size = None          # 折叠前的最小窗口尺寸（各类便签不同）
        self._saved_frame_min = None         # 折叠前卡片最小尺寸
        self._collapsed_toolbar_prev = None  # 折叠前工具栏可见性
        self._strip_drag_pos = None          # 折叠条整条拖动的位置基准
        self._pending_collapse = False       # 待落位的折叠状态（首次显示时应用）
        self._strip_h = 0                    # 折叠条基准高度缓存（不含悬停预览行）
        self._peeking = False                # 悬停"抽出"状态
        self._peek_lbl = None                # 悬停预览行标签
        self._hover_timer = None             # 悬停判定防抖
        self.stack_id = ""                   # 所属便签集（空串 = 未成集）
        self.stack_pos = 0                   # 在便签集内的顺序
        self._opacity_anim = None            # 显示/隐藏淡入淡出动画
        self._collapse_anim = None           # 折叠/展开高度动画

        # 防抖保存：500ms 无操作后才真正写盘
        self._dirty = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._flush_save)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)

        self.bg_frame = QFrame(self)
        self.bg_frame.setObjectName("bg_frame")
        self.bg_frame.setStyleSheet(
            "QFrame#bg_frame { background-color: rgba(255, 249, 196, 0.95); border-radius: 15px; }"
        )
        self.bg_frame.setMinimumSize(300, 260)
        main_layout.addWidget(self.bg_frame)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(15)
        shadow.setColor(QColor(0, 0, 0, 50))
        shadow.setOffset(0, 4)
        self.bg_frame.setGraphicsEffect(shadow)
        self._bg_shadow = shadow

        frame_layout = QVBoxLayout(self.bg_frame)
        frame_layout.setContentsMargins(12, 12, 12, 12)
        frame_layout.setSpacing(5)

        self.header = HeaderBar(self)
        # 顶部对齐：折叠条里其余控件全被隐藏，若交给 QVBoxLayout 分配，它会把
        # 多出来的高度居中给标题行 —— 折叠 / 展开动画时标题就会漂到窗口中间
        frame_layout.addWidget(self.header, 0, Qt.AlignTop)

        self.format_panel = FormatPanel(self)
        frame_layout.addWidget(self.format_panel)

        self._build_toolbar()

        self.header.toolbar_layout.addStretch()

        # 便签独立快捷键设置按钮
        self.settings_btn = self._create_tool_btn(
            icon("settings"), "设置便签快捷键",
            self._open_note_hotkey_dialog, "color: #555; font-weight: normal;"
        )
        set_icon_font(self.settings_btn, 14)

        cfg = load_config()
        self.new_note_btn = self._create_tool_btn(
            icon("add"), f"新建 ({cfg['new_hotkey'].upper()})",
            self.create_new_note, "color: #555;"
        )
        set_icon_font(self.new_note_btn, 14)
        self.update_button_hints()
        global_signaler.config_changed_signal.connect(self.update_button_hints)
        del_btn = self._create_tool_btn(
            icon("delete"), "彻底删除",
            lambda: self.delete_note(confirm=True), "color: #555; font-weight: normal;"
        )
        set_icon_font(del_btn, 14)

        # 右上角折叠按钮（减号）：把便签收成一条；锁定态也可点击，故放进标题行
        self.collapse_btn = QPushButton(icon("remove"))
        set_icon_font(self.collapse_btn, 14)
        self.collapse_btn.setToolTip("折叠便签（收成一条）")
        self.collapse_btn.setFixedSize(24, 24)
        self.collapse_btn.setCursor(Qt.PointingHandCursor)
        self.collapse_btn.setStyleSheet(
            "QPushButton { border: none; border-radius: 6px; color: #555;"
            " background: transparent; }"
            " QPushButton:hover { background-color: rgba(0,0,0,0.12); }"
            " QPushButton:pressed { background-color: rgba(0,0,0,0.2); }"
        )
        self.collapse_btn.clicked.connect(self.toggle_collapse)
        self.header.title_layout.addWidget(self.collapse_btn)
        # 展开态也让标题占大头（默认 QLineEdit 只按 sizeHint 那么宽，长标题显示不下）
        self._set_title_stretch(False)

        self.text_edit = NoteTextEdit(self)
        self.text_edit.viewport().installEventFilter(self)
        self.text_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.text_edit.setStyleSheet(
            f"QTextEdit {{ border: none; background: transparent; font-size: 18px; "
            f"font-family: '{cfg['font_family']}'; color: #333333; }}"
        )
        self.text_edit.textChanged.connect(self._mark_dirty)
        self.editor_host = EditorHost(self, self.text_edit)
        self.editor_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        frame_layout.addWidget(self.editor_host)
        # 眼睛按钮与源码可见性联动
        if not self._is_special_note():
            self.editor_host.md_view.src_visibility_changed.connect(
                self._update_src_vis_btn
            )

        # 四角缩放手柄（作为 bg_frame 的子控件，在 resizeEvent 中定位）
        self._grips = [
            ResizeHandle(self.bg_frame, ResizeHandle.TOP_LEFT, self),
            ResizeHandle(self.bg_frame, ResizeHandle.TOP_RIGHT, self),
            ResizeHandle(self.bg_frame, ResizeHandle.BOTTOM_LEFT, self),
            ResizeHandle(self.bg_frame, ResizeHandle.BOTTOM_RIGHT, self),
        ]

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.text_edit.setContextMenuPolicy(Qt.CustomContextMenu)
        self.text_edit.customContextMenuRequested.connect(self.show_context_menu)
        # MD 分栏的源码区与预览区右键 → 便签右键菜单
        self.editor_host.md_view.src.setContextMenuPolicy(Qt.CustomContextMenu)
        self.editor_host.md_view.src.customContextMenuRequested.connect(self.show_context_menu)
        self.editor_host.md_view.preview.setContextMenuPolicy(Qt.CustomContextMenu)
        self.editor_host.md_view.preview.customContextMenuRequested.connect(self.show_context_menu)

        self.load_data()

        # 默认 Markdown 模式（仅新建便签且配置开启时生效；特殊便签排除）
        if (not self.save_file and not self._is_special_note()
                and load_config().get("default_markdown", False)):
            self._set_markdown_mode(True)

        # Bangumi 新番便签特殊初始化
        if self.note_id == "bangumi_schedule":
            self._init_bangumi_mode()

        self.apply_window_states()
        self._apply_bg_color()

        if self not in ACTIVE_NOTES:
            ACTIVE_NOTES.append(self)
    
    # --- 字体与颜色控制接口 ---
    def change_font_family(self, font):
        if not hasattr(self, "text_edit"):
            return  # 初始化早期信号（font_combo 默认字体）触发时 text_edit 尚未创建
        fmt = self.text_edit.currentCharFormat()
        fmt.setFontFamily(font.family())
        self.text_edit.mergeCurrentCharFormat(fmt)
        self._mark_dirty()

    def change_font_size(self, size):
        self.text_edit.setFontPointSize(size)
        self._mark_dirty()

    def change_font_color_direct(self, hex_color):
        """直接使用预设的十六进制颜色"""
        self.text_edit.setTextColor(QColor(hex_color))
        self._mark_dirty()

    # --- 背景控制接口 ---
    def change_bg_base_color(self, rgb_tuple):
        """改变背景底色"""
        r, g, b = rgb_tuple
        self.bg_color = [r, g, b, self.bg_color[3]]
        self._apply_bg_color()
        self._mark_dirty()

    def pick_custom_bg_color(self):
        """调用系统原生拾色器选底色"""
        r, g, b, _ = self.bg_color
        color = QColorDialog.getColor(QColor(r, g, b), self, "选择自定义背景色")
        if color.isValid():
            self.change_bg_base_color((color.red(), color.green(), color.blue()))

    def change_bg_opacity(self, pct):
        """响应滑块改变透明度"""
        self.bg_color[3] = int(round(pct * 2.55))
        self._apply_bg_color()
        self._mark_dirty()

    # --- 工具栏辅助 ---

    def _create_tool_btn(self, text, tooltip, callback, style=""):
        """创建一个标准工具栏按钮并添加到 HeaderBar。"""
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.setFixedSize(28, 28)
        btn.setStyleSheet(
            f"QPushButton {{ border: none; border-radius: 4px; font-weight: bold; {style} }}"
            f" QPushButton:hover {{ background-color: rgba(0,0,0,0.1); }}"
        )
        btn.clicked.connect(callback)
        self.header.toolbar_layout.addWidget(btn)
        return btn

    def _build_toolbar(self):
        """构建精简版主工具栏，负责展开二级面板"""
        self.toggle_btns = []
        
        def create_toggle_btn(text, tooltip, index, style=""):
            btn = QPushButton(text)
            btn.setToolTip(tooltip)
            btn.setFixedSize(28, 28)
            btn.setCheckable(True) 
            btn.setStyleSheet(
                f"QPushButton {{ border: none; border-radius: 4px; font-weight: bold; {style} }}"
                f"QPushButton:hover {{ background-color: rgba(0,0,0,0.1); }}"
                f"QPushButton:checked {{ background-color: rgba(0,0,0,0.2); border: 1px solid #999; }}"
            )
            btn.clicked.connect(lambda: self._on_main_tool_clicked(btn, index))
            self.header.toolbar_layout.addWidget(btn)
            self.toggle_btns.append(btn)
            return btn

        create_toggle_btn("Aa", "字体与样式", 0)
        create_toggle_btn("A", "字体颜色", 1, "color: blue;")
        palette_btn = create_toggle_btn(icon("palette"), "便签外观", 2, "color: #555; font-weight: normal;")
        set_icon_font(palette_btn, 14)
        
        self._create_tool_btn("☑", "插入待办事项", self.insert_todo, "color: #e67e22;")
        # 截图和插入图片按钮
        self.screenshot_btn = self._create_tool_btn(
            icon("content_cut"), "区域截图",
            self._start_screenshot, "color: #555; font-weight: normal;"
        )
        set_icon_font(self.screenshot_btn, 14)
        self.image_btn = self._create_tool_btn(
            icon("insert_photo"), "插入图片",
            self._insert_image, "color: #555; font-weight: normal;"
        )
        set_icon_font(self.image_btn, 14)

        # 导出文件：普通模式导出 .doc，MD 模式导出 .md
        self.export_btn = self._create_tool_btn(
            icon("download"), "导出文件",
            self._export_note, "color: #555; font-weight: normal;"
        )
        set_icon_font(self.export_btn, 14)

        # Markdown 模式切换按钮（事务追踪器/新番便签无此功能）
        if not self._is_special_note():
            self.md_btn = QPushButton("Md")
            self.md_btn.setToolTip("转为 Markdown 便签（左源码右实时渲染）")
            self.md_btn.setFixedSize(28, 28)
            self.md_btn.setCheckable(True)
            self.md_btn.setStyleSheet(
                "QPushButton { border: none; border-radius: 4px; font-size: 11px;"
                " font-weight: bold; color: #555; }"
                " QPushButton:hover { background-color: rgba(0,0,0,0.1); }"
                " QPushButton:checked { background-color: #E8F4FD; color: #0078D7;"
                " border: 1px solid #B5D4F4; }"
            )
            self.md_btn.clicked.connect(self._toggle_markdown_mode)
            self.header.toolbar_layout.addWidget(self.md_btn)

            # 源码显示/隐藏按钮（仅 MD 模式可见；眼睛睁开=显示源码，闭眼=隐藏）
            self.src_vis_btn = QPushButton(icon("visibility"))
            set_icon_font(self.src_vis_btn, 14)
            self.src_vis_btn.setToolTip("隐藏源码")
            self.src_vis_btn.setFixedSize(28, 28)
            self.src_vis_btn.setCheckable(True)
            self.src_vis_btn.setStyleSheet(
                "QPushButton { border: none; border-radius: 4px; color: #555; }"
                " QPushButton:hover { background-color: rgba(0,0,0,0.1); }"
                " QPushButton:checked { background-color: #E8F4FD; color: #0078D7;"
                " border: 1px solid #B5D4F4; }"
            )
            self.src_vis_btn.clicked.connect(self._toggle_src_visible)
            self.header.toolbar_layout.addWidget(self.src_vis_btn)
            self.src_vis_btn.hide()  # 默认隐藏，进入 MD 模式后显示

    def _update_src_vis_btn(self, visible):
        """眼睛按钮状态：源码可见=睁眼，隐藏=闭眼（含锁定强制隐藏）。"""
        if not hasattr(self, 'src_vis_btn') or self.src_vis_btn is None:
            return
        self.src_vis_btn.setText(icon("visibility" if visible else "visibility_off"))
        self.src_vis_btn.setToolTip("隐藏源码" if visible else "显示源码")
        self.src_vis_btn.setChecked(not visible)

    def _toggle_src_visible(self):
        """工具栏眼睛按钮：切换源码显示/隐藏（窗口宽度同步切半/恢复）。"""
        md = self.editor_host.md_view
        md.set_user_hidden_src(md.src_visible_state())
        self._mark_dirty()
        self.save_data()

    def _is_special_note(self):
        """事务追踪器、新番便签与日程表：无 Markdown 切换能力。"""
        return (self.note_id == "bangumi_schedule"
                or self.note_id.startswith("habit_")
                or self.note_id.startswith("schedule_"))

    def _on_main_tool_clicked(self, clicked_btn, index):
        """处理主工具栏点击：互斥展开/收起二级面板"""
        for btn in self.toggle_btns:
            if btn != clicked_btn:
                btn.setChecked(False) # 弹起其他按钮
                
        if clicked_btn.isChecked():
            self.format_panel.stack.setCurrentIndex(index)
            self.format_panel.setVisible(True)
        else:
            self.format_panel.setVisible(False)

    def _init_bangumi_mode(self):
        """将当前窗口初始化为 Bangumi 新番便签模式（只读、蓝色主题）。

        仅首次创建时套用蓝色主题；已有存档则保留用户自定义的外观。
        """
        self.is_locked = True
        if not (self.save_file and os.path.exists(self.save_file)):
            self.is_always_on_top = False
            self.apply_window_states()   # 属性改了必须同步窗口 flag，否则置顶状态错乱
            self.bg_color = [235, 245, 255, 242]
            self._apply_bg_color()
        self._apply_lock_ui()
        # Bangumi 便签需保留链接点击能力，覆盖 _apply_lock_ui 的 NoTextInteraction
        self.text_edit.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.text_edit.setOpenExternalLinks(True)
        self.header.toolbar_container.hide()

        self.refresh_btn = QPushButton(icon("refresh"), self.header)
        set_icon_font(self.refresh_btn, 14)
        self.refresh_btn.setToolTip("立即同步新番日历")
        self.refresh_btn.setFixedSize(22, 22)
        self.refresh_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; font-size: 14px; }"
            " QPushButton:hover { background-color: rgba(0,0,0,0.1); border-radius: 4px; }"
        )
        self.refresh_btn.clicked.connect(lambda: global_signaler.force_sync_bangumi_signal.emit())
        self.header.title_layout.insertWidget(self.header.title_layout.count() - 1, self.refresh_btn)

    # --- 格式化操作 ---

    def insert_todo(self):
        """在当前光标位置插入一个待办项。"""
        if getattr(self, 'editor_host', None) and self.editor_host.is_md:
            # MD 模式：在源码光标处插入任务语法，右侧实时渲染
            cur = self.editor_host.md_view.src.textCursor()
            if cur.positionInBlock() > 0:
                cur.insertBlock()
            cur.insertText("- [ ] ")
            self.editor_host.md_view.src.setFocus()
            self._mark_dirty()
            return
        cursor = self.text_edit.textCursor()
        if cursor.positionInBlock() > 0:
            cursor.insertBlock()
        fmt = cursor.charFormat()
        fmt.setFontStrikeOut(False)
        fmt.setForeground(QColor("#333333"))
        cursor.setCharFormat(fmt)
        cursor.insertText("☐ ")
        self.text_edit.setFocus()
        self._mark_dirty()

    def toggle_bold(self):
        fmt = self.text_edit.currentCharFormat()
        fmt.setFontWeight(QFont.Bold if fmt.fontWeight() != QFont.Bold else QFont.Normal)
        self.text_edit.mergeCurrentCharFormat(fmt)
        self._mark_dirty()

    def toggle_italic(self):
        fmt = self.text_edit.currentCharFormat()
        fmt.setFontItalic(not fmt.fontItalic())
        self.text_edit.mergeCurrentCharFormat(fmt)
        self._mark_dirty()

    def toggle_underline(self):
        fmt = self.text_edit.currentCharFormat()
        fmt.setFontUnderline(not fmt.fontUnderline())
        self.text_edit.mergeCurrentCharFormat(fmt)
        self._mark_dirty()

    def change_font_color(self):
        color = QColorDialog.getColor(self.text_edit.textColor(), self, "选择字体颜色")
        if color.isValid():
            self.text_edit.setTextColor(color)
            self._mark_dirty()

    # --- 便签生命周期 ---

    def create_new_note(self):
        """从当前便签创建新的同级便签。"""
        new_note = AniNoteWindow()
        new_note.move(self.x() + 40, self.y() + 40)
        new_note.animated_show()
        new_note.activateWindow()
        new_note.save_data()
        global_signaler.note_updated_signal.emit()

    def delete_note(self, confirm=True):
        """删除当前便签（可选确认对话框）。"""
        if confirm:
            reply = QMessageBox.question(
                self, '删除确认',
                '确定要彻底删除这个便签吗？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        if self in ACTIVE_NOTES:
            ACTIVE_NOTES.remove(self)
        self._deleted = True
        stacks.STACKS.unregister(self)   # 从便签集里摘掉（剩余不足两条则解散）
        global_signaler.unregister_note_hotkey.emit(self.note_id)
        if self.save_file and os.path.exists(self.save_file):
            # 删除保存文件
            os.remove(self.save_file)
            # 如果是文件夹模式（data.json 在子目录中），连目录一起删
            note_dir = os.path.dirname(self.save_file)
            if note_dir != SAVE_DIR and os.path.isdir(note_dir):
                import shutil
                # ignore_errors：目录内有临时/只读残留文件时也保证删干净，不留空壳
                shutil.rmtree(note_dir, ignore_errors=True)
        self.close()
        global_signaler.note_updated_signal.emit()

    def _open_note_hotkey_dialog(self):
        """打开便签设置对话框（无边框圆角 + 阴影 + 可拖拽标题栏，对齐便签弹窗风格）。

        通用内容：便签专属快捷键；子类可通过 _append_extra_settings / _save_extra_settings
        扩展附加设置（如日程表的起始周）。
        """
        dlg = QDialog(self)
        dlg.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dlg.setAttribute(Qt.WA_TranslucentBackground)
        dlg.setFixedSize(430, 370)
        dlg.setStyleSheet(
            "QFrame#note_dlg_bg { background: #FAFAFA; border-radius: 12px;"
            " border: 1px solid #EAEAEA; }"
        )

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(12, 12, 12, 12)
        dlg_bg = QFrame()
        dlg_bg.setObjectName("note_dlg_bg")
        shadow = QGraphicsDropShadowEffect(dlg)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 40))
        shadow.setOffset(0, 6)
        dlg_bg.setGraphicsEffect(shadow)
        outer.addWidget(dlg_bg)

        layout = QVBoxLayout(dlg_bg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 自定义标题栏（可拖拽）
        dlg_bar = QFrame()
        dlg_bar.setStyleSheet("background: transparent;")
        dlg_bar.setFixedHeight(45)
        bar_layout = QHBoxLayout(dlg_bar)
        bar_layout.setContentsMargins(20, 0, 10, 0)
        dlg_title = QLabel("便签设置")
        # 字重走 QFont 真实字面（QSS 的 font-weight 会触发 Qt 合成且字体族不生效）
        dlg_title.setStyleSheet("color: #333;")
        dlg_title.setFont(fonts_mod.make_font(px=15, weight="bold"))
        bar_layout.addWidget(dlg_title)
        bar_layout.addStretch()
        dlg_close = QPushButton(icon("close"))
        set_icon_font(dlg_close, 16)
        dlg_close.setFixedSize(36, 30)
        dlg_close.setStyleSheet(
            "QPushButton { border: none; border-radius: 6px; background-color: transparent;"
            " font-size: 14px; color: #555; }"
            " QPushButton:hover { background-color: #E81123; color: white; }"
        )
        dlg_close.clicked.connect(dlg.reject)
        bar_layout.addWidget(dlg_close)
        layout.addWidget(dlg_bar)

        dlg_bar._drag_pos = None

        def _bar_press(e):
            if e.button() == Qt.LeftButton:
                dlg_bar._drag_pos = e.globalPosition().toPoint() - dlg.pos()
                e.accept()
        def _bar_move(e):
            if dlg_bar._drag_pos is not None:
                dlg.move(e.globalPosition().toPoint() - dlg_bar._drag_pos)
                e.accept()
        def _bar_release(e):
            dlg_bar._drag_pos = None
        dlg_bar.mousePressEvent = _bar_press
        dlg_bar.mouseMoveEvent = _bar_move
        dlg_bar.mouseReleaseEvent = _bar_release

        content = QVBoxLayout()
        content.setContentsMargins(24, 6, 24, 18)
        content.setSpacing(12)
        layout.addLayout(content, 1)

        input_style = (
            "QLineEdit, QDateEdit { padding: 8px 12px;"
            " border: 1px solid #D0D0D0; border-radius: 8px; font-size: 14px;"
            " background: #FFFFFF; }"
            " QLineEdit:focus, QDateEdit:focus { border-color: #1A73E8; }"
        )
        btn_style = (
            "QPushButton { padding: 8px 22px; border: 1px solid #D0D0D0;"
            " border-radius: 8px; background: #FFFFFF; font-size: 13px; color: #555; }"
            " QPushButton:hover { background: #F0F0F0; border-color: #B0B0B0; }"
        )
        ok_style = (
            "QPushButton { padding: 8px 26px; border: none; border-radius: 8px;"
            " background: #1A73E8; font-size: 13px; color: #FFFFFF; font-weight: 600; }"
            " QPushButton:hover { background: #1765CC; }"
            " QPushButton:pressed { background: #1557B0; }"
        )

        info = QLabel(f"便签「{self.header.title_edit.text()}」专属快捷键")
        info.setStyleSheet("font-size: 13px; color: #555; font-weight: 600;")
        content.addWidget(info)

        current = getattr(self, '_note_hotkey', '')
        input_field = QLineEdit(current)
        input_field.setPlaceholderText("例：alt+1")
        input_field.setStyleSheet(input_style)
        content.addWidget(input_field)

        hint = QLabel("留空则不设置快捷键")
        hint.setStyleSheet("font-size: 12px; color: #999;")
        content.addWidget(hint)

        # 子类附加设置（日程表：起始周）
        self._append_extra_settings(content)

        content.addStretch(1)

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet(btn_style)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("确定")
        ok_btn.setStyleSheet(ok_style)
        btn_row.addWidget(ok_btn)
        content.addLayout(btn_row)

        def do_register():
            text = input_field.text().strip()
            global_signaler.unregister_note_hotkey.emit(self.note_id)
            self._note_hotkey = text
            if text:
                def on_conflict_result(conflicts):
                    if conflicts:
                        names = "」、「".join(conflicts)
                        reply = QMessageBox.question(
                            dlg, '快捷键已存在',
                            f'快捷键已被「{names}」使用，要添加到此便签吗？',
                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                        )
                        if reply != QMessageBox.Yes:
                            self._mark_dirty()
                            dlg.reject()
                            return
                    self._save_extra_settings()
                    global_signaler.register_note_hotkey.emit(self.note_id, text)
                    self._mark_dirty()
                    dlg.accept()
                global_signaler.check_hotkey_conflict.emit(text, on_conflict_result)
            else:
                self._save_extra_settings()
                self._mark_dirty()
                dlg.accept()

        ok_btn.clicked.connect(do_register)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.exec()

    def _append_extra_settings(self, content):
        """子类扩展设置行（默认无）。"""

    def _save_extra_settings(self):
        """子类保存扩展设置（默认无操作）。"""

    # ---------- 图片管理 ----------

    def _note_image_dir(self):
        """返回便签的图片存储目录，按需创建。"""
        # 用 save_file 所在目录（如果已保存）
        if self.save_file:
            img_dir = os.path.dirname(self.save_file)
            if not os.path.isdir(img_dir):
                os.makedirs(img_dir, exist_ok=True)
        else:
            # 尚未保存：用标题推算
            base = sanitize_filename(self.header.title_edit.text())
            img_dir = os.path.join(SAVE_DIR, base)
            os.makedirs(img_dir, exist_ok=True)
        return img_dir

    def _insert_image(self):
        """从文件选择器插入图片。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.tiff *.svg);;所有文件 (*.*)"
        )
        if not path:
            return
        self._insert_image_file(path)

    def _insert_image_file(self, src_path):
        """将图片复制到便签目录并插入光标处。"""
        img_dir = self._note_image_dir()
        ext = os.path.splitext(src_path)[1].lower() or ".png"
        ts = int(time.time() * 1000)
        name = f"img_{ts:013d}{ext}"
        dest = os.path.join(img_dir, name)

        if ext == ".svg":
            raster, _ = QSvgRenderer.load(src_path)
            if raster and not raster.isNull():
                raster.save(dest, "PNG")
            shutil.copy2(src_path, os.path.join(img_dir, f"img_{ts:013d}.svg"))
        else:
            shutil.copy2(src_path, dest)

        if getattr(self, 'editor_host', None) and self.editor_host.is_md:
            # MD 模式：插入 Markdown 图片语法，右侧实时渲染
            rel = os.path.basename(dest).replace(os.sep, '/')
            # 同步 base_dir 到实际图片目录（新建便签未保存时 save_file 为空，渲染会缺 base_dir）
            self.editor_host.md_view.set_base_dir(img_dir.replace(os.sep, '/'))
            self.editor_host.md_view.insert_image_syntax(rel)
            self._mark_dirty()
            return

        cursor = self.text_edit.textCursor()
        url = f"file:///{dest.replace(os.sep, '/')}"
        cursor.insertHtml(
            f'<img src="{url}" '
            f'style="max-width:100%; max-height:400px;" '
            f'title="双击查看原图">'
        )
        self._mark_dirty()

    # --- 导出文件 ---

    def _export_note(self):
        """工具栏导出：MD 模式导出 .md，普通模式导出 .doc。"""
        if getattr(self, 'editor_host', None) and self.editor_host.is_md:
            self._export_note_md()
        else:
            self._export_note_doc()

    def _export_dir(self):
        """解析导出目录（默认「导出的便签文本」，可配置）。"""
        cfg = load_config()
        raw = cfg.get("export_dir", "default")
        if raw == "default" or not raw:
            return EXPORT_DIR
        return raw

    def _export_note_doc(self):
        """导出为 Word 文档（.doc，HTML 格式，与 app.py 导出一致）。"""
        title = self.header.title_edit.text()
        html = self.text_edit.toHtml()
        note_dir = self._note_image_dir().replace('\\', '/')

        def _unfile(m):
            raw = urllib.parse.unquote(m.group(1))
            for prefix in ('file:///', 'file://', 'file:'):
                if raw.startswith(prefix):
                    raw = raw[len(prefix):]
                    break
            try:
                rel = os.path.relpath(raw, note_dir.replace('/', os.sep))
                return f'src="{rel.replace(os.sep, "/")}"'
            except ValueError:
                return m.group(0)

        html = re.sub(r'src="(file://[^"]+)"', _unfile, html)
        if not html.strip():
            QMessageBox.information(self, "导出", "该便签没有文字内容，跳过导出。")
            return
        full_doc = (
            '<html xmlns:o="urn:schemas-microsoft-com:office:office"'
            ' xmlns:w="urn:schemas-microsoft-com:office:word"'
            ' xmlns="http://www.w3.org/TR/REC-html40">'
            f'<head><meta charset="utf-8"><title>{title}</title></head>'
            f'<body>{html}</body></html>'
        )
        export_dir = self._export_dir()
        os.makedirs(export_dir, exist_ok=True)
        safe_title = title.translate(_SAFE_TRANS)
        path = os.path.join(export_dir, f"{safe_title}.doc")
        with open(path, "w", encoding="utf-8") as f:
            f.write(full_doc)
        QMessageBox.information(self, "导出成功", f"已导出至：\n{path}")

    def _export_note_md(self):
        """导出为 Markdown（.md）：取当前源码，图片相对路径转 file:// 绝对。"""
        title = self.header.title_edit.text()
        md_text = self.editor_host.md_view.markdown_text()
        note_dir = self._note_image_dir().replace('\\', '/')

        def _abs(m):
            rel = m.group(2)
            if rel.startswith(("http://", "https://", "file:", "data:")):
                return m.group(0)
            return f"![{m.group(1)}](file:///{note_dir}/{rel})"

        md_text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _abs, md_text)
        if not md_text.strip():
            QMessageBox.information(self, "导出", "该便签没有文字内容，跳过导出。")
            return
        export_dir = self._export_dir()
        os.makedirs(export_dir, exist_ok=True)
        safe_title = title.translate(_SAFE_TRANS)
        path = os.path.join(export_dir, f"{safe_title}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(md_text)
        QMessageBox.information(self, "导出成功", f"已导出至：\n{path}")

    def _start_screenshot(self):
        """启动区域截图。"""
        self.hide()  # 隐藏便签避免截到自己
        QApplication.processEvents()
        QTimer.singleShot(200, self._do_region_screenshot)

    def _do_region_screenshot(self):
        """弹出区域截图遮罩。"""
        screen = QApplication.primaryScreen()
        if not screen:
            self.show()
            return
        pixmap = screen.grabWindow(0)
        self._screenshot_overlay = RegionSelector(pixmap)
        self._screenshot_overlay.selected.connect(self._on_region_selected)
        self._screenshot_overlay.showFullScreen()

    def _on_region_selected(self, cropped_pixmap):
        """区域截图完成，插入到便签。"""
        self.show()
        if cropped_pixmap.isNull():
            return
        img_dir = self._note_image_dir()
        ts = int(time.time() * 1000)
        name = f"screenshot_{ts:013d}.png"
        dest = os.path.join(img_dir, name)
        ok = cropped_pixmap.save(dest, "PNG")
        if not ok:
            return
        if getattr(self, 'editor_host', None) and self.editor_host.is_md:
            # MD 模式：插入 Markdown 图片语法，右侧实时渲染
            rel = os.path.basename(dest).replace(os.sep, '/')
            self.editor_host.md_view.insert_image_syntax(rel)
            self._mark_dirty()
            self.raise_()
            self.activateWindow()
            return

        cursor = self.text_edit.textCursor()
        url = f"file:///{dest.replace(os.sep, '/')}"
        cursor.insertHtml(
            f'<img src="{url}" '
            f'style="max-width:100%; max-height:400px;" '
            f'title="双击查看原图">'
        )
        self._mark_dirty()
        self.raise_()
        self.activateWindow()

    # ---------- 双击查看大图 ----------

    def eventFilter(self, obj, event):
        """拦截 QTextEdit 内双击图片事件。"""
        if obj == self.text_edit.viewport() and event.type() == QEvent.MouseButtonDblClick:
            cursor = self.text_edit.cursorForPosition(event.pos())
            fmt = cursor.charFormat()
            if fmt.isImageFormat():
                img_fmt = fmt.toImageFormat()
                if img_fmt.name():
                    self._show_full_image(img_fmt.name())
                    return True
        return super().eventFilter(obj, event)

    def _show_full_image(self, src):
        """弹出窗口显示原图。"""
        # 处理 file:// 协议
        if src.startswith("file:///"):
            full_path = urllib.parse.unquote(src[8:])  # 去掉 file:///
            full_path = full_path.replace('/', os.sep)
        else:
            img_dir = self._note_image_dir()
            full_path = os.path.join(img_dir, src)
        if not os.path.exists(full_path):
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("图片预览")
        dlg.setMinimumSize(200, 200)
        dlg.setStyleSheet(
            "QDialog { background-color: #1E1E1E; }"
            " QLabel { background: transparent; }"
        )
        pixmap = QPixmap(full_path)
        if pixmap.isNull():
            return
        screen = QApplication.primaryScreen().geometry()
        max_w = int(screen.width() * 0.85)
        max_h = int(screen.height() * 0.85)
        if pixmap.width() > max_w or pixmap.height() > max_h:
            pixmap = pixmap.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        lbl = QLabel(pixmap=pixmap)
        lbl.setScaledContents(False)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(lbl)
        dlg.adjustSize()
        dlg.exec()

    def _open_image_external(self, src):
        """用系统默认看图程序打开文件夹内存储的原图（MD 预览双击）。

        MD 渲染的缩略图不够清晰，直接打开磁盘上的原图文件供查看。
        """
        if src.startswith("file:///"):
            full_path = urllib.parse.unquote(src[len("file:///"):])
            full_path = full_path.replace('/', os.sep)
        elif src.startswith("file://"):
            full_path = urllib.parse.unquote(src[len("file://"):])
            full_path = full_path.replace('/', os.sep)
        else:
            full_path = os.path.join(self._note_image_dir(), src)
        if os.path.exists(full_path):
            try:
                os.startfile(full_path)
            except OSError:
                pass

    def show_context_menu(self, pos):
        """构建并显示右键上下文菜单。"""
        # 坐标基准：信号来自哪个控件就用哪个控件映射（MD 源码区/预览区相对窗口有偏移）
        sender = self.sender()
        if isinstance(sender, QWidget) and sender is not self:
            global_pos = sender.mapToGlobal(pos)
        else:
            global_pos = self.mapToGlobal(pos)

        menu = QMenu(self)
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setStyleSheet(
            "QMenu {"
            " background-color: #FAFAFA;"
            " border: 1px solid #E0E0E0;"
            " border-radius: 10px;"
            " padding: 6px;"
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

        lock_action = None
        if not getattr(self, 'is_collapsed', False):
            # 折叠态不提供锁定入口：那一条里没有工具栏，锁定/解锁只会造成显示错乱
            lock_action = menu.addAction(
                "解除锁定" if self.is_locked else "锁定便签 (防误触)"
            )
        top_action = menu.addAction(
            "取消置顶" if self.is_always_on_top else "置顶"
        )
        collapse_action = menu.addAction(
            "展开便签" if getattr(self, 'is_collapsed', False) else "折叠便签（收成一条）"
        )
        open_panel_action = menu.addAction("打开控制台")
        menu.addSeparator()

        hide_single_action = menu.addAction("暂时隐藏此便签")
        del_action = menu.addAction("删除此便签")
        menu.addSeparator()

        md_action = None
        hide_src_action = None
        if not self._is_special_note():
            md_action = menu.addAction(
                "转回普通便签" if self.editor_host.is_md else "转为 Markdown 便签"
            )
            # 锁定时强制只展示渲染，不提供显示源码入口（避免切宽状态错乱）
            if self.editor_host.is_md and not self.is_locked:
                show_src = self.editor_host.md_view.src_visible_state()
                hide_src_action = menu.addAction(
                    "隐藏源码" if show_src else "显示源码"
                )
        menu.addSeparator()

        cfg = load_config()
        hide_action = menu.addAction(
            f"隐藏全部便签 ({cfg['toggle_hotkey'].upper()})"
        )
        show_all_action = menu.addAction("显示全部便签")

        action = menu.exec(global_pos)

        # 注意：菜单外左键关闭时 exec 返回 None。
        # 特殊便签的 md_action / hide_src_action / 折叠态的 lock_action 为 None，
        # 直接用 `action == xxx` 会因 None == None 误判触发 → 必须判空。
        if lock_action is not None and action == lock_action:
            self._toggle_lock()
        elif action == top_action:
            self._toggle_always_on_top()
        elif action == collapse_action:
            self.toggle_collapse()
        elif action == hide_single_action:
            self.is_hidden = True
            self.save_data()
            self.animated_hide()
        elif action == open_panel_action:
            global_signaler.open_panel_signal.emit()
        elif action == del_action:
            self.delete_note()
        elif md_action is not None and action == md_action:
            self._toggle_markdown_mode()
        elif hide_src_action is not None and action == hide_src_action:
            self.editor_host.md_view.set_user_hidden_src(
                self.editor_host.md_view.src_visible_state()
            )
            self.save_data()
        elif action == hide_action:
            toggle_all_notes()
        elif action == show_all_action:
            show_all_notes()

    def _apply_lock_ui(self):
        """将当前 is_locked 状态同步到所有 UI 控件。消除 load_data 与 _toggle_lock 的重复代码。"""
        locked = self.is_locked
        self.header.toolbar_container.setVisible(not locked)
        self.header.drag_handle.setVisible(not locked)
        for g in self._grips:
            g.setVisible(not locked)
        self.header.title_edit.setReadOnly(locked)
        self.text_edit.setReadOnly(locked)
        if hasattr(self, 'editor_host'):
            self.editor_host.md_view.set_locked(locked)
        if locked:
            self.text_edit.setTextInteractionFlags(Qt.NoTextInteraction)
            self.header.title_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.text_edit.clearFocus()
            self.header.title_edit.clearFocus()
        else:
            self.text_edit.setTextInteractionFlags(Qt.TextEditorInteraction)
            self.header.title_edit.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        # 折叠态：窗口只有"一条"，工具栏与内容一律不显示（无论锁定与否），
        # 标题设为只读且不响应鼠标，避免误进编辑态（整条拖动由窗口级鼠标事件负责）
        if getattr(self, 'is_collapsed', False):
            self._enforce_collapsed_ui()
            # 子类重写的 _apply_lock_ui 会在 super() 之后再把内容显出来 → 延后一拍再收敛
            QTimer.singleShot(0, self._enforce_collapsed_ui)

    # ---------- 便签显示 / 隐藏淡入淡出动画 ----------

    def _ensure_opacity_anim(self):
        """惰性创建并复用同一个透明度动画对象（避免动画对象堆积）。"""
        if getattr(self, '_opacity_anim', None) is None:
            anim = QPropertyAnimation(self, b"windowOpacity", self)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.finished.connect(self._on_opacity_anim_done)
            self._opacity_anim = anim
        return self._opacity_anim

    def _on_opacity_anim_done(self):
        """淡出结束才真正 hide；任何情况都把不透明度复位，避免残留半透明。"""
        try:
            if getattr(self, '_fade_out_pending', False):
                self._fade_out_pending = False
                self.hide()
            self.setWindowOpacity(1.0)
        except RuntimeError:
            pass

    def animated_show(self):
        """带淡入的显示。已可见且未在淡出时只前置，不重复淡入。"""
        pending_out = getattr(self, '_fade_out_pending', False)
        if self.isVisible() and not pending_out:
            self.setWindowOpacity(1.0)
            self.raise_()
            return
        self._fade_out_pending = False
        anim = self._ensure_opacity_anim()
        anim.stop()
        if self.isVisible():
            # 正在淡出中被打断 → 直接淡回，避免动画结束后又被 hide
            anim.setDuration(ANIM_FADE_IN_MS)
            anim.setStartValue(float(self.windowOpacity()))
            anim.setEndValue(1.0)
            anim.start()
            self.raise_()
            return
        self.setWindowOpacity(0.0)   # 先透明再 show，避免闪一帧全亮
        self.show()
        anim.setDuration(ANIM_FADE_IN_MS)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.start()

    def animated_hide(self):
        """带淡出的隐藏（动画结束才真正 hide）。"""
        if not self.isVisible():
            self.setWindowOpacity(1.0)
            return
        anim = self._ensure_opacity_anim()
        anim.stop()
        self._fade_out_pending = True
        anim.setDuration(ANIM_FADE_OUT_MS)
        anim.setStartValue(float(self.windowOpacity()))
        anim.setEndValue(0.0)
        anim.start()

    # ---------- 便签折叠（收成一条，仅显示标题 / 底色 / 透明度）----------

    def _collapsed_height(self):
        """折叠条的基准高度（不含悬停预览行）：标题行 + 各级边距。

        结果缓存到 `_strip_h`（样式变化时由 set_collapsed 置 0 失效），
        计算时临时隐藏悬停预览行，避免它把基准高度算大。
        """
        cached = getattr(self, '_strip_h', 0)
        if cached > 0:
            return cached
        peek = getattr(self, '_peek_lbl', None)
        peek_was_visible = bool(peek is not None and peek.isVisible())
        try:
            if peek_was_visible:
                peek.hide()
            lay = self.bg_frame.layout()
            if lay is not None:
                lay.invalidate()
                lay.activate()
            h = self.bg_frame.sizeHint().height()
            win_lay = self.layout()
            if win_lay is not None:
                m = win_lay.contentsMargins()
                h += m.top() + m.bottom()
        finally:
            if peek_was_visible:
                peek.show()
        self._strip_h = max(int(h), _COLLAPSED_MIN_HEIGHT)
        return self._strip_h

    def _set_collapse_margins(self, collapsed):
        """折叠时收紧窗口与卡片上下边距，让那一条更薄、可点面积更大；展开时还原。"""
        win_lay = self.layout()
        frame_lay = self.bg_frame.layout()
        if collapsed:
            if win_lay is not None:
                m = win_lay.contentsMargins()
                self._saved_win_margins = (m.left(), m.top(), m.right(), m.bottom())
                win_lay.setContentsMargins(m.left(), _COLLAPSED_V_MARGIN,
                                           m.right(), _COLLAPSED_V_MARGIN)
            if frame_lay is not None:
                m = frame_lay.contentsMargins()
                self._saved_frame_margins = (m.left(), m.top(), m.right(), m.bottom())
                frame_lay.setContentsMargins(m.left(), _COLLAPSED_V_MARGIN,
                                             m.right(), _COLLAPSED_V_MARGIN)
        else:
            saved = getattr(self, '_saved_win_margins', None)
            if saved and win_lay is not None:
                win_lay.setContentsMargins(*saved)
                self._saved_win_margins = None
            saved = getattr(self, '_saved_frame_margins', None)
            if saved and frame_lay is not None:
                frame_lay.setContentsMargins(*saved)
                self._saved_frame_margins = None

    def _hide_content_for_collapse(self):
        """折叠：先记录各内容控件可见性快照，再隐藏 header 之外的一切。

        统一按 bg_frame 布局遍历，四类便签（普通 / MD / 事务追踪器 / 日程表 / 新番）通用。
        """
        snap = {}
        lay = self.bg_frame.layout()
        if lay is not None:
            for i in range(lay.count()):
                item = lay.itemAt(i)
                w = item.widget() if item is not None else None
                if w is None or w is self.header:
                    continue
                snap[w] = not w.isHidden()
        self._enforce_collapsed_ui()
        return snap

    def _enforce_collapsed_ui(self):
        """折叠态的显示不变量：除标题行外一律不显示（幂等，可重复调用）。

        特殊便签的内容容器是在父类 __init__ 之后才插进布局的，子类重写的
        `_apply_lock_ui`（在 super() 之后执行）也可能把内容再显出来，
        所以收敛动作必须能被重复触发。
        """
        if not getattr(self, 'is_collapsed', False):
            return
        try:
            peek_lbl = getattr(self, '_peek_lbl', None)
            lay = self.bg_frame.layout()
            if lay is not None:
                for i in range(lay.count()):
                    item = lay.itemAt(i)
                    w = item.widget() if item is not None else None
                    if w is None or w is self.header or w is peek_lbl:
                        continue    # 悬停预览行由 _peeking 控制，不在这里压掉
                    w.hide()
            self.header.toolbar_container.hide()
            self.header.drag_handle.hide()
            for g in self._grips:
                g.hide()
            self.header.title_edit.setReadOnly(True)
            self.header.title_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        except RuntimeError:
            pass

    def showEvent(self, event):
        """首次显示时落位待恢复的折叠状态（load_data 只打标记，原因见那里）。

        特殊便签的内容容器与最小尺寸要等子类 __init__ 走完才成立；若在
        load_data 里就收成一条，随后子类的 setMinimumSize(500/520/420, …)
        会把最小高度顶回去，出现"标题变小、+ 号在、内容照旧显示"的错乱。
        """
        super().showEvent(event)
        if getattr(self, '_pending_collapse', False):
            self._pending_collapse = False
            self.set_collapsed(True, animate=False, save=False)
        # 显示后交给便签集管理器归组（批量加载时它自己防抖，只重建一次）
        stacks.STACKS.on_note_shown(self)

    def _set_title_stretch(self, collapsed):
        """调整标题行拉伸因子，决定标题能显示多宽。

        标题行右侧有个 stretch（把折叠按钮顶到最右）。
        - 展开态：标题 0 / 空白 1。实测标题给 stretch 反而会被 Qt 压缩
          （标题=3/空白=1 时标题只有 206px，标题=0/空白=1 时有 274px），
          所以这里保持标题不吃 stretch，靠缩小拖拽点心把宽度让给标题。
        - 折叠态：手柄已隐藏，标题直接吃满整条（STRETCH 1 / 空白 0）。
        """
        if collapsed:
            # 折叠条：标题吃满整条，解除展开态的定宽限制
            self.header.title_edit.setMinimumWidth(0)
            self.header.title_edit.setMaximumWidth(_QWIDGETSIZE_MAX)
        lay = self.header.title_layout
        for i in range(lay.count()):
            item = lay.itemAt(i)
            if item is None:
                continue
            if item.spacerItem() is not None:
                lay.setStretch(i, 0 if collapsed else 1)
            elif item.widget() is self.header.title_edit:
                lay.setStretch(i, 1 if collapsed else 0)

    def toggle_collapse(self):
        """右上角按钮：折叠 / 展开便签。"""
        self.set_collapsed(not self.is_collapsed)

    def set_collapsed(self, collapsed, animate=True, save=True):
        """折叠（收成一条）或展开，带高度动画。

        Args:
            collapsed: True=折叠，False=展开。
            animate: False 时直接落位（启动恢复折叠状态用，避免闪动）。
            save: 是否立即落盘。
        """
        collapsed = bool(collapsed)
        if collapsed == getattr(self, 'is_collapsed', False):
            return
        self.is_collapsed = collapsed
        self._strip_h = 0          # 折叠条基准高度重新计算（紧凑样式会改变标题行高）
        self._peeking = False
        if getattr(self, '_peek_lbl', None) is not None:
            self._peek_lbl.hide()
        anim = getattr(self, '_collapse_anim', None)
        if anim is not None:
            try:
                anim.stop()
            except RuntimeError:
                self._collapse_anim = None
        self._unlock_header_height()   # 上一次动画被打断，也别留下固定的标题行高度

        if collapsed:
            self._expanded_size = (self.width(), self.height())
            # 各便签最小尺寸不同（普通 320×280 / 事务 500×320 / 日程 520×360 / 新番 420×320），
            # MD 切半时最小宽还会变 → 折叠前快照，展开时原样还原
            self._saved_min_size = (self.minimumWidth(), self.minimumHeight())
            self._saved_frame_min = (self.bg_frame.minimumWidth(),
                                     self.bg_frame.minimumHeight())
            # 工具栏快照要抢在 _hide_content_for_collapse 之前取：那个收敛动作会顺手
            # 把工具栏也藏起来，之后再读 isHidden() 就恒为 True —— 展开后工具栏再也回不来
            self._collapsed_toolbar_prev = not self.header.toolbar_container.isHidden()
            self._collapsed_prev_visible = self._hide_content_for_collapse()
            self.header.toolbar_container.hide()
            self._set_collapse_margins(True)
            self.bg_frame.setMinimumSize(0, 0)
            self.setMinimumSize(120, _COLLAPSED_MIN_HEIGHT)
            # 折叠态整条可拖动：标题占满整条、隐藏拖拽手柄，标题不响应鼠标避免误进编辑态
            self.header.set_title_compact(True)
            self._set_title_stretch(True)
            self.header.title_edit.setReadOnly(True)
            self.header.title_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.header.title_edit.clearFocus()   # 去掉标题框的聚焦白底，让那一条更干净
            self.header.drag_handle.hide()
            for g in self._grips:
                g.hide()
            # 锁住标题行高度：折叠 / 展开动画期间 bg_frame 里只剩标题行可见，多出来的
            # 高度会被整个灌给标题行 → 标题跟着窗口漂到中间。锁死后它稳稳待在顶部。
            self._lock_header_height()
            target_h = self._collapsed_height()
            self.setMinimumHeight(target_h)   # 先抬最小高度，动画终点即新下限
            self.collapse_btn.setText(icon("add"))
            self.collapse_btn.setToolTip("展开便签")
            self._animate_collapse_height(target_h, animate)
        else:
            expanded_h = (self._expanded_size[1] if self._expanded_size
                          else max(self.height(), 320))
            self.setMaximumHeight(_QWIDGETSIZE_MAX)   # 先解开上限，否则动画长不高
            # 标题先切到展开态样式（顺带算出目标字号），再让字号从折叠态值平滑过渡过去。
            # 否则动画期间每帧 resizeEvent 都会把它顶成"大号粗体"，看着就是"突然变粗"。
            self.header.set_title_compact(False)
            target_px = int(getattr(self.header, '_title_px', self.header.TITLE_PX))
            self.header.animate_title_font(self.header.TITLE_PX_COMPACT, target_px,
                                           ANIM_COLLAPSE_MS if animate else 1)
            self._lock_header_height()
            self.collapse_btn.setText(icon("remove"))
            self.collapse_btn.setToolTip("折叠便签（收成一条）")
            self._animate_collapse_height(int(expanded_h), animate)

        if save and not getattr(self, '_is_loading', False):
            self._mark_dirty()
            self.save_data()

    def _lock_header_height(self):
        """把标题行钉在当前高度，折叠 / 展开动画期间不许它被布局拉伸。

        折叠条里除标题行外的控件全被隐藏，bg_frame 的剩余高度会整个灌给标题行，
        于是标题跟着"长高"的窗口一路漂到中间，动画结束才跳回顶部。
        """
        try:
            h = max(int(self.header.sizeHint().height()), 24)
            self.header.setFixedHeight(h)
            self._header_height_locked = True
        except RuntimeError:
            pass

    def _unlock_header_height(self):
        """解除标题行高度锁定，交回布局自然分配。"""
        if not getattr(self, '_header_height_locked', False):
            return
        self._header_height_locked = False
        try:
            self.header.setMinimumHeight(0)
            self.header.setMaximumHeight(_QWIDGETSIZE_MAX)
        except RuntimeError:
            pass

    def _ensure_collapse_anim(self):
        """惰性创建并复用同一个高度动画对象。

        动画的是 `size` 而不是 `geometry`：geometry 会连带设置窗口位置，而动画
        每帧写入的位置是「动画启动那一刻」的旧值 —— 动画进行中被便签夹重排
        move 到新位置后，下一帧又被拉回旧坐标，整叠位置会累积漂移
        （表现为悬停预览几次之后整叠跑偏、要手动拖回来）。只动 size 时位置
        完全交给排布逻辑掌控。
        """
        if getattr(self, '_collapse_anim', None) is None:
            anim = QPropertyAnimation(self, b"size", self)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.finished.connect(self._on_collapse_anim_done)
            anim.valueChanged.connect(self._on_collapse_anim_step)
            self._collapse_anim = anim
        return self._collapse_anim

    def _on_collapse_anim_step(self, _value):
        """动画每一帧都重排整叠 —— 下方成员实时让位，不再等动画结束才"啪"地跳过去。"""
        try:
            stacks.STACKS.on_geometry_changed(self)
        except RuntimeError:
            pass

    def _on_collapse_anim_done(self):
        self._finish_collapse(self.is_collapsed)
        # 动画结束后再补一次整叠重排。
        # 动画途中各成员的高度是渐变的中间值，期间的重排是按"中间高度"算出来的，
        # 会留下位置偏差；此时高度已经到位，用最终高度重排一次即可把整叠收敛回
        # 正确坐标 —— 少这一步的话，反复悬停预览会让整叠一点点跑偏、要手动拉回。
        try:
            stacks.STACKS.on_geometry_changed(self)
        except RuntimeError:
            pass

    def _install_bg_shadow(self, enabled=True):
        """给便签底板装上 / 卸下投影特效。

        半透明无边框窗口在 Windows 上是"分层窗口"，几何动画期间阴影会把绘制
        区域扩到窗口矩形之外，导致 UpdateLayeredWindowIndirect 失败并刷报错，
        所以折叠 / 展开动画期间先卸下投影，动画结束再装回。
        """
        if not enabled:
            self.bg_frame.setGraphicsEffect(None)   # Qt 会接管并删除旧特效
            self._bg_shadow = None
            return
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(15)
        shadow.setColor(QColor(0, 0, 0, 50))
        shadow.setOffset(0, 4)
        self.bg_frame.setGraphicsEffect(shadow)
        self._bg_shadow = shadow

    def _animate_collapse_height(self, target_h, animate):
        """把窗口高度动画到目标值（宽度与位置都不动）。"""
        start = self.size()
        end = QSize(start.width(), int(target_h))
        if not animate:
            self.resize(end)
            self._finish_collapse(self.is_collapsed)
            return
        # 动画期间卸下底板投影：避免分层窗口的绘制区域超出窗口（见 _install_bg_shadow）
        self._install_bg_shadow(False)
        anim = self._ensure_collapse_anim()
        anim.stop()
        anim.setDuration(ANIM_COLLAPSE_MS)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.start()

    # ---------- 便签集：悬停"抽出"预览 ----------

    def _build_peek_lbl(self):
        """建立折叠条的悬停预览行（正常状态下隐藏）。"""
        lbl = QLabel("")
        lbl.setObjectName("peek_lbl")
        lbl.setStyleSheet(
            "background: transparent; color: #666666; font-size: 11px;"
            " padding: 0 2px; border: none;"
        )
        lbl.setFixedHeight(_PEEK_LBL_H)
        lbl.hide()
        lay = self.bg_frame.layout()
        if lay is not None:
            lay.addWidget(lbl)
        self._peek_lbl = lbl

    def _grid_summary(self):
        """网格类便签（事务 / 日程 / 新番）的摘要文案，供悬停预览使用。"""
        try:
            if hasattr(self, '_events'):
                today = datetime_module.date.today().strftime("%Y-%m-%d")
                today_n = sum(1 for e in self._events if e.get("date") == today)
                return f"{len(self._events)} 个事件 · 今日 {today_n} 个"
            if hasattr(self, '_habits'):
                return f"{len(self._habits)} 个事务"
            if hasattr(self, '_schedule'):
                total = sum(len(v) for v in self._schedule.values())
                return f"追番 {total} 部"
        except Exception:
            pass
        return ""

    def peek_text(self, max_len=42):
        """悬停预览文字：网格便签给摘要，普通便签给正文首行，都没有则退回标题。"""
        line = ""
        if self._is_special_note():
            line = self._grid_summary()
        if not line:
            try:
                txt = self.text_edit.toPlainText().strip()
                for ln in txt.splitlines():
                    ln = ln.strip()
                    if ln:
                        line = ln
                        break
            except Exception:
                line = ""
        if not line:
            line = self._grid_summary()
        if len(line) > max_len:
            line = line[:max_len - 1] + "…"
        if not line:
            line = self.header.title_edit.text().strip()
        return line

    def set_peek(self, on):
        """折叠条"抽出一点"：临时增高一行，显示缩略预览（仅便签集成员）。"""
        if not getattr(self, 'is_collapsed', False):
            return
        on = bool(on)
        if bool(getattr(self, '_peeking', False)) == on:
            return
        self._peeking = on
        if self._peek_lbl is None:
            self._build_peek_lbl()
        target = self._collapsed_height() + (_PEEK_H if on else 0)
        try:
            if on:
                self._peek_lbl.setText(self.peek_text())
            self._peek_lbl.setVisible(on)
            if abs(self.height() - target) <= 1:
                self._finish_collapse(True)     # 高度已到位，仅同步 min/max
            else:
                # 折叠态被 _finish_collapse 锁成 min = max = 原高度，
                # 不先把约束放到能容纳目标高度，动画会被 Qt 夹回原值（抽出永远不生效）
                lo, hi = sorted((target, self.height()))
                self.setMinimumHeight(lo)
                self.setMaximumHeight(hi)
                self._animate_collapse_height(target, True)
        except RuntimeError:
            pass

    def _refresh_hover_peek(self):
        """防抖后的悬停判定：鼠标是否真的还在便签上（含子控件之间移动）。"""
        try:
            if not getattr(self, 'is_collapsed', False):
                return
            inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
            stacks.STACKS.peek(self, inside)
        except RuntimeError:
            pass

    def _schedule_hover_peek(self):
        if self._hover_timer is None:
            self._hover_timer = QTimer(self)
            self._hover_timer.setSingleShot(True)
            self._hover_timer.setInterval(60)
            self._hover_timer.timeout.connect(self._refresh_hover_peek)
        self._hover_timer.start()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._schedule_hover_peek()   # 悬停预览行按需惰性创建

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._schedule_hover_peek()

    def hideEvent(self, event):
        """隐藏 / 显示都会影响便签集的排布（成员不占位、夹子跟着收）。"""
        super().hideEvent(event)
        stacks.STACKS.on_geometry_changed(self)

    def _finish_collapse(self, collapsed):
        """折叠 / 展开动画结束后的收尾：尺寸约束与内容可见性。"""
        try:
            if getattr(self, '_bg_shadow', None) is None and hasattr(self, 'bg_frame'):
                self._install_bg_shadow(True)   # 动画结束把投影装回
            if collapsed:
                # 锁成一条，避免被误拉高
                self.setMinimumHeight(self.height())
                self.setMaximumHeight(self.height())
            else:
                self._set_collapse_margins(False)
                self.header.set_title_compact(False)   # 还原标题字号与粗体
                self._set_title_stretch(False)
                fw, fh = getattr(self, '_saved_frame_min', None) or (300, 260)
                self.bg_frame.setMinimumSize(fw, fh)
                mw, mh = getattr(self, '_saved_min_size', None) or (320, 280)
                self.setMaximumHeight(_QWIDGETSIZE_MAX)
                self.setMinimumSize(mw, mh)
                for w, vis in (self._collapsed_prev_visible or {}).items():
                    try:
                        w.setVisible(vis)
                    except RuntimeError:
                        continue
                self._collapsed_prev_visible = {}
                self._apply_lock_ui()   # 恢复工具栏 / 缩放手柄 / 标题只读态
                self._peeking = False
                if getattr(self, '_peek_lbl', None) is not None:
                    self._peek_lbl.hide()
                # 工具栏可见性按折叠前的状态还原（新番便签常态下不显示工具栏）
                prev = getattr(self, '_collapsed_toolbar_prev', None)
                if prev is not None:
                    self.header.toolbar_container.setVisible(
                        bool(prev) and not self.is_locked)
                    self._collapsed_toolbar_prev = None
                # 展开收尾：标题行恢复由布局自由分配（工具栏回来后它会自然变高）
                self._unlock_header_height()
            # 高度变了 → 让便签集里的上下成员跟着让位
            stacks.STACKS.on_geometry_changed(self)
        except RuntimeError:
            pass

    def _is_collapsed_for_save(self):
        """写盘用的折叠状态：待落位的折叠也算折叠。

        折叠状态在首次显示时才真正落位（特殊便签的内容要等子类建完），
        这期间若因退出等原因触发保存，不能把状态写成未折叠。
        """
        return bool(getattr(self, 'is_collapsed', False)
                    or getattr(self, '_pending_collapse', False))

    def _persist_height(self):
        """写盘用的高度：折叠态返回展开高度，避免用一条的高度覆盖存档。"""
        if self._is_collapsed_for_save() and self._expanded_size:
            return self._expanded_size[1]
        return self.height()

    # ---------- 折叠条整条拖动 ----------
    # 标题行内的拖动由 HeaderBar 负责；卡片边距与窗口留白处的拖动落到窗口自身事件上，
    # 这样"整条任意位置都能拖"，无需再保留那三个点的手柄。

    def mousePressEvent(self, event):
        if getattr(self, 'is_collapsed', False) and event.button() == Qt.LeftButton:
            self._strip_drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = getattr(self, '_strip_drag_pos', None)
        if (getattr(self, 'is_collapsed', False) and pos is not None
                and (event.buttons() & Qt.LeftButton)):
            self.move(event.globalPosition().toPoint() - pos)
            stacks.STACKS.on_drag_moved(self)        # 叠内实时让位
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if getattr(self, '_strip_drag_pos', None) is not None:
            self._strip_drag_pos = None
            self.save_data()   # 拖完落盘新位置
            stacks.STACKS.on_drag_released(self)     # 吸附 / 排序 / 拆出
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # --- Markdown 模式切换 ---

    def _apply_md_toolbar(self, enable):
        """MD 模式：隐藏字体/颜色按钮（源码下无意义），收起二级面板。"""
        for i in (0, 1):  # Aa 字体, A 颜色
            if i < len(self.toggle_btns):
                self.toggle_btns[i].setVisible(not enable)
        if enable:
            self.format_panel.setVisible(False)
            for b in self.toggle_btns:
                b.setChecked(False)

    def _toggle_markdown_mode(self):
        """便签右键菜单：普通 ⇄ Markdown 切换。"""
        self._set_markdown_mode(not self.editor_host.is_md)
        self.save_data()

    def _set_markdown_mode(self, enable):
        """核心切换逻辑：双格式双写缓存，切换瞬间零转换。

        普通 → MD：优先复用 md_view 内存中保留的 Markdown 源码（零丢失）；
                  仅当 MD 源码为空、或普通模式被编辑过（指纹变化）时才重新转换。
        MD → 普通：源码未改 → 直接用缓存 HTML（零损失）；改过 → 才用新渲染。
        """
        if enable == self.editor_host.is_md:
            return
        note_dir = ""
        if self.save_file:
            note_dir = os.path.dirname(self.save_file).replace('\\', '/')
        if enable:
            md_existing = self.editor_host.md_view.markdown_text().strip()
            use_existing = bool(md_existing)
            if use_existing and hasattr(self, '_rich_fp_at_leave'):
                # 普通模式被编辑过 → 重新转换同步新内容；否则复用 MD 源码保留语法细节
                if self._rich_fingerprint() != self._rich_fp_at_leave:
                    use_existing = False
            if use_existing:
                self.editor_host.md_view.set_markdown(md_existing, note_dir)
            else:
                from markdown_conv import doc_to_markdown
                md_src = doc_to_markdown(self.text_edit.document(), note_dir)
                self.editor_host.md_view.set_markdown(md_src, note_dir)
            self.editor_host.rich_view.hide()
            self.editor_host.md_view.show()
            if hasattr(self, 'src_vis_btn'):
                self.src_vis_btn.show()
                self._update_src_vis_btn(
                    self.editor_host.md_view.src_visible_state()
                )
        else:
            if self.editor_host.md_view.is_dirty():
                from markdown_conv import md_to_html
                html = md_to_html(self.editor_host.md_view.markdown_text(), note_dir)
                # 相对路径图片 → file:// 绝对（与 load_data 一致）
                if note_dir:
                    html = re.sub(
                        r'src="([^"]+)"',
                        lambda m: f'src="file:///{note_dir}/{m.group(1)}"'
                        if not m.group(1).startswith(('file:', 'http:', 'data:'))
                        else m.group(0),
                        html,
                    )
                self.text_edit.setHtml(html)
            self.editor_host.md_view.hide()
            self.editor_host.rich_view.show()
            # 记录离开 MD 时的富文本指纹，供切回时判断普通模式是否被编辑
            self._rich_fp_at_leave = self._rich_fingerprint()
        self.editor_host.is_md = enable
        self._apply_md_toolbar(enable)
        if hasattr(self, 'md_btn'):
            self.md_btn.setChecked(enable)
        if not enable and hasattr(self, 'src_vis_btn'):
            self.src_vis_btn.hide()
        self._mark_dirty()

    def _rich_fingerprint(self):
        """当前富文本内容的指纹（切换 MD 时判断普通模式是否被编辑过）。"""
        try:
            return self.text_edit.document().toHtml()
        except Exception:
            return ""
    
    def _apply_bg_color(self):
        """将当前的背景色和透明度动态渲染到便签底板上。"""
        r, g, b, a = self.bg_color
        # 转换 Alpha 通道：Qt 取值 0-255，CSS rgba 需要 0.0-1.0
        alpha_css = a / 255.0
        
        # 新番便签不保留特殊边框（用户要求删除蓝色外包边）
        border_css = ""
        
        self.bg_frame.setStyleSheet(f"""
            QFrame#bg_frame {{
                background-color: rgba({r}, {g}, {b}, {alpha_css:.2f});
                border-radius: 15px;
                {border_css}
            }}
        """)

    def _toggle_lock(self):
        """切换便签的锁定状态。"""
        self.is_locked = not self.is_locked
        self._apply_lock_ui()
        self.save_data()

    def _ui_font_family(self):
        """当前界面字体族（读用户配置，缺失时回退到默认）。

        每次读取而非缓存：用户在控制面板换字体后无需重启即可生效。
        读取失败一律降级为默认字体，不影响便签主流程。
        """
        try:
            return fonts_mod.resolve_family(load_config().get("font_family"))
        except Exception:
            return fonts_mod.resolve_family()

    def _toggle_always_on_top(self):
        """切换置顶状态。需要 hide + 改 flag + show 来刷新窗口属性。"""
        self.is_always_on_top = not self.is_always_on_top
        self.save_data()
        self.hide()
        self.apply_window_states()
        self.show()
        global_signaler.note_updated_signal.emit()

    def apply_window_states(self):
        """根据当前属性设置窗口标志。

        Qt.Tool 隐藏任务栏图标；WindowStaysOnTopHint 控制置顶。
        """
        flags = Qt.Tool | Qt.FramelessWindowHint
        if self.is_always_on_top:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        # 便签夹的标题条也要跟着置顶 / 取消置顶，否则整叠沉下去了只剩它还浮在最上层
        stacks.STACKS.on_window_state_changed(self)

    # --- 持久化 ---

    def _mark_dirty(self):
        """标记内容已变更，启动 500ms 防抖定时器。到期后自动调用 _flush_save。"""
        self._dirty = True
        self._save_timer.start()

    def _flush_save(self):
        """防抖定时器到期回调：执行实际的写盘操作。"""
        if self._dirty:
            self.save_data()
            self._dirty = False

    def save_data(self):
        """将便签状态（位置、大小、内容、设置）序列化为 JSON。"""
        if getattr(self, '_is_loading', False):
            return          # 加载期间禁止触发保存，防止覆盖旧数据
        if self.width() < 250:
            return
        if not os.path.exists(SAVE_DIR):
            os.makedirs(SAVE_DIR)

        title = self.header.title_edit.text()
        base = sanitize_filename(title)

        # 统一文件夹模式：所有便签都存为 {标题}/data.json
        folder = os.path.join(SAVE_DIR, base)
        new_path = os.path.join(folder, "data.json")

        # 如果有旧单文件，先删除（升级场景）
        old_single = os.path.join(SAVE_DIR, f"{base}.json")
        if os.path.exists(old_single):
            try:
                os.remove(old_single)
            except OSError:
                pass

        # 标题变更 → 整体重命名目录（含 data.json 与图片），保证目录名 = 标题
        new_path = self.save_file if (self.save_file and os.path.exists(self.save_file)) else None
        if new_path:
            old_dir = os.path.dirname(self.save_file)
            new_folder = os.path.join(SAVE_DIR, base)
            if os.path.abspath(old_dir) != os.path.abspath(new_folder):
                # 目标目录未被占用才重命名；失败（占用/权限）则沿用旧路径
                if not os.path.exists(new_folder):
                    try:
                        os.rename(old_dir, new_folder)
                        new_path = os.path.join(new_folder, "data.json")
                        self.save_file = new_path
                    except OSError:
                        pass
        else:
            new_path = make_save_path(title, exclude_path=self.save_file)

        os.makedirs(os.path.dirname(new_path), exist_ok=True)

        # 如果有旧文件且路径不同 → 删除旧文件，清理旧空文件夹
        if self.save_file and os.path.exists(self.save_file) and os.path.abspath(self.save_file) != os.path.abspath(new_path):
            old_dir = os.path.dirname(self.save_file)
            try:
                os.remove(self.save_file)
                # 尝试删除旧文件夹（失败了也无所谓，里面可能有图片）
                if old_dir != SAVE_DIR and os.path.isdir(old_dir):
                    shutil.rmtree(old_dir, ignore_errors=True)
            except OSError:
                pass

        self.save_file = new_path
        # 将文件绝对路径的 img src 还原为相对路径
        note_dir = os.path.dirname(self.save_file).replace('\\', '/')
        html = self.text_edit.toHtml()
        if getattr(self, 'editor_host', None) and self.editor_host.is_md:
            # MD 模式：源码为主格式；HTML 作为渲染缓存（源码被改过才重渲染）
            from markdown_conv import md_to_html
            md_src = self.editor_host.md_view.markdown_text()
            if self.editor_host.md_view.is_dirty():
                html = md_to_html(md_src, note_dir)
        else:
            from markdown_conv import doc_to_markdown
            md_src = doc_to_markdown(self.text_edit.document(), note_dir)
        # 处理 file:// URL（含 URL 编码），还原为相对路径
        def _unfile_src(m):
            raw = m.group(1)
            decoded = urllib.parse.unquote(raw)
            for prefix in ('file:///', 'file://', 'file:'):
                if decoded.startswith(prefix):
                    decoded = decoded[len(prefix):]
                    break
            try:
                rel = os.path.relpath(decoded, note_dir.replace('/', os.sep))
                return f'src="{rel.replace(os.sep, "/")}"'
            except ValueError:
                return m.group(0)
        html = re.sub(r'src="(file://[^"]+)"', _unfile_src, html)
        # 清理未被引用的图片文件
        _cleanup_orphan_images(note_dir, html)

        # MD 切半状态下的原始全宽/最小宽（仅源码隐藏时记录，供重启后还原）
        md_full_w = 0
        md_min_w = 0
        md_user_hidden = False
        if getattr(self, 'editor_host', None) and self.editor_host.is_md:
            mdv = self.editor_host.md_view
            if mdv._saved_full_width:
                md_full_w = mdv._saved_full_width
                md_min_w = mdv._orig_min_width
            # 用户手动隐藏源码状态（重启后保持切半/全宽一致）
            md_user_hidden = mdv._user_hidden_src

        data = {
            "note_id": self.note_id,
            "title": title,
            "html_content": html,
            "markdown": bool(getattr(self, 'editor_host', None) and self.editor_host.is_md),
            "content_md": md_src,
            "x": self.x(), "y": self.y(),
            "width": self.width(), "height": self._persist_height(),
            "is_locked": self.is_locked,
            "is_always_on_top": getattr(self, 'is_always_on_top', True),
            "is_hidden": getattr(self, 'is_hidden', False),
            "is_collapsed": self._is_collapsed_for_save(),
            "stack_id": getattr(self, 'stack_id', ''),
            "stack_pos": int(getattr(self, 'stack_pos', 0) or 0),
            "bg_color": self.bg_color,
            "note_hotkey": getattr(self, '_note_hotkey', ''),
            # 持久化 MD 切半前的原始全宽（重启后避免二次切半，解除锁定可还原全宽）
            "md_full_width": md_full_w,
            "md_orig_min_width": md_min_w,
            "md_user_hidden_src": md_user_hidden,
        }
        with open(self.save_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def closeEvent(self, event):
        """窗口关闭前确保脏数据落盘（已删除的便签除外），并从活动列表中移除。"""
        if not self._deleted:
            self._save_timer.stop()
            if self._dirty:
                self.save_data()
        if self in ACTIVE_NOTES:
            ACTIVE_NOTES.remove(self)
        super().closeEvent(event)

    def resizeEvent(self, event):
        """重新定位四角缩放手柄，并让过长标题重新自适应字号。"""
        super().resizeEvent(event)
        bw = self.bg_frame.width()
        bh = self.bg_frame.height()
        gs = 20
        self._grips[0].move(0, 0)           # 左上
        self._grips[1].move(bw - gs, 0)      # 右上
        self._grips[2].move(0, bh - gs)      # 左下
        self._grips[3].move(bw - gs, bh - gs) # 右下
        self.header.refresh_title_font()

    def load_data(self):
        """从 JSON 文件恢复便签状态。"""
        self._is_loading = True     # 护盾：加载期间禁止自动保存

        # 通过 note_id 在目录中查找对应的文件（文件夹模式）
        if not self.save_file and os.path.exists(SAVE_DIR):
            for item in os.listdir(SAVE_DIR):
                item_path = os.path.join(SAVE_DIR, item)
                if not os.path.isdir(item_path):
                    continue
                data_file = os.path.join(item_path, "data.json")
                if os.path.exists(data_file):
                    try:
                        with open(data_file, 'r', encoding='utf-8') as fh:
                            data = json.load(fh)
                        if data.get("note_id") == self.note_id:
                            self.save_file = data_file
                            break
                    except (json.JSONDecodeError, OSError):
                        pass
            # 旧格式兜底：文件名即 ID（v2.x 及更早）
            if not self.save_file:
                legacy = os.path.join(SAVE_DIR, f"{self.note_id}.json")
                if os.path.exists(legacy):
                    self.save_file = legacy

        if self.save_file and os.path.exists(self.save_file):
            try:
                with open(self.save_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.header.title_edit.setText(data.get("title", get_new_note_title()))
                    html = data.get("html_content", "")
                    # 将相对路径图片转为绝对 file:// URL
                    if self.save_file:
                        note_dir = os.path.dirname(self.save_file).replace('\\', '/')
                        html = re.sub(
                            r'src="([^"]+)"',
                            lambda m: f'src="file:///{note_dir}/{m.group(1)}"'
                            if not m.group(1).startswith(('file:', 'http:', 'data:'))
                            else m.group(0),
                            html
                        )
                    self.text_edit.setHtml(html)
                    # Markdown 模式恢复：读源码装载到分栏视图
                    if data.get("markdown"):
                        note_dir = os.path.dirname(self.save_file).replace('\\', '/')
                        self.editor_host.md_view.set_markdown(
                            data.get("content_md", ""), note_dir
                        )
                        # 恢复切半前的原始全宽（重启后避免二次切半，解锁可还原全宽）
                        self.editor_host.md_view._saved_full_width = (
                            data.get("md_full_width", 0) or 0
                        )
                        self.editor_host.md_view._orig_min_width = (
                            data.get("md_orig_min_width", 0) or 320
                        )
                        # 恢复用户手动隐藏源码状态（旧数据无该字段时，
                        # 用"存在全宽记录"推断上次为隐藏态，保持切半不跳动）
                        self.editor_host.md_view._user_hidden_src = data.get(
                            "md_user_hidden_src",
                            bool(data.get("md_full_width")),
                        )
                        self.editor_host.md_view.show()
                        self.editor_host.rich_view.hide()
                        self.editor_host.is_md = True
                        if hasattr(self, 'md_btn'):
                            self.md_btn.setChecked(True)
                        if hasattr(self, 'src_vis_btn'):
                            self.src_vis_btn.show()
                            self._update_src_vis_btn(
                                self.editor_host.md_view.src_visible_state()
                            )
                        self._apply_md_toolbar(True)
                    x = data.get("x", 100)
                    y = data.get("y", 100)
                    w = max(data.get("width", 320), 300)
                    h = max(data.get("height", 320), 280)
                    self.setGeometry(x, y, w, h)
                    if data.get("markdown") and hasattr(self, 'editor_host'):
                        # 按恢复的隐藏源码状态同步窗口宽度（切半保持，或还原全宽）
                        self.editor_host.md_view._apply_src_visible()
                    self.is_locked = data.get("is_locked", False)
                    self.is_hidden = data.get("is_hidden", False)
                    self.stack_id = data.get("stack_id", "") or ""
                    self.stack_pos = int(data.get("stack_pos", 0) or 0)
                    self.is_always_on_top = data.get("is_always_on_top", True)
                    self.bg_color = data.get("bg_color", [255, 249, 196, 242])
                    self._note_hotkey = data.get("note_hotkey", "")
                    if self._note_hotkey:
                        # 延迟注册：确保 app.exec() 已启动，消息循环就绪
                        QTimer.singleShot(0, lambda nid=self.note_id, hk=self._note_hotkey: global_signaler.register_note_hotkey.emit(nid, hk))
                    self.format_panel.opacity_slider.setValue(int(round(self.bg_color[3] / 2.55)))
                    self._apply_lock_ui()
                    # 折叠状态不在此刻落位：特殊便签（事务/日程/新番）的内容与最小尺寸
                    # 是在父类 __init__ 返回之后才建立的，这里收成一条会被随后设置的
                    # 最小高度顶开（标题变小但内容照样显示）。改为首次显示时统一收（showEvent）
                    if data.get("is_collapsed"):
                        self._pending_collapse = True
            except Exception as e:
                print(f"[AniNote] 加载便签 {self.note_id} 失败: {e}")
        else:
            self.header.title_edit.setText(get_new_note_title())

        self._is_loading = False
        # 装载完标题后再自适应字号（此时字段宽度才算得准）
        QTimer.singleShot(0, self.header.refresh_title_font)

    def update_button_hints(self):
        """配置变更后更新工具栏按钮的快捷键提示。"""
        cfg = load_config()
        self.new_note_btn.setToolTip(f"新建 ({cfg.get('new_hotkey', 'alt+m').upper()})")


# ---------- 区域截图遮罩 ----------

class RegionSelector(QWidget):
    """全屏半透明遮罩，支持拖拽选区截图。"""

    selected = Signal(QPixmap)

    def __init__(self, full_pixmap):
        super().__init__()
        self._full = full_pixmap
        self._origin = QPoint()
        self._rect = QRect()
        self._drawing = False
        self.setCursor(Qt.CrossCursor)
        # 确保遮罩在所有窗口之上
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)

    def showEvent(self, event):
        super().showEvent(event)
        self.grabMouse()

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._full)
        dark = QColor(0, 0, 0, 100)
        p.fillRect(self.rect(), dark)
        if not self._rect.isNull():
            # 用背景图的逻辑坐标缩放版本填充选区，避免高 DPI 放大
            sx = self._full.width() / self.width()
            sy = self._full.height() / self.height()
            src_rect = QRect(
                int(self._rect.x() * sx),
                int(self._rect.y() * sy),
                int(self._rect.width() * sx),
                int(self._rect.height() * sy)
            )
            p.drawPixmap(self._rect, self._full, src_rect)
            pen = QPen(QColor("#0078D7"), 2, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(self._rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._origin = event.pos()
            self._rect = QRect()
            self._drawing = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._drawing:
            self._rect = QRect(self._origin, event.pos()).normalized()
            self.update()

    def mouseReleaseEvent(self, event):
        if self._drawing and event.button() == Qt.LeftButton:
            self._drawing = False
            self._rect = QRect(self._origin, event.pos()).normalized()
            if self._rect.width() > 10 and self._rect.height() > 10:
                # 将逻辑坐标映射到物理像素
                sx = self._full.width() / self.width()
                sy = self._full.height() / self.height()
                phys = QRect(
                    int(self._rect.x() * sx),
                    int(self._rect.y() * sy),
                    int(self._rect.width() * sx),
                    int(self._rect.height() * sy)
                )
                cropped = self._full.copy(phys)
                self.selected.emit(cropped)
            self.releaseMouse()
            self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.releaseMouse()
            self.close()

    def closeEvent(self, event):
        try:
            self.releaseMouse()
        except:
            pass
        super().closeEvent(event)


# ---------- 事务追踪器 ----------

class DragHandle(QLabel):
    """拖拽排序手柄：点击并纵向拖动可移动所在行。"""

    drag_started = Signal(object)   # handle 自身
    drag_moved = Signal(object, int)  # handle, global_y
    drag_dropped = Signal(object, int)  # handle, global_y

    def __init__(self, parent=None):
        super().__init__("⋮⋮", parent)
        self.setCursor(Qt.OpenHandCursor)
        self.setStyleSheet(
            "font-size: 14px; color: #bbb; padding: 0 4px;"
            " background: transparent; border: none;"
        )
        self.setFixedWidth(20)
        self._drag_start_y = 0
        self._dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_y = event.globalPosition().y()
            self._dragging = False
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton):
            return
        delta = abs(event.globalPosition().y() - self._drag_start_y)
        if delta < 6:
            return
        if not self._dragging:
            self._dragging = True
            self.drag_started.emit(self)
        self.drag_moved.emit(self, int(event.globalPosition().y()))

    def mouseReleaseEvent(self, event):
        self.setCursor(Qt.OpenHandCursor)
        if self._dragging:
            self._dragging = False
            self.drag_dropped.emit(self, int(event.globalPosition().y()))

class HabitTrackerWindow(AniNoteWindow):
    """事务追踪器窗口，继承便签的全部功能。

    在便签基础上增加周视图日历导航、事务列表、每日打卡圆钮、
    新增事务等功能。数据存储在便签 JSON 的 habits_data 中。
    """

    WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

    def __init__(self, note_id=None):
        # 使用固定的 note_id 前缀，方便全局管理
        nid = note_id if note_id else f"habit_{uuid.uuid4().hex[:8]}"
        super().__init__(note_id=nid)

        # 追踪器最小尺寸要能容下固定布局，防止列错位
        self.setMinimumSize(500, 320)

        # 隐藏文本编辑区，替换为习惯追踪 UI
        self.text_edit.hide()

        # 状态
        self._week_offset = 0
        self._habits = []       # [{id, name, color, records: {date: bool}}]
        self._drag_handles = {}  # hab_id -> DragHandle
        self._drag_row = None    # 正在拖拽的 grid row
        self._drag_placeholder = None  # 拖拽时的浮动控件

        # 统一网格：日期 + 事务行共用同一列宽，杜绝对不齐
        self._build_tracker_grid()

        # 底部操作栏
        self._build_bottom_actions()

        # 加载旧数据
        self._load_habits()

        # 延迟弹出
        QTimer.singleShot(0, self._show_if_not_hidden)

    def _show_if_not_hidden(self):
        if not getattr(self, 'is_hidden', False):
            # 淡入显示（不再直入直出）
            self.animated_show()
            self.raise_()
            self.activateWindow()

    # ---------- 统一网格布局 ----------

    def _build_tracker_grid(self):
        """构建滚动区域 + 内部网格，日期和事务行共享列宽。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: transparent; width: 5px; margin: 0; }
            QScrollBar::handle:vertical { background: #D0D0D0; border-radius: 2px; min-height: 20px; }
            QScrollBar::handle:vertical:hover { background: #A0A0A0; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)
        # 滚动条自动隐藏：未滚动时透明，滚动时显示，停 1.2s 后隐藏（对齐 MD 便签）
        setup_auto_hide_scrollbar(
            scroll.verticalScrollBar(), scroll.styleSheet(),
            "QScrollBar:vertical { background: transparent; width: 5px; margin: 0; }"
            " QScrollBar::handle:vertical { background: transparent; border-radius: 2px; min-height: 20px; }"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            " QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }",
        )

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(container)
        self._grid.setContentsMargins(0, 5, 0, 5)
        self._grid.setVerticalSpacing(4)

        # ── 列定义 ──
        # col 0（左区）：◀ 按钮 / 事务名称      → 固定 138px
        # col 1 ~ 7（打卡列）：日期标签 / 打卡钮  → 均分拉伸
        # col 8（右区）：▶ 按钮 / 删除按钮       → 固定 30px
        self._grid.setColumnMinimumWidth(0, 138)
        self._grid.setColumnStretch(0, 0)

        for c in range(1, 8):
            self._grid.setColumnMinimumWidth(c, 36)
            self._grid.setColumnStretch(c, 1)

        self._grid.setColumnMinimumWidth(8, 30)
        self._grid.setColumnStretch(8, 0)

        # ── 行 0：日期导航头 ──
        btn_style = (
            "QPushButton { border: none; background: transparent; font-size: 16px; "
            "color: #888; padding: 4px 8px; }"
            "QPushButton:hover { color: #333; background: rgba(0,0,0,0.05); border-radius: 4px; }"
        )
        self._btn_prev = QPushButton("◀")
        self._btn_prev.setStyleSheet(btn_style)
        self._btn_prev.clicked.connect(self._prev_week)
        self._grid.addWidget(self._btn_prev, 0, 0, Qt.AlignLeft | Qt.AlignVCenter)

        self._day_labels = []
        for i in range(7):
            lbl = QLabel()
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(
                "color: #999; font-size: 12px; font-weight: bold; padding: 2px;"
            )
            self._grid.addWidget(lbl, 0, i + 1)
            self._day_labels.append(lbl)

        self._btn_next = QPushButton("▶")
        self._btn_next.setStyleSheet(btn_style)
        self._btn_next.clicked.connect(self._next_week)
        self._grid.addWidget(self._btn_next, 0, 8, Qt.AlignCenter)

        self._refresh_date_labels()

        # 占位 stretch 行，确保行 1+ 从顶部开始
        self._grid.setRowStretch(50, 1)

        scroll.setWidget(container)
        self._tracker_scroll = scroll
        self._tracker_container = container

        # 插入到编辑器原来的位置（text_edit 已在 editor_host 内，以其定位）
        frame_layout = self.bg_frame.layout()
        idx = frame_layout.indexOf(self.editor_host)
        if idx < 0:
            idx = frame_layout.indexOf(self.text_edit)
        frame_layout.insertWidget(idx, scroll)
        self.editor_host.hide()

    def _refresh_date_labels(self):
        """根据当前周偏移量刷新日期标签。"""
        today = datetime_module.date.today()
        monday = today - datetime_module.timedelta(days=today.weekday())
        monday += datetime_module.timedelta(weeks=self._week_offset)

        for i, lbl in enumerate(self._day_labels):
            day = monday + datetime_module.timedelta(days=i)
            date_str = day.strftime("%d")
            is_today = day == today
            lbl.setText(f"{self.WEEKDAYS[i]}\n{date_str}")
            if is_today:
                lbl.setStyleSheet(
                    "color: #0078D7; font-size: 12px; font-weight: bold; "
                    "padding: 2px; background: #E8F4FD; border-radius: 6px;"
                )
            else:
                lbl.setStyleSheet(
                    "color: #999; font-size: 12px; font-weight: bold; padding: 2px;"
                )

    def _get_week_dates(self):
        """返回本周的 7 个 date 对象列表。"""
        today = datetime_module.date.today()
        monday = today - datetime_module.timedelta(days=today.weekday())
        monday += datetime_module.timedelta(weeks=self._week_offset)
        return [monday + datetime_module.timedelta(days=i) for i in range(7)]

    def _prev_week(self):
        if self._week_offset <= -52:
            return
        self._week_offset -= 1
        self._refresh_date_labels()
        self._refresh_habit_list()

    def _next_week(self):
        if self._week_offset >= 52:
            return
        self._week_offset += 1
        self._refresh_date_labels()
        self._refresh_habit_list()

    # ---------- 事务列表（网格行） ----------

    def _refresh_habit_list(self):
        """重建网格中的事务行。"""
        # 清除旧的事务行（row 1 开始）
        row = 1
        while True:
            item = self._grid.itemAtPosition(row, 0)
            if item is None:
                break
            # 清除该行所有列的控件
            for col in range(9):
                w = self._grid.itemAtPosition(row, col)
                if w:
                    widget = w.widget()
                    if widget:
                        widget.deleteLater()
            row += 1

        week_dates = self._get_week_dates()
        today = datetime_module.date.today()

        for i, habit in enumerate(self._habits):
            grid_row = i + 1
            self._add_habit_to_grid(habit, week_dates, today, grid_row)

    def _on_drag_started(self, handle):
        """开始拖拽：记录源行，创建浮动预览。"""
        if getattr(self, 'is_locked', False):
            return
        for hab in self._habits:
            if self._drag_handles.get(hab["id"]) is handle:
                self._drag_row = self._habits.index(hab) + 1
                break
        if self._drag_row is None:
            return
        left_widget = self._grid.itemAtPosition(self._drag_row, 0)
        if left_widget and left_widget.widget():
            w = left_widget.widget()
            self._drag_placeholder = QLabel(pixmap=w.grab())
            self._drag_placeholder.setWindowFlags(
                Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            )
            self._drag_placeholder.setAttribute(Qt.WA_TranslucentBackground)
            self._drag_placeholder.setStyleSheet(
                "background: white; border: 1px solid #0078D7; opacity: 0.85;"
            )
            self._drag_placeholder.resize(w.width() + 300, w.height())
            self._drag_placeholder.move(
                w.mapToGlobal(w.rect().topLeft())
            )
            self._drag_placeholder.show()

    def _on_drag_moved(self, handle, global_y):
        """拖拽移动：更新浮动预览位置。"""
        if self._drag_placeholder:
            x = self._drag_placeholder.x()
            self._drag_placeholder.move(x, global_y - self._drag_placeholder.height() // 2)

    def _on_drag_dropped(self, handle, global_y):
        """拖拽释放：计算目标行，重排列表。"""
        if self._drag_row is None:
            return
        if self._drag_placeholder:
            self._drag_placeholder.close()
            self._drag_placeholder = None
        # 计算目标行：找到全局 y 落在哪个行的中心以下
        target_row = len(self._habits)
        for r in range(1, len(self._habits) + 1):
            item = self._grid.itemAtPosition(r, 0)
            if item is None or item.widget() is None:
                continue
            w = item.widget()
            center_y = w.mapToGlobal(w.rect().topLeft()).y() + w.height() // 2
            if global_y < center_y:
                target_row = r
                break
        src_idx = self._drag_row - 1
        dst_idx = max(0, min(target_row - 1, len(self._habits) - 1))
        if src_idx != dst_idx:
            hab = self._habits.pop(src_idx)
            self._habits.insert(dst_idx, hab)
            self._refresh_habit_list()
            self.save_data()
        self._drag_row = None

    def _add_habit_to_grid(self, habit, week_dates, today, grid_row):
        """向网格的指定行填充一条事务，按模式渲染不同内容。"""
        hab_id = habit["id"]

        # col 0：拖拽手柄 + 颜色条 + 名称（三种模式共用）
        left = QWidget()
        left.setStyleSheet("background: transparent;")
        left_layout = QHBoxLayout(left)
        left_layout.setContentsMargins(2, 6, 0, 6)
        left_layout.setSpacing(4)

        handle = DragHandle()
        handle.setToolTip("拖拽排序")
        handle.drag_started.connect(self._on_drag_started)
        handle.drag_moved.connect(self._on_drag_moved)
        handle.drag_dropped.connect(self._on_drag_dropped)
        self._drag_handles[hab_id] = handle
        left_layout.addWidget(handle)

        color_bar = QWidget()
        color_bar.setFixedSize(4, 28)
        color_bar.setStyleSheet(
            f"background-color: {habit.get('color', '#0078D7')}; "
            f"border-radius: 2px; border: none;"
        )
        left_layout.addWidget(color_bar)

        name_lbl = QLabel(habit.get("name", ""))
        name_lbl.setStyleSheet(
            "font-size: 14px; color: #333; font-weight: bold; "
            "background: transparent; border: none;"
        )
        name_lbl.setWordWrap(True)
        left_layout.addWidget(name_lbl)
        self._grid.addWidget(left, grid_row, 0)

        mode = habit.get("mode", "free")

        # ── 模式：周期循环 ──
        if mode == "cycle":
            cycle_days = habit.get("cycle_days", 7)
            cycle_start_str = habit.get("cycle_start", "")
            if not cycle_start_str:
                cycle_start_str = today.strftime("%Y-%m-%d")
                habit["cycle_start"] = cycle_start_str

            cycle_start = datetime_module.date.fromisoformat(cycle_start_str)
            days_in = (today - cycle_start).days + 1

            # 周期结束 → 自动推进
            while days_in > cycle_days:
                cycle_start += datetime_module.timedelta(days=cycle_days)
                days_in = (today - cycle_start).days + 1
                habit["cycle_start"] = cycle_start.strftime("%Y-%m-%d")
                habit["cycle_completed"] = False

            status_text = f"第 {days_in}/{cycle_days} 天"
            if habit.get("cycle_completed", False):
                status_text += "  ✅ 已完成"
                status_color = habit.get("color", "#4CAF50")
                bg = f"background: {status_color}; color: white; font-weight: bold;"
            else:
                status_text += "  ⭕ 未完成"
                bg = "background: rgba(0,0,0,0.03); color: #555;"

            def make_cycle_toggle(hid, cyc_done):
                return lambda: self._toggle_cycle(hid, not cyc_done)

            btn = QPushButton(status_text)
            btn.setEnabled(not self.is_locked)
            btn.setCursor(Qt.PointingHandCursor if not self.is_locked else Qt.ArrowCursor)
            btn.setStyleSheet(
                f"QPushButton {{ padding: 4px 10px; border-radius: 6px; font-size: 13px; "
                f"border: none; {bg} }}"
                f"QPushButton:hover {{ opacity: 0.8; }}"
            )
            btn.clicked.connect(make_cycle_toggle(habit["id"], habit.get("cycle_completed", False)))
            self._grid.addWidget(btn, grid_row, 1, 1, 7, Qt.AlignCenter)

        # ── 模式：倒计时 ──
        elif mode == "countdown":
            end_str = habit.get("countdown_end", "")
            if end_str:
                try:
                    end_date = datetime_module.date.fromisoformat(end_str)
                    remaining = (end_date - today).days
                except ValueError:
                    remaining = -1
            else:
                remaining = -1

            if remaining > 0:
                status_text = f"距离结束还有 {remaining} 天"
                bg = "background: rgba(0,0,0,0.03); color: #e67e22;"
            else:
                status_text = "已结束"
                bg = "background: rgba(0,0,0,0.06); color: #999;"

            lbl = QLabel(status_text)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(
                f"padding: 4px 10px; border-radius: 6px; font-size: 13px; "
                f"font-weight: bold; {bg}"
            )
            self._grid.addWidget(lbl, grid_row, 1, 1, 7, Qt.AlignCenter)

        # ── 模式：自由打卡（默认）──
        else:
            for ci, d in enumerate(week_dates):
                date_key = d.strftime("%Y-%m-%d")
                checked = habit.get("records", {}).get(date_key, False)
                is_future = d > today

                cb = QPushButton()
                cb.setFixedSize(26, 26)
                cb.setCheckable(True)
                cb.setChecked(checked)
                cb.setEnabled(not is_future and not self.is_locked)

                if checked:
                    cb.setStyleSheet(
                        f"QPushButton {{"
                        f" background-color: {habit.get('color', '#4CAF50')}; "
                        f" border-radius: 13px; border: none; color: white; font-size: 12px; "
                        f" font-weight: bold;"
                        f"}}"
                    )
                    cb.setText("✓")
                elif is_future:
                    cb.setStyleSheet(
                        "QPushButton { background: transparent; border-radius: 13px; "
                        "border: 1px dashed #ddd; color: transparent; }"
                    )
                else:
                    cb.setStyleSheet(
                        f"QPushButton {{"
                        f" background: transparent; border-radius: 13px; "
                        f" border: 1.5px solid {habit.get('color', '#ccc')}; color: transparent;"
                        f"}}"
                        f"QPushButton:hover {{ background: rgba(0,0,0,0.05); }}"
                    )

                hab_id = habit["id"]
                cb.clicked.connect(lambda checked, hid=hab_id, dk=date_key: self._toggle_habit(hid, dk, checked))
                self._grid.addWidget(cb, grid_row, ci + 1, Qt.AlignCenter)

        # col 8：删除按钮（三种模式共用）
        del_btn = QPushButton("×")
        del_btn.setFixedSize(22, 22)
        del_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; color: #ccc; "
            "font-size: 16px; font-weight: bold; }"
            "QPushButton:hover { color: #E81123; background: rgba(231,17,35,0.1); border-radius: 4px; }"
        )
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.clicked.connect(lambda checked, hid=habit["id"]: self._delete_habit(hid))
        self._grid.addWidget(del_btn, grid_row, 8, Qt.AlignCenter)

    # ---------- 周期切换 ----------

    def _toggle_cycle(self, hab_id, completed):
        for h in self._habits:
            if h["id"] == hab_id:
                h["cycle_completed"] = completed
                break
        self._refresh_habit_list()
        self.save_data()

    # ---------- 操作 ----------

    def _toggle_habit(self, hab_id, date_key, checked):
        try:
            for h in self._habits:
                if h["id"] == hab_id:
                    h.setdefault("records", {})[date_key] = checked
                    break
            self._refresh_habit_list()
            self.save_data()
        except Exception:
            import traceback
            log_path = os.path.join(SAVE_DIR, "aninote_crash.log")
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"\n--- {datetime_module.datetime.now()} ---\n")
                traceback.print_exc(file=lf)
            QMessageBox.warning(
                self, "操作失败",
                "打卡操作出错，错误信息已写入 notes_data/aninote_crash.log。\n"
                "请将此文件提交给开发者排查。"
            )

    def _delete_habit(self, hab_id):
        if self.is_locked:
            return
        self._habits = [h for h in self._habits if h["id"] != hab_id]
        self._refresh_habit_list()
        self.save_data()

    def _add_habit(self):
        """弹出对话框添加新事务，支持自由打卡 / 周期循环 / 倒计时三种模式。"""
        if self.is_locked:
            return

        # 无边框圆角窗口（对齐控制面板风格）：dlg_bg 圆角底 + 阴影 + 自定义标题栏
        dialog = QDialog(self)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setAttribute(Qt.WA_TranslucentBackground)
        dialog.setFixedSize(420, 500)
        dialog.setStyleSheet(
            "QFrame#habit_dlg_bg { background: #FAFAFA; border-radius: 12px;"
            " border: 1px solid #EAEAEA; }"
        )

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(12, 12, 12, 12)

        dlg_bg = QFrame()
        dlg_bg.setObjectName("habit_dlg_bg")
        shadow = QGraphicsDropShadowEffect(dialog)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 40))
        shadow.setOffset(0, 6)
        dlg_bg.setGraphicsEffect(shadow)
        outer.addWidget(dlg_bg)

        layout = QVBoxLayout(dlg_bg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 自定义标题栏：标题 + 关闭按钮（hover 红），可拖拽
        dlg_bar = QFrame()
        dlg_bar.setStyleSheet("background: transparent;")
        dlg_bar.setFixedHeight(45)
        bar_layout = QHBoxLayout(dlg_bar)
        bar_layout.setContentsMargins(20, 0, 10, 0)
        dlg_title = QLabel("新建事务")
        # 字重走 QFont 真实字面（QSS 的 font-weight 会触发 Qt 合成且字体族不生效）
        dlg_title.setStyleSheet("color: #333;")
        dlg_title.setFont(fonts_mod.make_font(px=15, weight="bold"))
        bar_layout.addWidget(dlg_title)
        bar_layout.addStretch()
        dlg_close = QPushButton(icon("close"))
        set_icon_font(dlg_close, 16)
        dlg_close.setFixedSize(36, 30)
        dlg_close.setStyleSheet(
            "QPushButton { border: none; border-radius: 6px; background-color: transparent;"
            " font-size: 14px; color: #555; }"
            " QPushButton:hover { background-color: #E81123; color: white; }"
        )
        dlg_close.clicked.connect(dialog.reject)
        bar_layout.addWidget(dlg_close)
        layout.addWidget(dlg_bar)

        dlg_bar._drag_pos = None
        def _bar_press(e):
            if e.button() == Qt.LeftButton:
                dlg_bar._drag_pos = e.globalPosition().toPoint() - dialog.pos()
                e.accept()
        def _bar_move(e):
            if dlg_bar._drag_pos is not None:
                dialog.move(e.globalPosition().toPoint() - dlg_bar._drag_pos)
                e.accept()
        def _bar_release(e):
            dlg_bar._drag_pos = None
        dlg_bar.mousePressEvent = _bar_press
        dlg_bar.mouseMoveEvent = _bar_move
        dlg_bar.mouseReleaseEvent = _bar_release

        content = QVBoxLayout()
        content.setContentsMargins(24, 6, 24, 18)
        content.setSpacing(14)
        layout.addLayout(content, 1)

        # 名称
        name_lbl = QLabel("事务名称")
        name_lbl.setStyleSheet("font-size: 13px; color: #555; font-weight: 600;")
        name_input = QLineEdit()
        name_input.setPlaceholderText("例如：早睡早起")
        name_input.setStyleSheet(
            "QLineEdit { padding: 8px 12px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " font-size: 14px; background: #FFFFFF; }"
            " QLineEdit:focus { border-color: #1A73E8; }"
        )

        # 颜色
        color_lbl = QLabel("标记颜色")
        color_lbl.setStyleSheet("font-size: 13px; color: #555; font-weight: 600; margin-top: 2px;")
        preset_colors = ["#E81123", "#FF8C00", "#107C10", "#0078D7", "#881798", "#333333"]
        color_btns = []
        selected_color = [preset_colors[0]]

        def on_color_click(c):
            selected_color[0] = c
            for b, oc in zip(color_btns, preset_colors):
                if oc == c:
                    # 选中态：白色内圈 + 色块描边
                    b.setStyleSheet(
                        f"QPushButton {{ background-color: {oc}; border-radius: 14px;"
                        f" border: 3px solid #FFFFFF; outline: 2px solid {oc}; }}"
                    )
                else:
                    b.setStyleSheet(
                        f"QPushButton {{ background-color: {oc}; border-radius: 14px;"
                        f" border: 2px solid rgba(0,0,0,0.08); }}"
                        " QPushButton:hover { border: 2px solid rgba(0,0,0,0.25); }"
                    )

        color_layout = QHBoxLayout()
        color_layout.setSpacing(10)
        for c in preset_colors:
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            btn.setCursor(Qt.PointingHandCursor)
            if c == preset_colors[0]:
                btn.setStyleSheet(
                    f"QPushButton {{ background-color: {c}; border-radius: 14px;"
                    f" border: 3px solid #FFFFFF; outline: 2px solid {c}; }}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ background-color: {c}; border-radius: 14px;"
                    f" border: 2px solid rgba(0,0,0,0.08); }}"
                    " QPushButton:hover { border: 2px solid rgba(0,0,0,0.25); }"
                )
            btn.clicked.connect(lambda checked, clr=c: on_color_click(clr))
            color_layout.addWidget(btn)
            color_btns.append(btn)
        color_layout.addStretch()

        # 模式选择
        mode_lbl = QLabel("打卡模式")
        mode_lbl.setStyleSheet("font-size: 13px; color: #555; font-weight: 600; margin-top: 4px;")
        mode_layout = QHBoxLayout()
        mode_layout.setSpacing(15)
        radio_free = QRadioButton("自由打卡")
        radio_cycle = QRadioButton("周期循环")
        radio_countdown = QRadioButton("倒计时")
        radio_free.setChecked(True)
        for r in [radio_free, radio_cycle, radio_countdown]:
            r.setStyleSheet("font-size: 13px;")
            r.setCursor(Qt.PointingHandCursor)
            mode_layout.addWidget(r)
        mode_layout.addStretch()

        # 周期/倒计时参数
        param_widget = QWidget()
        param_layout = QVBoxLayout(param_widget)
        param_layout.setContentsMargins(0, 0, 0, 0)
        param_layout.setSpacing(6)

        # 行 1：天数输入
        days_row = QHBoxLayout()
        days_row.setSpacing(8)
        param_lbl = QLabel("每")
        param_lbl.setStyleSheet("font-size: 13px; color: #333;")
        param_input = QSpinBox()
        param_input.setRange(1, 999)
        param_input.setValue(7)
        param_input.setSuffix(" 天")
        param_input.setFixedWidth(100)
        param_input.setStyleSheet(
            "QSpinBox { padding: 6px 8px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " background: #FFFFFF; font-size: 13px; }"
            " QSpinBox:focus { border-color: #1A73E8; }"
        )
        suffix_lbl = QLabel("循环")
        suffix_lbl.setStyleSheet("font-size: 13px; color: #333;")
        days_row.addWidget(param_lbl)
        days_row.addWidget(param_input)
        days_row.addWidget(suffix_lbl)
        days_row.addStretch()
        param_layout.addLayout(days_row)

        # 行 2：起始日期（仅周期模式可见）
        date_row_widget = QWidget()
        date_row = QHBoxLayout(date_row_widget)
        date_row.setContentsMargins(0, 0, 0, 0)
        date_row.setSpacing(8)
        date_lbl = QLabel("周期起点：")
        date_lbl.setStyleSheet("font-size: 13px; color: #333;")
        date_edit = QDateEdit()
        date_edit.setCalendarPopup(True)
        # 去掉上下步进按钮：那个"隐身"小按钮会误改年份（保留日历下拉箭头）
        date_edit.setButtonSymbols(QAbstractSpinBox.NoButtons)
        date_edit.setDate(QDate.currentDate())
        date_edit.setDisplayFormat("yyyy-MM-dd")
        date_edit.setStyleSheet(
            "QDateEdit { padding: 6px 8px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " background: #FFFFFF; font-size: 13px; }"
            " QDateEdit:focus { border-color: #1A73E8; }"
        )
        date_edit.setFixedWidth(140)
        date_hint = QLabel("（周期将匹配此起点）")
        date_hint.setStyleSheet("font-size: 11px; color: #999;")
        date_row.addWidget(date_lbl)
        date_row.addWidget(date_edit)
        date_row.addWidget(date_hint)
        date_row.addStretch()
        param_layout.addWidget(date_row_widget)

        param_widget.setVisible(False)

        # 切换可见性
        def on_mode_changed():
            if radio_cycle.isChecked():
                param_lbl.setText("每")
                suffix_lbl.setText("天循环")
                date_row_widget.setVisible(True)
                param_widget.setVisible(True)
            elif radio_countdown.isChecked():
                param_lbl.setText("倒计时")
                suffix_lbl.setText("天结束")
                date_row_widget.setVisible(False)
                param_widget.setVisible(True)
            else:
                param_widget.setVisible(False)

        radio_free.toggled.connect(on_mode_changed)
        radio_cycle.toggled.connect(on_mode_changed)
        radio_countdown.toggled.connect(on_mode_changed)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet(
            "QPushButton { padding: 8px 22px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " background: #FFFFFF; font-size: 13px; color: #555; }"
            " QPushButton:hover { background: #F0F0F0; border-color: #B0B0B0; }"
        )
        cancel_btn.clicked.connect(dialog.reject)
        ok_btn = QPushButton("添加")
        ok_btn.setStyleSheet(
            "QPushButton { padding: 8px 26px; border: none; border-radius: 8px;"
            " background: #1A73E8; font-size: 13px; color: #FFFFFF; font-weight: 600; }"
            " QPushButton:hover { background: #1765CC; }"
            " QPushButton:pressed { background: #1557B0; }"
        )
        ok_btn.clicked.connect(dialog.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)

        content.addWidget(name_lbl)
        content.addWidget(name_input)
        content.addWidget(color_lbl)
        content.addLayout(color_layout)
        content.addWidget(mode_lbl)
        content.addLayout(mode_layout)
        content.addWidget(param_widget)
        content.addLayout(btn_layout)

        if dialog.exec() == QDialog.Accepted and name_input.text().strip():
            mode = "free"
            cycle_days = 0
            countdown_end = ""
            cycle_start = ""
            if radio_cycle.isChecked():
                mode = "cycle"
                cycle_days = param_input.value()
                cycle_start = date_edit.date().toPython().strftime("%Y-%m-%d")
            elif radio_countdown.isChecked():
                mode = "countdown"
                end_date = datetime_module.date.today() + datetime_module.timedelta(days=param_input.value())
                countdown_end = end_date.strftime("%Y-%m-%d")
                cycle_start = ""

            habit = {
                "id": uuid.uuid4().hex[:8],
                "name": name_input.text().strip(),
                "color": selected_color[0],
                "records": {},
                "mode": mode,
                "cycle_days": cycle_days,
                "countdown_end": countdown_end,
                "cycle_start": cycle_start if mode == "cycle" else "",
                "cycle_completed": False,
            }
            self._habits.append(habit)
            self._refresh_habit_list()
            self.save_data()

    # ---------- 底部操作栏 ----------

    def _build_bottom_actions(self):
        """底部按钮：新的事务。"""
        bottom = QWidget()
        bottom.setStyleSheet("background: transparent;")
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 6, 0, 0)
        bottom_layout.setSpacing(10)

        bottom_layout.addStretch()

        add_btn = QPushButton("＋ 新的事务")
        add_btn.setStyleSheet(
            "QPushButton { padding: 6px 18px; border-radius: 8px; font-size: 13px; "
            "font-weight: bold; background: #0078D7; color: white; border: none; }"
            "QPushButton:hover { background: #005A9E; }"
        )
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(self._add_habit)
        bottom_layout.addWidget(add_btn)

        frame_layout = self.bg_frame.layout()
        frame_layout.insertWidget(frame_layout.count() - 1, bottom)
        self._bottom_widget = bottom

    # ---------- 持久化 ----------

    def _load_habits(self):
        """从便签 JSON 的 habits_data 字段恢复数据。"""
        if self.save_file and os.path.exists(self.save_file):
            try:
                with open(self.save_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                hd = data.get("habits_data", {})
                self._week_offset = hd.get("week_offset", 0)
                self._habits = hd.get("habits", [])
            except Exception:
                pass
        self._refresh_date_labels()
        self._refresh_habit_list()

    def save_data(self):
        """保存时附加 habits_data。"""
        if getattr(self, '_is_loading', False):
            return
        if self.width() < 250:
            return
        if not os.path.exists(SAVE_DIR):
            os.makedirs(SAVE_DIR)

        title = self.header.title_edit.text()
        new_path = make_save_path(title, exclude_path=self.save_file)
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        if self.save_file and os.path.exists(self.save_file) and os.path.abspath(self.save_file) != os.path.abspath(new_path):
            try:
                os.remove(self.save_file)
            except OSError:
                pass
        self.save_file = new_path

        # 图片路径归一化 + 孤儿清理（与父类保持一致）
        html = self.text_edit.toHtml()
        note_dir = os.path.dirname(self.save_file).replace('\\', '/')
        def _unfile_src(m):
            raw = m.group(1)
            decoded = urllib.parse.unquote(raw)
            for prefix in ('file:///', 'file://', 'file:'):
                if decoded.startswith(prefix):
                    decoded = decoded[len(prefix):]
                    break
            try:
                rel = os.path.relpath(decoded, note_dir.replace('/', os.sep))
                return f'src="{rel.replace(os.sep, "/")}"'
            except ValueError:
                return m.group(0)
        html = re.sub(r'src="(file://[^"]+)"', _unfile_src, html)
        _cleanup_orphan_images(note_dir, html)

        data = {
            "note_id": self.note_id,
            "title": title,
            "html_content": html,
            "x": self.x(), "y": self.y(),
            "width": self.width(), "height": self._persist_height(),
            "is_locked": self.is_locked,
            "is_always_on_top": getattr(self, 'is_always_on_top', True),
            "is_hidden": getattr(self, 'is_hidden', False),
            "is_collapsed": self._is_collapsed_for_save(),
            "stack_id": getattr(self, 'stack_id', ''),
            "stack_pos": int(getattr(self, 'stack_pos', 0) or 0),
            "bg_color": self.bg_color,
            "note_hotkey": getattr(self, '_note_hotkey', ''),
            "habits_data": {
                "week_offset": self._week_offset,
                "habits": self._habits,
            },
        }
        with open(self.save_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def _apply_lock_ui(self):
        """锁定 / 解锁时同步控制事务 UI。"""
        super()._apply_lock_ui()
        if not hasattr(self, '_btn_prev'):
            return  # UI 尚未构建（父类 load_data 早于子类 _build_tracker_grid）
        locked = self.is_locked
        self._btn_prev.setVisible(not locked)
        self._btn_next.setVisible(not locked)
        self._bottom_widget.setVisible(not locked)
        self._refresh_habit_list()
        # 锁定时不显示拖拽手柄
        for h in self._drag_handles.values():
            h.setVisible(not locked)


class ClickableLabel(QLabel):
    """支持点击信号 + 右键菜单的富文本标签（用于新番网格的番剧名 + 集数徽标）。"""

    clicked = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._bgm_item = None    # 绑定番剧数据（含 subject_id），供右键菜单使用
        self._bgm_window = None  # 持有者窗口（BangumiScheduleWindow）

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        if self._bgm_item and self._bgm_window:
            self._bgm_window._show_item_menu(self._bgm_item, event.globalPos())
        else:
            super().contextMenuEvent(event)


def _time_to_min(hhmm):
    """"HH:MM" → 当日分钟数（如 "09:30" → 570）。"""
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 9 * 60


def _min_to_hhmm(mins):
    """当日分钟数 → "HH:MM"。"""
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _make_time_selectors(style=""):
    """生成「时 + 分」两个下拉框（点开直接选，不用按步进箭头试探）。"""
    hour_cb = QComboBox()
    hour_cb.addItems([f"{h:02d}" for h in range(24)])
    minute_cb = QComboBox()
    minute_cb.addItems([f"{m:02d}" for m in range(60)])   # 00 ~ 59 全量
    for cb in (hour_cb, minute_cb):
        cb.setStyleSheet(style)
        cb.setFixedWidth(64)
        cb.setMaxVisibleItems(12)
        cb.setCursor(Qt.PointingHandCursor)
    return hour_cb, minute_cb


def _set_time_selectors(hour_cb, minute_cb, hhmm, default="09:00"):
    """把 "HH:MM" 回填到「时 / 分」下拉框（非 5 分钟整的旧数据临时补进分列表）。"""
    text = hhmm if isinstance(hhmm, str) and ":" in hhmm else default
    try:
        h_str, m_str = text.split(":")[:2]
        h, m = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        h, m = int(default.split(":")[0]), int(default.split(":")[1])
    h, m = max(0, min(23, h)), max(0, min(59, m))
    hour_cb.setCurrentText(f"{h:02d}")
    m_text = f"{m:02d}"
    if minute_cb.findText(m_text) < 0:
        minute_cb.addItem(m_text)
        try:
            minute_cb.model().sort(0)   # 补进来的非整值也按序显示
        except Exception:
            pass
    minute_cb.setCurrentText(m_text)


def _read_time_selectors(hour_cb, minute_cb, default="09:00"):
    """「时 / 分」下拉框 → "HH:MM"（越界值钳制，文本异常回退默认）。"""
    try:
        h = max(0, min(23, int(hour_cb.currentText())))
        m = max(0, min(59, int(minute_cb.currentText())))
        return f"{h:02d}:{m:02d}"
    except (ValueError, TypeError):
        return default


class ScheduleDayHeader(QWidget):
    """日程表日期表头（不跟随滚动）：周视图显示两行（周几 + 日期），今天列高亮。"""

    WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]
    HEADER_H = 34  # 两行高度

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dates = []
        self.time_col_w = 44
        self.week_no = None        # 当前周数（锚点周=第1周；None=不显示）
        self.setFixedHeight(self.HEADER_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_dates(self, dates):
        self.dates = dates
        self.update()

    def set_week_no(self, week_no):
        self.week_no = week_no
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        n = len(self.dates)
        if n == 0:
            return
        col_w = (self.width() - self.time_col_w) / n
        today = datetime_module.date.today()
        # 左侧时间刻度列：周视图且有锚点周时显示「第 N 周」
        if n > 1 and self.week_no is not None:
            painter.save()
            painter.setPen(QColor(150, 150, 150))
            f = painter.font()
            f.setPixelSize(10)
            painter.setFont(f)
            painter.drawText(QRect(0, 0, self.time_col_w, self.HEADER_H),
                             Qt.AlignCenter, f"第{self.week_no}周")
            painter.restore()
        for c, d in enumerate(self.dates):
            x0 = int(self.time_col_w + c * col_w)
            x1 = int(self.time_col_w + (c + 1) * col_w)
            is_today = (d == today)
            if is_today:
                # 今天列：浅蓝圆角底
                painter.fillRect(x0 + 4, 2, max(x1 - x0 - 8, 10),
                                 self.HEADER_H - 4, QColor(232, 244, 253))
        for c, d in enumerate(self.dates):
            x0 = int(self.time_col_w + c * col_w)
            x1 = int(self.time_col_w + (c + 1) * col_w)
            cw = x1 - x0
            is_today = (d == today)
            rect = QRect(x0, 0, cw, self.HEADER_H)
            if is_today:
                painter.setPen(QColor(0, 120, 215))
                # 用真实 Bold 字面（合成粗体在小字号下发虚、笔画粘连）
                font = fonts_mod.make_font(
                    painter.font().family(), painter.font().pixelSize(), "bold"
                )
                painter.setFont(font)
            else:
                painter.setPen(QColor(95, 107, 122))
                font = fonts_mod.make_font(
                    painter.font().family(), painter.font().pixelSize(), "regular"
                )
                painter.setFont(font)
            # 上排：周几
            painter.drawText(rect.adjusted(0, 1, 0, -self.HEADER_H // 2),
                             Qt.AlignCenter, self.WEEKDAYS[d.weekday()])
            # 下排：日期
            painter.drawText(rect.adjusted(0, self.HEADER_H // 2, 0, -1),
                             Qt.AlignCenter, str(d.day))
        # 底边分隔线
        painter.setPen(QPen(QColor(220, 224, 229), 1))
        painter.drawLine(0, self.HEADER_H - 1, self.width(), self.HEADER_H - 1)


class ScheduleGridArea(QWidget):
    """日程表时间轴画布：绘制整点刻度线 + 承载绝对定位的事件块（不含表头）。

    时间轴默认 6:00 - 21:00；若事件超出该范围，_compute_axis 会动态拉长
    起点/终点（取整到整点），翻页后按新视图事件重新计算即自然恢复。
    表头日期行由独立的 `ScheduleDayHeader` 显示（不跟随滚动）。
    """

    resized = Signal()    # 画布尺寸变化（含滚动条出现/消失导致的宽度变化）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.axis_start = 6 * 60      # 轴起点（分钟）
        self.axis_end = 21 * 60       # 轴终点（分钟）
        self.hour_h = 44              # 每小时像素高度（自适应，见 _update_hour_h）
        self.time_col_w = 44          # 左侧时间刻度列宽（与表头一致）
        self.day_cols = 7             # 列数：日视图 1 / 周视图 7
        self._blocks = []             # [(block, col_idx, lane, lane_count)]
        self.pad_top = 16             # 轴首尾留白：保证 6:00 / 21:00 标签完整渲染
        self.pad_bottom = 16
        self.MIN_HOUR_H = 18          # 每小时最小像素（防止过度压缩）
        self.MAX_HOUR_H = 80          # 每小时最大像素（拉长时行距更宽，清晰度更高）
        self._updating_scale = False  # 防 setMinimumHeight 触发递归 resize
        self.show_now_line = False    # 当前视图包含今天时绘制"现在"时间指示线
        self.setMouseTracking(True)   # hover 左侧时间指针时浮出当前时间
        self.setMinimumHeight(120)

    # ---------- 几何 ----------

    def total_minutes(self):
        return self.axis_end - self.axis_start

    def total_height(self):
        return (self.pad_top + self.pad_bottom
                + max(int(self.total_minutes() / 60 * self.hour_h), 120))

    def _viewport_height(self):
        """向上查找 QScrollArea 的视口高度（画布实际可见区域高度）。"""
        p = self.parent()
        while p is not None:
            if isinstance(p, QScrollArea):
                return p.viewport().height()
            p = p.parent()
        return self.height()

    def _update_hour_h(self):
        """自适应缩放：按视口可见高度反推每小时像素，让整日行程尽量完整可见。

        窗口越大 → 每小时像素越多（更清晰）；窗口越小 → 压缩到下限 18px，
        此时整日总高超出视口，才出现滚动条。
        """
        if self._updating_scale:
            return
        self._updating_scale = True
        try:
            avail = self._viewport_height() - self.pad_top - self.pad_bottom
            hours = self.total_minutes() / 60.0
            if hours <= 0 or avail <= 0:
                return
            self.hour_h = max(self.MIN_HOUR_H, min(self.MAX_HOUR_H, avail / hours))
            # 需要的最小高度：小时高未到下限时 = 视口高（无滚动）；到下限后 = 更高（出现滚动）
            need = self.pad_top + self.pad_bottom + int(hours * self.hour_h)
            self.setMinimumHeight(max(need, 120))
            self.update()
            self._relayout()
        finally:
            self._updating_scale = False

    def set_axis(self, start_min, end_min):
        self.axis_start = start_min
        self.axis_end = end_min
        # 时间范围变化后重新缩放适配（如拉长到 5:00-23:00 时缩小比例保持完整可见）
        self._update_hour_h()
        self.update()

    def set_day_cols(self, n):
        self.day_cols = max(1, n)
        self.update()
        self._relayout()

    def _col_width(self):
        area_w = max(self.width() - self.time_col_w, 50)
        return area_w / self.day_cols

    # ---------- 事件块管理 ----------

    def clear_blocks(self):
        for blk, *_ in self._blocks:
            blk.deleteLater()
        self._blocks = []

    def add_block(self, block, col_idx, lane, lane_count):
        self._blocks.append((block, col_idx, lane, lane_count))
        block.setParent(self)
        block.show()
        self._relayout()

    def _relayout(self):
        col_w = self._col_width()
        for block, col_idx, lane, lane_count in self._blocks:
            ev = block.event_data
            s = _time_to_min(ev.get("start", "09:00"))
            e = _time_to_min(ev.get("end", "10:00"))
            y = self.pad_top + int((s - self.axis_start) / 60 * self.hour_h)
            h = max(int((e - s) / 60 * self.hour_h), 18)
            w = max(int(col_w / lane_count) - 2, 24)
            x = int(self.time_col_w + col_idx * col_w + lane * (w + 2))
            block.setGeometry(x, y + 1, w, h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_hour_h()
        self.resized.emit()

    # ---------- 绘制 ----------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        start_h = self.axis_start // 60
        end_h = (self.axis_end + 59) // 60
        # 刻度密度自适应：每小时像素 < 26 时改为每 2 小时一条线，避免缩小后过于密集
        step = 1 if self.hour_h >= 26 else 2
        for hh in range(start_h, end_h + 1, step):
            y = self.pad_top + int((hh * 60 - self.axis_start) / 60 * self.hour_h)
            # 左侧时间标签
            painter.setPen(QColor(150, 150, 150))
            painter.drawText(
                QRect(2, y - 8, self.time_col_w - 8, 16),
                Qt.AlignRight | Qt.AlignVCenter, f"{hh:02d}:00"
            )
            # 整点横线
            painter.setPen(QPen(QColor(228, 228, 228), 1))
            painter.drawLine(self.time_col_w, y, self.width(), y)
        # 列分隔线
        col_w = self._col_width()
        for c in range(1, self.day_cols):
            x = self.time_col_w + int(c * col_w)
            painter.setPen(QPen(QColor(240, 240, 240), 1))
            painter.drawLine(x, self.pad_top, x, self.height() - self.pad_bottom)
        # 当前时间指示线（浅灰细线横跨 + 左侧小箭头指针），仅当前视图包含今天时绘制
        if self.show_now_line:
            import datetime as _dt
            _now = _dt.datetime.now()
            _mins = _now.hour * 60 + _now.minute
            if self.axis_start <= _mins <= self.axis_end:
                _y = self.pad_top + int((_mins - self.axis_start) / 60 * self.hour_h)
                painter.setPen(QPen(QColor(192, 192, 192), 0.8))
                painter.drawLine(self.time_col_w, _y, self.width(), _y)
                # 左侧小箭头指针（实心三角，尖端指向时间线起点；仅约两个数字宽，不横跨整个刻度列）
                _aw = 15
                _tri = QPolygon()
                _tri << QPoint(self.time_col_w - _aw, _y - 4) \
                     << QPoint(self.time_col_w - _aw, _y + 4) \
                     << QPoint(self.time_col_w - 3, _y)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(192, 192, 192))
                painter.drawPolygon(_tri)
        super().paintEvent(event)

    def mouseMoveEvent(self, event):
        """悬停在左侧时间指针附近时，浮出当前时间（精确到分钟）。"""
        if self.show_now_line:
            pos = event.pos()
            if pos.x() < self.time_col_w + 6:
                import datetime as _dt
                _now = _dt.datetime.now()
                _mins = _now.hour * 60 + _now.minute
                if self.axis_start <= _mins <= self.axis_end:
                    _y = self.pad_top + int((_mins - self.axis_start) / 60 * self.hour_h)
                    if abs(pos.y() - _y) < 10:
                        QToolTip.showText(event.globalPos(), _now.strftime("%H:%M"))
                        return
        super().mouseMoveEvent(event)


def _wrap_text_lines(fm, text, width, max_lines):
    """按像素宽度把文本折行，返回行列表。

    - 中英文混排逐字符累加，超出宽度即换行（英文单词会被按字符切开，
      但事件名普遍较短，视觉上比整词换行更省空间）。
    - 超过 max_lines 时截断，返回的最后一行末尾带省略号（"…"）。
    """
    lines = []
    if not text or width <= 0 or max_lines < 1:
        return lines
    cur = ""
    i = 0
    n = len(text)
    truncated = False
    while i < n:
        ch = text[i]
        if ch == "\n":                      # 显式换行
            lines.append(cur)
            cur = ""
            i += 1
            if len(lines) >= max_lines:
                truncated = i < n
                break
            continue
        if cur and fm.horizontalAdvance(cur + ch) > width:
            lines.append(cur)
            cur = ""
            if len(lines) >= max_lines:
                truncated = True
                break
            continue                        # 当前字符留到下一行重新判断
        cur += ch
        i += 1
    if not truncated:
        if cur or not lines:
            lines.append(cur)
        return lines
    # 已截断：把最后一行按宽度补省略号
    ell = "…"
    ell_w = fm.horizontalAdvance(ell)
    last = lines[-1] if lines else ""
    while last and fm.horizontalAdvance(last) + ell_w > width:
        last = last[:-1]
    if lines:
        lines[-1] = last + ell
    else:
        lines.append(ell)
    return lines


class ScheduleBlock(QFrame):
    """日程表事件块：彩色圆角色块，左键标记完成，右键弹出编辑菜单。

    hover 时在事件条上下显示具体开始/结束时间（默认隐藏）。
    date 参数：该块对应的事件日期（重复事件按日期判断完成状态）。
    事件名按块尺寸自动折行/收缩字号，尽量在同一块内完整显示。
    """

    clicked = Signal()

    def __init__(self, event_data, expanded, parent=None, date=None):
        super().__init__(parent)
        self.event_data = event_data
        self.expanded = expanded      # 日视图 True（展开备注）/ 周视图 False（缩略）
        self.date = date              # 该块对应的事件日期（datetime.date）
        self._window = None
        self._last_fit_size = None    # 上次自适应时的块尺寸（避免重复计算）
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self._hover = False
        self._build_ui()
        self._apply_style()

    def _is_done(self):
        """该日期下事件是否已完成（重复事件查 done_dates，单次查 done）。"""
        ev = self.event_data
        if ev.get("repeat") and ev.get("repeat") != "none":
            dk = self.date.strftime("%Y-%m-%d") if self.date else ""
            return bool((ev.get("done_dates") or {}).get(dk, False))
        return bool(ev.get("done", False))

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        ev = self.event_data
        # 顶部时间标签（hover 显示具体开始时间，左上角、无底色）
        self._start_lbl = QLabel(ev.get("start", "09:00"))
        self._start_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._start_lbl.setVisible(False)
        layout.addWidget(self._start_lbl)
        # 标题：字号自适应 + 自动折行（由 _fit_content 按块尺寸计算）
        # ⚠️ 尺寸策略不能用 Ignored —— QWidgetItem::isEmpty() 对水平 Ignored 的子件返回 True，
        # 布局会直接跳过它（分到 0 尺寸）。这里用 垂直 Expanding（吃掉剩余高度）+
        # 最小尺寸 0（允许被压到很小），宽度由块几何决定，不会反向撑大块。
        self._title_lbl = QLabel(ev.get("title", ""))
        self._title_lbl.setWordWrap(False)
        self._title_lbl.setTextFormat(Qt.PlainText)   # 标题按纯文本渲染，避免被当作富文本
        self._title_lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._title_lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._title_lbl.setMinimumSize(0, 0)
        layout.addWidget(self._title_lbl, 1)     # 拉伸因子 1：占满时间/备注之外的全部空间
        # 展开模式：时间范围 + 备注
        if self.expanded:
            self._time_lbl = QLabel(f"{ev.get('start', '09:00')} - {ev.get('end', '10:00')}")
            self._time_lbl.setStyleSheet("background: transparent; border: none; font-size: 11px;")
            self._time_lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            layout.addWidget(self._time_lbl)
            note = ev.get("note", "")
            if note:
                self._note_lbl = QLabel(note)
                self._note_lbl.setWordWrap(True)
                # 高度策略 Maximum：备注按需占高、不抢伸展空间，剩余高度全部留给标题
                self._note_lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
                self._note_lbl.setStyleSheet("background: transparent; border: none; font-size: 12px;")
                layout.addWidget(self._note_lbl)
        # 底部时间标签（hover 显示具体结束时间，左下角、无底色）
        self._end_lbl = QLabel(ev.get("end", "10:00"))
        self._end_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._end_lbl.setVisible(False)
        layout.addWidget(self._end_lbl)

    def _apply_style(self):
        ev = self.event_data
        color = ev.get("color", "#0078D7")
        done = self._is_done()
        if done:
            bg = "#ECECEC"
            border = "#BDBDBD"
            text = "#9E9E9E"
            time_text = "#9E9E9E"
        else:
            c = QColor(color)
            bg = f"rgba({c.red()},{c.green()},{c.blue()},60)"
            border = color
            text = "#333333"
            # hover 时间标签：无底色，用与标题同色的深色文字
            time_text = "#555555"
        self.setStyleSheet(
            f"QFrame {{ background: {bg}; border: none;"
            f" border-left: 3px solid {border}; border-radius: 5px; }}"
            f"QFrame:hover {{ background: {bg}; border: 1px solid {border};"
            f" border-left: 3px solid {border}; }}"
        )
        self._title_lbl.setStyleSheet(
            f"background: transparent; border: none; color: {text};"
        )
        # 字号不写进 QSS（避免覆盖自适应字号），统一由 _title_font 设置
        self._title_lbl.setFont(self._title_font(13 if self.expanded else 12))
        # 时间标签：无底色、小字、左上/左下角
        time_style = (
            f"background: transparent; border: none; color: {time_text};"
            f" font-size: 10px; font-weight: bold;"
        )
        self._start_lbl.setStyleSheet(time_style)
        self._end_lbl.setStyleSheet(time_style)

    def _title_font(self, px):
        """构造标题字体：字号按块尺寸自适应（像素级，避免 QSS 覆盖）。

        走 fonts_mod.make_font 命中真实 Bold 字面，避免合成粗体糊笔画；
        字体族沿用控件自身的（日程表可被用户改字体）。
        """
        fam = self._title_lbl.font().family()
        return fonts_mod.make_font(fam, px=int(px), weight="bold")

    def _sync_layout(self):
        """立即让布局按当前可见性重排（同步拿到子件真实几何）。

        注意：单独调用 QLayout.activate() 不会重排——布局未被标记失效时直接返回，
        读到的仍是旧几何。必须先 invalidate() 再 activate()。
        """
        layout = self.layout()
        if layout is None:
            return
        layout.invalidate()
        layout.activate()

    def _fit_content(self):
        """按块的实际尺寸排布内容：优先让事件名在同一块内完整显示。

        依次尝试（越靠前信息越全）：
          1. 保留时间行 + 备注，标题自动折行，字号 13→9 逐级收缩；
          2. 名字仍放不下 → 收起备注，把空间让给标题；
          3. 还放不下 → 连时间行一起收起，只留名字；
          4. 极限兜底：最小字号 + 末行省略号（尽量多显示字符、不硬切）。

        可用宽高直接取标题标签的真实几何（已扣除布局边距与 QSS 左边框），
        避免按块宽估算导致末字被裁。
        """
        layout = self.layout()
        size = (self.width(), self.height())
        if size == self._last_fit_size:
            return
        self._last_fit_size = size

        # hover 时间标签只在鼠标悬停时出现，会临时挤占高度。
        # 自适应一律按"非 hover 稳态"计算，否则悬停时改窗口大小会把标题字号误缩一档。
        hover_was = (not self._start_lbl.isHidden(), not self._end_lbl.isHidden())
        if hover_was[0]:
            self._start_lbl.setVisible(False)
        if hover_was[1]:
            self._end_lbl.setVisible(False)
        try:
            self._fit_content_inner(layout)
        finally:
            if hover_was[0]:
                self._start_lbl.setVisible(True)
            if hover_was[1]:
                self._end_lbl.setVisible(True)

    def _fit_content_inner(self, layout):
        """_fit_content 的实际计算部分（已排除 hover 标签干扰）。"""

        # 矮块收紧内边距/间距，腾出足够的行高（否则 18px 的块放不下一行字）
        h = self.height()
        m_v, sp = (1, 1) if h < 26 else ((3, 2) if h < 40 else (4, 2))
        if layout is not None and layout.contentsMargins().top() != m_v:
            layout.setContentsMargins(6, m_v, 6, m_v)
            layout.setSpacing(sp)

        title = (self.event_data.get("title") or "").strip()
        has_time = hasattr(self, "_time_lbl")
        has_note = hasattr(self, "_note_lbl")
        sizes = (13, 12, 11, 10) if self.expanded else (12, 11, 10, 9)

        if has_time and has_note:
            combos = [(True, True), (True, False), (False, False)]
        elif has_time:
            combos = [(True, False), (False, False)]
        elif has_note:
            combos = [(False, True), (False, False)]
        else:
            combos = [(False, False)]

        chosen = None
        for keep_time, keep_note in combos:
            if has_time:
                self._time_lbl.setVisible(keep_time)
            if has_note:
                self._note_lbl.setVisible(keep_note)
            self._sync_layout()                 # 立即按新可见性重排（见 _sync_layout 说明）
            w = max(self._title_lbl.width(), 10)
            avail = self._title_lbl.height()
            if avail < 8:
                continue
            for px in sizes:
                fm = QFontMetrics(self._title_font(px))
                if fm.lineSpacing() > avail + 1:    # 该字号一行都塞不进 → 换更小字号
                    continue
                max_lines = max(1, int(avail // max(fm.lineSpacing(), 1)))
                lines = _wrap_text_lines(fm, title, w, 10 ** 6)
                if len(lines) <= max_lines:         # 完整放得下（无省略号）
                    chosen = (keep_time, keep_note, px, lines)
                    break
            if chosen:
                break

        if chosen is None:                  # 兜底：只留名字 + 最小字号 + 省略号
            if has_time:
                self._time_lbl.setVisible(False)
            if has_note:
                self._note_lbl.setVisible(False)
            self._sync_layout()
            px = sizes[-1]
            fm = QFontMetrics(self._title_font(px))
            w = max(self._title_lbl.width(), 10)
            avail = max(self._title_lbl.height(), fm.lineSpacing())
            max_lines = max(1, int(avail // max(fm.lineSpacing(), 1)))
            chosen = (False, False, px,
                      _wrap_text_lines(fm, title, w, max_lines))

        keep_time, keep_note, px, lines = chosen
        if has_time:
            self._time_lbl.setVisible(keep_time)
        if has_note:
            self._note_lbl.setVisible(keep_note)
        self._title_lbl.setFont(self._title_font(px))
        self._title_lbl.setText("\n".join(lines))
        # 名字被省略号截断时，悬停可看全名
        self._title_lbl.setToolTip(title if "…" in "".join(lines) else "")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_content()

    def enterEvent(self, event):
        self._hover = True
        self._start_lbl.setVisible(True)
        self._end_lbl.setVisible(True)

    def leaveEvent(self, event):
        self._hover = False
        self._start_lbl.setVisible(False)
        self._end_lbl.setVisible(False)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        if self._window:
            self._window._show_block_menu(self.event_data, event.globalPos())
        else:
            super().contextMenuEvent(event)


class ScheduleWindow(AniNoteWindow):
    """日程表便签窗口，继承便签的全部功能。

    支持日视图（事件展开备注）与周视图（课程表缩略，7 列按时间分布）
    双模式切换；时间轴默认 6:00-21:00，事件超出时自动拉长，翻页恢复。
    数据存储在便签 JSON 的 schedule_data 中。
    """

    WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

    def __init__(self, note_id=None):
        nid = note_id if note_id else f"schedule_{uuid.uuid4().hex[:8]}"
        super().__init__(note_id=nid)

        self.setMinimumSize(520, 360)
        self.text_edit.hide()
        self.format_panel.hide()

        # 状态
        self._view_mode = "week"      # "day" | "week"
        self._center_date = datetime_module.date.today()
        self._events = []             # [{id, title, date, start, end, note, color, done}]
        self._rebuilding = False      # 防 resizeEvent 递归
        self._anchor_week = None      # 起始周锚点（该周周一，date 或 None；None=未设置）

        # 网格（插入到 text_edit 原位置）
        self._build_schedule_grid()

        # 恢复数据
        self._load_schedule()

        # 首次创建：浅蓝主题 + 默认标题
        if not (self.save_file and os.path.exists(self.save_file)):
            self.is_always_on_top = False
            self.apply_window_states()   # 属性改了必须同步窗口 flag，否则置顶状态错乱
            self.bg_color = [235, 245, 255, 242]
            self._apply_bg_color()
            self.header.title_edit.setText("日程表")
        self._apply_lock_ui()

        self._refresh_view()

        QTimer.singleShot(0, self._show_if_not_hidden)

        # 提醒检查：每 20 秒扫描一次，到点触发系统通知
        self._remind_fired = set()   # 已触发的 (event_id, date_str) 组合，避免重复提醒
        self._remind_timer = QTimer(self)
        self._remind_timer.setInterval(20000)
        self._remind_timer.timeout.connect(self._check_reminders)
        self._remind_timer.start()

        # 当前时间指示线：每 30 秒重绘一次（分钟级平滑移动）
        self._now_timer = QTimer(self)
        self._now_timer.setInterval(30000)
        self._now_timer.timeout.connect(self._refresh_now_line)
        self._now_timer.start()
        self._last_seen_day = datetime_module.date.today()   # 跨天检测基准

    def _refresh_now_line(self):
        """重绘当前时间指示线（30 秒定时触发，随真实时间前进）。

        同时做跨天检测：程序长时间开着时，日视图若停留在旧日期，
        自动跳到当天（周视图不动——本来就含今天）。
        """
        if hasattr(self, '_grid_area'):
            self._grid_area.update()
        today = datetime_module.date.today()
        if getattr(self, '_last_seen_day', None) != today:
            self._last_seen_day = today
            if self._view_mode == "day" and self._center_date != today:
                self._center_date = today
                self._refresh_view()

    def _init_bangumi_mode(self):
        pass

    def _apply_lock_ui(self):
        super()._apply_lock_ui()
        self.format_panel.hide()
        if not hasattr(self, '_top_widget'):
            return
        locked = self.is_locked
        # 锁定下保留顶部工具行（今天/日/周/◀▶/日期范围）和日期表头（保留日周视图点击切换），
        # 仅隐藏底部"新的事件"按钮（编辑/新建入口全部关闭）
        self._bottom_widget.setVisible(not locked)
        # 左键标记完成保留可用（对齐新番便签：锁定不影响条目标记）

    # 网格模式无文本编辑区：字体格式操作一律忽略
    def change_font_family(self, font):
        pass

    # ---------- 便签设置（起始周/锚点周） ----------

    def _append_extra_settings(self, content):
        """日程表附加设置行：起始周（锚点周）。"""
        lbl = QLabel("起始周（锚点周）")
        lbl.setStyleSheet("font-size: 13px; color: #555; font-weight: 600;")
        content.addWidget(lbl)

        self._anchor_enable = QCheckBox("启用：该周视为第 1 周")
        self._anchor_enable.setStyleSheet("font-size: 12px; color: #333;")
        self._anchor_enable.setChecked(self._anchor_week is not None)
        content.addWidget(self._anchor_enable)

        self._anchor_edit = QDateEdit()
        self._anchor_edit.setCalendarPopup(True)
        self._anchor_edit.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._anchor_edit.setDisplayFormat("yyyy-MM-dd")
        if self._anchor_week:
            d = self._anchor_week
            self._anchor_edit.setDate(QDate(d.year, d.month, d.day))
        else:
            self._anchor_edit.setDate(QDate.currentDate())
        self._anchor_edit.setStyleSheet(
            "QDateEdit { padding: 8px 12px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " font-size: 14px; background: #FFFFFF; }"
            " QDateEdit:focus { border-color: #1A73E8; }"
        )
        self._anchor_edit.setEnabled(self._anchor_week is not None)
        self._anchor_enable.toggled.connect(self._anchor_edit.setEnabled)
        content.addWidget(self._anchor_edit)

        hint = QLabel("选择该周任意一天即可（自动取周一为锚点）；"
                      "周视图表头与每周重复的起始/结束周按它计算第 N 周")
        hint.setStyleSheet("font-size: 11px; color: #999;")
        hint.setWordWrap(True)
        content.addWidget(hint)

    def _save_extra_settings(self):
        """保存起始周设置：勾选则取所选日期所在周的周一为锚点，否则清除。"""
        if not hasattr(self, '_anchor_enable'):
            return
        if self._anchor_enable.isChecked():
            qd = self._anchor_edit.date()
            d = datetime_module.date(qd.year(), qd.month(), qd.day())
            self._anchor_week = d - datetime_module.timedelta(days=d.weekday())
        else:
            self._anchor_week = None
        self._mark_dirty()
        self.save_data()
        self._refresh_view()

    def change_font_size(self, size):
        pass

    def change_font_color_direct(self, hex_color):
        pass

    def _show_if_not_hidden(self):
        if not getattr(self, 'is_hidden', False):
            # 淡入显示（不再直入直出）
            self.animated_show()
            self.raise_()
            self.activateWindow()

    # ---------- 网格构建 ----------

    def _build_schedule_grid(self):
        # 外层容器：顶部工具行（固定） + 日期表头（固定，不跟随滚动） + 滚动区 + 底部操作栏（固定）
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(4)

        # 顶部工具行（不跟随滚动）
        self._build_top_bar(outer)

        # 日期表头（两行：周几 + 日期；今天高亮；不跟随滚动）
        self._day_header = ScheduleDayHeader()
        outer.addWidget(self._day_header)

        # 滚动区：仅包含时间轴画布（超高时滚动，工具行/日期表头/底部按钮保持可见）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: transparent; width: 5px; margin: 0; }
            QScrollBar::handle:vertical { background: #D0D0D0; border-radius: 2px; min-height: 20px; }
            QScrollBar::handle:vertical:hover { background: #A0A0A0; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)
        # 滚动条自动隐藏：未滚动时透明，滚动时显示，停 1.2s 后隐藏（对齐 MD 便签）
        setup_auto_hide_scrollbar(
            scroll.verticalScrollBar(), scroll.styleSheet(),
            "QScrollBar:vertical { background: transparent; width: 5px; margin: 0; }"
            " QScrollBar::handle:vertical { background: transparent; border-radius: 2px; min-height: 20px; }"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            " QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }",
        )

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        self._grid_area = ScheduleGridArea()
        inner_layout.addWidget(self._grid_area)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        # 监听 viewport resize：viewport 宽度即画布可用宽度（已扣除滚动条占位），
        # 用它同步日期表头宽度，确保两列对齐
        scroll.viewport().installEventFilter(self)
        self._scroll_viewport = scroll.viewport()

        # 底部操作栏（不跟随滚动）
        self._build_bottom_actions(outer)

        frame_layout = self.bg_frame.layout()
        idx = frame_layout.indexOf(self.editor_host)
        if idx < 0:
            idx = frame_layout.indexOf(self.text_edit)
        frame_layout.insertWidget(idx, container)
        self.editor_host.hide()
        self._tracker_scroll = scroll
        self._tracker_container = container

    def eventFilter(self, obj, event):
        """监听滚动区 viewport 尺寸变化，同步日期表头宽度（列对齐）。

        在 Resize 事件里 viewport 宽度已是新值，直接同步无需延迟。
        """
        if obj is getattr(self, '_scroll_viewport', None) and event.type() == QEvent.Resize:
            self._sync_day_header_width()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        """窗口缩放时驱动时间轴自适应缩放 + 日期表头宽度同步。

        注意：不能只依赖 grid_area 自身的 resizeEvent——窗口缩小时画布可能被
        minimumHeight 撑住不触发 resize，因此由窗口级 resize 统一驱动。
        """
        super().resizeEvent(event)
        if hasattr(self, '_grid_area') and self._grid_area is not None:
            self._grid_area._update_hour_h()
        self._sync_day_header_width()

    def _sync_day_header_width(self):
        """同步日期表头宽度到画布可用宽度（viewport 宽度，含滚动条占位差异）。"""
        if hasattr(self, '_day_header') and self._day_header is not None:
            vp = getattr(self, '_scroll_viewport', None)
            w = vp.width() if vp is not None else self._grid_area.width()
            self._day_header.setFixedWidth(w)

    def _build_top_bar(self, outer):
        top = QWidget()
        top.setStyleSheet("background: transparent;")
        row = QHBoxLayout(top)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(6)

        # 左侧：今天 + 日/周切换
        today_btn = QPushButton("今天")
        today_btn.setStyleSheet(
            "QPushButton { border: none; border-radius: 6px; padding: 3px 10px;"
            " font-size: 12px; color: #0078D7; background: #E8F4FD; font-weight: bold; }"
            "QPushButton:hover { background: #D0E9FB; }"
        )
        today_btn.setCursor(Qt.PointingHandCursor)
        today_btn.clicked.connect(self._go_today)
        row.addWidget(today_btn)

        seg = QWidget()
        seg.setStyleSheet("background: transparent;")
        seg_layout = QHBoxLayout(seg)
        seg_layout.setContentsMargins(0, 0, 0, 0)
        seg_layout.setSpacing(2)
        self._btn_day = QPushButton("日")
        self._btn_week = QPushButton("周")
        seg_style = (
            "QPushButton { border: 1px solid #D0D0D0; border-radius: 6px; padding: 3px 12px;"
            " font-size: 12px; color: #777; background: #FFFFFF; }"
            "QPushButton:hover { background: #F0F0F0; }"
            "QPushButton:checked { background: #0078D7; color: white; border-color: #0078D7;"
            " font-weight: bold; }"
        )
        self._btn_day.setCheckable(True)
        self._btn_week.setCheckable(True)
        self._btn_day.setStyleSheet(seg_style)
        self._btn_week.setStyleSheet(seg_style)
        self._btn_day.clicked.connect(lambda: self._switch_view("day"))
        self._btn_week.clicked.connect(lambda: self._switch_view("week"))
        seg_layout.addWidget(self._btn_day)
        seg_layout.addWidget(self._btn_week)
        row.addWidget(seg)

        row.addStretch(1)

        # 右侧：◀ 日期范围 ▶
        btn_style = (
            "QPushButton { border: none; background: transparent; font-size: 16px; "
            "color: #888; padding: 2px 8px; }"
            "QPushButton:hover { color: #333; background: rgba(0,0,0,0.05); border-radius: 4px; }"
        )
        self._btn_prev = QPushButton("◀")
        self._btn_prev.setStyleSheet(btn_style)
        self._btn_prev.setToolTip("向前一天/一周")
        self._btn_prev.clicked.connect(self._prev_step)
        row.addWidget(self._btn_prev)

        self._range_lbl = QLabel()
        self._range_lbl.setAlignment(Qt.AlignCenter)
        self._range_lbl.setStyleSheet(
            "color: #333; font-size: 14px; font-weight: bold; background: transparent;"
        )
        row.addWidget(self._range_lbl)

        self._btn_next = QPushButton("▶")
        self._btn_next.setStyleSheet(btn_style)
        self._btn_next.setToolTip("向后一天/一周")
        self._btn_next.clicked.connect(self._next_step)
        row.addWidget(self._btn_next)

        outer.addWidget(top)
        self._top_widget = top

    def _build_bottom_actions(self, outer):
        bottom = QWidget()
        bottom.setStyleSheet("background: transparent;")
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 2, 0, 2)
        bottom_layout.addStretch()
        add_btn = QPushButton("＋ 新的事件")
        add_btn.setStyleSheet(
            "QPushButton { padding: 6px 18px; border-radius: 8px; font-size: 13px; "
            "font-weight: bold; background: #0078D7; color: white; border: none; }"
            "QPushButton:hover { background: #005A9E; }"
        )
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.clicked.connect(lambda: self._show_event_dialog(None))
        bottom_layout.addWidget(add_btn)
        outer.addWidget(bottom)
        self._bottom_widget = bottom

    # ---------- 视图计算 ----------

    def _visible_dates(self):
        """当前视图的日期列表。日视图：[center_date]；周视图：center_date 所在周 7 天。"""
        if self._view_mode == "day":
            return [self._center_date]
        monday = self._center_date - datetime_module.timedelta(days=self._center_date.weekday())
        return [monday + datetime_module.timedelta(days=i) for i in range(7)]

    def _events_for_date(self, date):
        """返回在指定日期出现的事件（含重复展开）。

        事件 repeat 规则：
          "none" / 缺省   → 仅事件本身日期出现
          "daily"         → 事件日期当天及之后每天出现
          "weekly"        → 事件日期当天及之后每周同星期几出现
          "monthly"       → 事件日期当天及之后每月同日出现
          "yearly"        → 事件日期当天及之后每年同月同日出现
        """
        out = []
        for ev in self._events:
            try:
                base = datetime_module.date.fromisoformat(ev.get("date", ""))
            except ValueError:
                continue
            repeat = ev.get("repeat", "none")
            if date < base:
                continue
            # 兼容旧数据（repeat 未物化的周期事件）也应用重复边界
            if repeat != "none" and not self._in_repeat_bounds(date, ev):
                continue
            if repeat == "daily":
                out.append(ev)
            elif repeat == "weekly":
                if date.weekday() == base.weekday():
                    out.append(ev)
            elif repeat == "monthly":
                if date.day == base.day:
                    out.append(ev)
            elif repeat == "yearly":
                if date.month == base.month and date.day == base.day:
                    out.append(ev)
            else:
                if date == base:
                    out.append(ev)
        return out

    def _compute_axis(self, events):
        """根据当前视图事件计算时间轴范围（分钟），默认 6:00-21:00，超出则拉长到整点。"""
        start = 6 * 60
        end = 21 * 60
        for ev in events:
            s = _time_to_min(ev.get("start", "09:00"))
            e = _time_to_min(ev.get("end", "10:00"))
            if s < start:
                start = (s // 60) * 60
            if e > end:
                end = ((e + 59) // 60) * 60
        return start, end

    def _assign_lanes(self, events):
        """同列事件贪心分道，返回 {event_id: lane_index}，重叠事件横向避让。"""
        sorted_evs = sorted(events, key=lambda e: _time_to_min(e.get("start", "09:00")))
        lane_ends = []
        lane_of = {}
        for ev in sorted_evs:
            s = _time_to_min(ev.get("start", "09:00"))
            e = _time_to_min(ev.get("end", "10:00"))
            placed = False
            for i, le in enumerate(lane_ends):
                if s >= le:
                    lane_ends[i] = max(le, e)
                    lane_of[ev["id"]] = i
                    placed = True
                    break
            if not placed:
                lane_ends.append(e)
                lane_of[ev["id"]] = len(lane_ends) - 1
        return lane_of

    @staticmethod
    def _repeat_matches(date, base, repeat):
        """判断 date 是否符合 repeat 规则的某一期（相对 base 起始日）。

        monthly 通用规则：匹配 min(base.day, 该月最后一天)。
        例：31 号起始 → 2 月 28/29、4 月 30；30 号起始 → 2 月 28/29；
            29 号起始 → 2 月 28（非闰年）/29（闰年）；28 号起始 → 每月 28。
        """
        if repeat == "daily":
            return True
        if repeat == "weekly":
            return date.weekday() == base.weekday()
        if repeat == "monthly":
            import calendar
            last_day = calendar.monthrange(date.year, date.month)[1]
            return date.day == min(base.day, last_day)
        if repeat == "yearly":
            return date.month == base.month and date.day == base.day
        return False

    def _week_number(self, d):
        """返回 d 所在周的周数（锚点周 = 第 1 周）。

        未设置锚点 / d 早于锚点周 → 返回 None（无特别显示）。
        """
        aw = self._anchor_week
        if not aw:
            return None
        monday = d - datetime_module.timedelta(days=d.weekday())
        if monday < aw:
            return None
        return (monday - aw).days // 7 + 1

    def _in_repeat_bounds(self, cur, ev):
        """判断 cur 是否在事件的重复边界内（终止日期 / 周数 / 年份）。

        边界字段均可选（缺失 = 不限）：
          repeat_until : 终止日期（每天/每月/每周）
          week_start/end : 起始/结束周数（每周，需设置锚点周才生效）
          year_start/end : 起始/终止年（每年）
        """
        until = ev.get("repeat_until") or ""
        if until:
            try:
                if cur > datetime_module.date.fromisoformat(until):
                    return False
            except ValueError:
                pass
        if self._anchor_week:
            ws = int(ev.get("week_start") or 0)
            we = int(ev.get("week_end") or 0)
            if ws or we:
                n = self._week_number(cur)
                if n is None:
                    return False  # 锚点周之前
                if ws and n < ws:
                    return False
                if we and n > we:
                    return False
        ys = int(ev.get("year_start") or 0)
        ye = int(ev.get("year_end") or 0)
        if ys and cur.year < ys:
            return False
        if ye and cur.year > ye:
            return False
        return True

    def _materialize_repeat(self, ev, batch_id):
        """将重复事件物化为实体条目（起始日 → 起始日 + 400 天，受重复边界限制）。

        每个匹配日期生成一条独立实体（repeat 置 none、独立 id、共享 batch_id），
        让每个日子都有真实条目，可单独编辑 / 删除 / 标记完成。
        done_dates 按日期映射到对应实体的 done。
        重复边界（repeat_until / week_start/end / year_start/end）在此过滤。
        """
        base = datetime_module.date.fromisoformat(ev["date"])
        end = base + datetime_module.timedelta(days=400)
        # 物化窗口按重复边界延伸：终止日期 / 每年终止年（默认 400 天不够覆盖）
        until = ev.get("repeat_until") or ""
        if until:
            try:
                u = datetime_module.date.fromisoformat(until)
                if u > end:
                    end = u
            except ValueError:
                pass
        if ev.get("repeat") == "yearly":
            ye = int(ev.get("year_end") or 0)
            if ye:
                ye_end = datetime_module.date(ye, 12, 31)
                if ye_end > end:
                    end = ye_end
        done_dates = ev.get("done_dates") or {}
        rep = ev.get("repeat", "none")
        items = []
        cur = base
        guard = 0
        while cur <= end and guard < 4000:
            if (self._repeat_matches(cur, base, rep)
                    and self._in_repeat_bounds(cur, ev)):
                item = dict(ev)
                item["id"] = uuid.uuid4().hex[:8]
                item["date"] = cur.strftime("%Y-%m-%d")
                item["repeat"] = "none"        # 实体不再按周期展开
                item["batch_id"] = batch_id    # 批次标识（用于整体替换/删除）
                # 记录所属系列的重复规则：实体自身 repeat 为 none，
                # 编辑单条时据此回显原规则（否则下拉只能显示"不重复"，需重设）
                item["series_repeat"] = rep
                dk = cur.strftime("%Y-%m-%d")
                item["done"] = bool(done_dates.get(dk, False))
                item.pop("done_dates", None)
                items.append(item)
            cur += datetime_module.timedelta(days=1)
            guard += 1
        return items

    @staticmethod
    def _infer_series_repeat(members):
        """从批次成员的日期间隔推断重复规则（旧数据兼容，推断不出返回空串）。"""
        try:
            ds = sorted(datetime_module.date.fromisoformat(m.get("date", ""))
                        for m in members if m.get("date"))
        except ValueError:
            return ""
        if len(ds) < 2:
            return ""
        deltas = [(ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)]
        if all(d == 1 for d in deltas):
            return "daily"
        if all(d == 7 for d in deltas):
            return "weekly"
        if all(a.day == b.day for a, b in zip(ds, ds[1:])):
            months = [d.year * 12 + d.month for d in ds]
            md = [months[i + 1] - months[i] for i in range(len(months) - 1)]
            if all(m == 1 for m in md):
                return "monthly"
            if all(m == 12 for m in md):
                return "yearly"
        return ""

    def _migrate_series_repeat(self):
        """旧数据补写：为缺 series_repeat 的重复系列实体推断原规则，
        使单条编辑时能回显（否则只能显示"不重复"）。"""
        batches = {}
        for e in self._events:
            bid = e.get("batch_id")
            if bid:
                batches.setdefault(bid, []).append(e)
        for _bid, members in batches.items():
            if any(m.get("series_repeat") for m in members):
                continue
            rep = self._infer_series_repeat(members)
            if rep:
                for m in members:
                    m["series_repeat"] = rep

    # ---------- 渲染 ----------

    def _refresh_view(self):
        if self._rebuilding:
            return
        self._rebuilding = True
        try:
            dates = self._visible_dates()
            all_events = [ev for d in dates for ev in self._events_for_date(d)]

            # 时间轴动态范围（翻页后自动恢复默认 6-21）
            axis_start, axis_end = self._compute_axis(all_events)
            self._grid_area.set_axis(axis_start, axis_end)
            self._grid_area.set_day_cols(len(dates))
            self._day_header.set_dates(dates)
            # 周视图：表头左侧显示「第 N 周」（锚点周起的周数，早于锚点或未设置则不显示）
            if self._view_mode == "week" and len(dates) > 1:
                self._day_header.set_week_no(self._week_number(dates[0]))
            else:
                self._day_header.set_week_no(None)
            # 当前视图包含今天 → 显示"现在"时间指示线（日视图=今天，周视图=本周）
            _today = datetime_module.date.today()
            self._grid_area.show_now_line = (
                (self._view_mode == "day" and dates and dates[0] == _today)
                or (self._view_mode == "week" and _today in dates)
            )
            # 首次构建后同步表头宽度（viewport 宽度此时已确定）
            self._sync_day_header_width()

            # 顶部范围标签 + 视图切换按钮状态
            self._update_range_label(dates)
            self._btn_day.setChecked(self._view_mode == "day")
            self._btn_week.setChecked(self._view_mode == "week")

            # 重建事件块
            self._grid_area.clear_blocks()
            for ci, d in enumerate(dates):
                day_events = self._events_for_date(d)
                if not day_events:
                    continue
                lane_of = self._assign_lanes(day_events)
                lane_count = max(len(set(lane_of.values())), 1)
                for ev in day_events:
                    block = ScheduleBlock(ev, self._view_mode == "day", date=d)
                    block._window = self
                    block.clicked.connect(lambda e=ev, dt=d: self._toggle_done(e, dt))
                    self._grid_area.add_block(block, ci, lane_of[ev["id"]], lane_count)
        finally:
            self._rebuilding = False

    def _update_range_label(self, dates):
        if self._view_mode == "day":
            d = dates[0]
            today = datetime_module.date.today()
            marker = "今天" if d == today else f"{d.month}月{d.day}日"
            self._range_lbl.setText(f"{marker} 周{self.WEEKDAYS[d.weekday()]}")
        else:
            first, last = dates[0], dates[-1]
            self._range_lbl.setText(f"{first.month}月{first.day}日 - {last.month}月{last.day}日")

    # ---------- 交互 ----------

    def _prev_step(self):
        step = 1 if self._view_mode == "day" else 7
        self._center_date -= datetime_module.timedelta(days=step)
        self._refresh_view()

    def _next_step(self):
        step = 1 if self._view_mode == "day" else 7
        self._center_date += datetime_module.timedelta(days=step)
        self._refresh_view()

    def _go_today(self):
        self._center_date = datetime_module.date.today()
        self._refresh_view()

    def _switch_view(self, mode):
        if mode == self._view_mode:
            return
        self._view_mode = mode
        self._refresh_view()

    def _toggle_done(self, ev, date):
        """左键单击事件块：切换完成状态（置灰，无删除线），对齐新番"看过"交互。

        锁定态下保留标记能力（对齐新番便签：条目标记不受锁定影响）。
        重复事件按日期独立记录完成状态（done_dates），单次事件用 done 布尔。
        """
        date_str = date.strftime("%Y-%m-%d")
        if ev.get("repeat") and ev.get("repeat") != "none":
            done_dates = ev.setdefault("done_dates", {})
            done_dates[date_str] = not done_dates.get(date_str, False)
        else:
            ev["done"] = not ev.get("done", False)
        self._refresh_view()
        self._mark_dirty()
        self.save_data()

    def _show_block_menu(self, ev, global_pos):
        """事件右键菜单：编辑 / 标记完成 / 删除（样式对齐便签右键菜单）。

        锁定态下隐藏编辑与删除入口（标记切换保留，与左键一致）。
        """
        menu = QMenu(self)
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setStyleSheet(
            "QMenu { background-color: #FAFAFA; border: 1px solid #E0E0E0;"
            " border-radius: 10px; padding: 6px; }"
            " QMenu::item { padding: 7px 24px; border-radius: 6px; margin: 1px 3px;"
            " color: #333333; font-size: 13px; }"
            " QMenu::item:selected { background-color: #E8F0FE; color: #1A73E8; }"
            " QMenu::separator { height: 1px; background: #E8E8E8; margin: 4px 12px; }"
        )
        # 找到对应日期的 block（右键事件的完成状态按日期判断）
        date = None
        for blk, *_ in self._grid_area._blocks:
            if blk.event_data is ev:
                date = blk.date
                break
        done_now = False
        if date is not None:
            if ev.get("repeat") and ev.get("repeat") != "none":
                done_now = bool((ev.get("done_dates") or {}).get(
                    date.strftime("%Y-%m-%d"), False))
            else:
                done_now = bool(ev.get("done", False))
        act_toggle = menu.addAction("取消完成" if done_now else "标记完成")
        act_toggle.triggered.connect(lambda: self._toggle_done(ev, date or self._center_date))
        if not self.is_locked:
            menu.addSeparator()
            act_edit = menu.addAction("编辑事件…")
            act_edit.triggered.connect(lambda: self._show_event_dialog(ev))
            # 物化实体（有 batch_id）→ 提供"编辑整个系列 / 删除整个系列"入口
            batch_id = ev.get("batch_id")
            if batch_id:
                n = sum(1 for e in self._events if e.get("batch_id") == batch_id)
                act_edit_series = menu.addAction(f"编辑整个系列（{n} 条）…")
                # ⚠️ triggered 传 bool checked，必须显式接收，否则默认参数被覆盖
                act_edit_series.triggered.connect(
                    lambda checked=False, e=ev: self._show_event_dialog(e, series=True))
                act_del_batch = menu.addAction(f"删除整个重复系列（{n} 条）")
                # ⚠️ triggered 信号会传 bool checked 参数，默认值 b=batch_id 会被覆盖！
                # 必须显式接收 checked 并默认 False，batch_id 用默认参数捕获。
                act_del_batch.triggered.connect(
                    lambda checked=False, b=batch_id: self._delete_event_batch(b))
            act_del = menu.addAction("删除事件")
            act_del.triggered.connect(lambda: self._delete_event(ev))
        menu.exec(global_pos)

    def _delete_event(self, ev):
        if self.is_locked:
            return
        self._events = [e for e in self._events if e.get("id") != ev.get("id")]
        self._refresh_view()
        self._mark_dirty()
        self.save_data()

    def _delete_event_batch(self, batch_id):
        """删除整个重复系列（batch_id 相同的所有实体）。"""
        if self.is_locked:
            return
        self._events = [e for e in self._events if e.get("batch_id") != batch_id]
        self._refresh_view()
        self._mark_dirty()
        self.save_data()

    # ---------- 新建 / 编辑事件弹窗（对齐事务追踪器新建事务风格）----------

    def _show_event_dialog(self, ev=None, series=False):
        """新建（ev=None）/ 编辑单条 / 编辑整个重复系列（series=True）事件弹窗。

        - 单条编辑系列成员：重复下拉禁用并回显系列规则（保留最近状态，不必重设），
          边界行隐藏（规则归系列管理），仅标题/日期/时间/提醒/颜色/备注可改。
        - 系列编辑：以批次全量为基准预填（起始日期取最早一条），保存后重建整个系列，
          已完成标记按日期保留。
        """
        if self.is_locked:
            return
        editing = ev is not None
        # 系列成员的单条编辑（下拉只读，避免误改规则导致整批重建）
        is_series_member = bool(editing and not series and ev.get("batch_id"))
        # 系列编辑：批次成员集合（用于取最早起始日与保留完成状态）
        series_members = []
        if series and ev:
            _bid = ev.get("batch_id")
            series_members = [e for e in self._events if e.get("batch_id") == _bid]
        # 回显用的重复规则：系列成员记在 series_repeat 上（实体自身 repeat 恒为 none）
        base_rep = "none"
        if editing:
            base_rep = ev.get("series_repeat") or ev.get("repeat", "none")

        dialog = QDialog(self)
        dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        dialog.setAttribute(Qt.WA_TranslucentBackground)
        # 高度留足：备注栏与底部按钮之间避免重叠（含重复选择 + 提醒两行控件）
        dialog.setFixedSize(440, 700)
        dialog.setStyleSheet(
            "QFrame#sched_dlg_bg { background: #FAFAFA; border-radius: 12px;"
            " border: 1px solid #EAEAEA; }"
        )

        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(12, 12, 12, 12)
        dlg_bg = QFrame()
        dlg_bg.setObjectName("sched_dlg_bg")
        shadow = QGraphicsDropShadowEffect(dialog)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 40))
        shadow.setOffset(0, 6)
        dlg_bg.setGraphicsEffect(shadow)
        outer.addWidget(dlg_bg)

        layout = QVBoxLayout(dlg_bg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 自定义标题栏（可拖拽）
        dlg_bar = QFrame()
        dlg_bar.setStyleSheet("background: transparent;")
        dlg_bar.setFixedHeight(45)
        bar_layout = QHBoxLayout(dlg_bar)
        bar_layout.setContentsMargins(20, 0, 10, 0)
        dlg_title = QLabel("编辑整个系列" if series else ("编辑事件" if editing else "新建事件"))
        # 字重走 QFont 真实字面（QSS 的 font-weight 会触发 Qt 合成且字体族不生效）
        dlg_title.setStyleSheet("color: #333;")
        dlg_title.setFont(fonts_mod.make_font(px=15, weight="bold"))
        bar_layout.addWidget(dlg_title)
        bar_layout.addStretch()
        dlg_close = QPushButton(icon("close"))
        set_icon_font(dlg_close, 16)
        dlg_close.setFixedSize(36, 30)
        dlg_close.setStyleSheet(
            "QPushButton { border: none; border-radius: 6px; background-color: transparent;"
            " font-size: 14px; color: #555; }"
            " QPushButton:hover { background-color: #E81123; color: white; }"
        )
        dlg_close.clicked.connect(dialog.reject)
        bar_layout.addWidget(dlg_close)
        layout.addWidget(dlg_bar)

        dlg_bar._drag_pos = None
        def _bar_press(e):
            if e.button() == Qt.LeftButton:
                dlg_bar._drag_pos = e.globalPosition().toPoint() - dialog.pos()
                e.accept()
        def _bar_move(e):
            if dlg_bar._drag_pos is not None:
                dialog.move(e.globalPosition().toPoint() - dlg_bar._drag_pos)
                e.accept()
        def _bar_release(e):
            dlg_bar._drag_pos = None
        dlg_bar.mousePressEvent = _bar_press
        dlg_bar.mouseMoveEvent = _bar_move
        dlg_bar.mouseReleaseEvent = _bar_release

        content = QVBoxLayout()
        content.setContentsMargins(24, 6, 24, 18)
        content.setSpacing(12)
        layout.addLayout(content, 1)

        lbl_style = "font-size: 13px; color: #555; font-weight: 600;"
        input_style = (
            "QLineEdit, QDateEdit, QTimeEdit { padding: 8px 12px;"
            " border: 1px solid #D0D0D0; border-radius: 8px; font-size: 14px;"
            " background: #FFFFFF; }"
            " QLineEdit:focus, QDateEdit:focus, QTimeEdit:focus { border-color: #1A73E8; }"
        )

        # 标题
        title_lbl = QLabel("事件标题")
        title_lbl.setStyleSheet(lbl_style)
        title_input = QLineEdit()
        title_input.setPlaceholderText("例如：团队晨会")
        title_input.setStyleSheet(input_style)
        if editing:
            title_input.setText(ev.get("title", ""))
        content.addWidget(title_lbl)
        content.addWidget(title_input)

        # 日期 + 重复（放在同一行，重复在日期栏旁边）
        date_lbl = QLabel("起始日期" if series else "日期")
        date_lbl.setStyleSheet(lbl_style)
        date_edit = QDateEdit()
        date_edit.setCalendarPopup(True)
        # 去掉上下步进按钮（防误改年份），保留日历下拉箭头
        date_edit.setButtonSymbols(QAbstractSpinBox.NoButtons)
        date_edit.setDisplayFormat("yyyy-MM-dd")
        date_edit.setStyleSheet(input_style)
        if series and series_members:
            # 系列编辑：起始日期取批次最早一条（保持原起点，可改）
            try:
                first_date = min(m.get("date", "") for m in series_members)
                date_edit.setDate(QDate.fromString(first_date, "yyyy-MM-dd"))
            except Exception:
                date_edit.setDate(QDate.currentDate())
        elif editing:
            try:
                date_edit.setDate(QDate.fromString(ev.get("date", ""), "yyyy-MM-dd"))
            except Exception:
                date_edit.setDate(QDate.currentDate())
        else:
            # 新建事件：日期默认当天（不跟随视图所在日期）
            date_edit.setDate(QDate.currentDate())
        repeat_edit = QComboBox()
        repeat_edit.addItems(["不重复", "每天重复", "每周重复", "每月重复", "每年重复"])
        repeat_edit.setStyleSheet(
            "QComboBox { padding: 8px 10px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " font-size: 13px; background: #FFFFFF; }"
            " QComboBox:focus { border-color: #1A73E8; }"
            " QComboBox::drop-down { border: none; width: 22px; }"
        )
        if editing:
            # 用 base_rep 回显：系列成员显示其所属系列的重复规则（保留最近状态）
            idx = {"none": 0, "daily": 1, "weekly": 2, "monthly": 3, "yearly": 4}.get(base_rep, 0)
            repeat_edit.setCurrentIndex(idx)
            if is_series_member:
                # 单条编辑：重复规则由系列决定，禁止在此改动（避免误触整批重建）
                repeat_edit.setEnabled(False)
                repeat_edit.setToolTip("该事件属于重复系列，重复规则请使用「编辑整个系列」修改")
        date_row = QHBoxLayout()
        date_row.setSpacing(8)
        date_row.addWidget(date_edit, 1)
        date_row.addWidget(repeat_edit, 1)
        content.addWidget(date_lbl)
        content.addLayout(date_row)
        if is_series_member:
            series_hint = QLabel("属于重复系列：如需修改重复规则或周期，请右键选择「编辑整个系列」")
            series_hint.setStyleSheet("font-size: 11px; color: #999; background: transparent;")
            series_hint.setWordWrap(True)
            content.addWidget(series_hint)

        # 时间：时 + 分 两个下拉（直接点选，没有步进箭头可误触）
        time_lbl = QLabel("时间")
        time_lbl.setStyleSheet(lbl_style)
        time_row = QHBoxLayout()
        time_row.setSpacing(4)
        time_combo_style = (
            "QComboBox { padding: 6px 8px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " font-size: 13px; background: #FFFFFF; }"
            " QComboBox:focus { border-color: #1A73E8; }"
            " QComboBox::drop-down { border: none; width: 18px; }"
        )
        start_h, start_m = _make_time_selectors(time_combo_style)
        end_h, end_m = _make_time_selectors(time_combo_style)
        _set_time_selectors(start_h, start_m, ev.get("start", "09:00") if editing else "09:00")
        _set_time_selectors(end_h, end_m, ev.get("end", "10:00") if editing else "10:00")
        colon1 = QLabel(":")
        colon2 = QLabel(":")
        for c in (colon1, colon2):
            c.setStyleSheet("font-size: 13px; color: #888; background: transparent;")
        dash = QLabel("至")
        dash.setStyleSheet("font-size: 13px; color: #888; background: transparent;")
        time_row.addWidget(start_h)
        time_row.addWidget(colon1)
        time_row.addWidget(start_m)
        time_row.addWidget(dash)
        time_row.addWidget(end_h)
        time_row.addWidget(colon2)
        time_row.addWidget(end_m)
        time_row.addStretch()
        content.addWidget(time_lbl)
        content.addLayout(time_row)

        # ---- 重复边界（按重复类型动态显示）----
        # 每天：终止日期
        bound_daily = QWidget()
        bd_row = QHBoxLayout(bound_daily)
        bd_row.setContentsMargins(0, 0, 0, 0)
        bd_row.setSpacing(8)
        bd_label = QLabel("终止日期")
        bd_label.setStyleSheet(lbl_style)
        bd_infinite = QCheckBox("无限")
        bd_infinite.setStyleSheet("font-size: 13px; color: #333;")
        bd_infinite.setChecked(True)
        bd_date = QDateEdit()
        bd_date.setCalendarPopup(True)
        bd_date.setButtonSymbols(QAbstractSpinBox.NoButtons)
        bd_date.setDisplayFormat("yyyy-MM-dd")
        bd_date.setStyleSheet(input_style)
        bd_date.setDate(date_edit.date())
        bd_date.setEnabled(False)
        # 勾选"无限"→ 日期禁用；取消 → 可选终止日期
        bd_infinite.toggled.connect(lambda checked: bd_date.setEnabled(not checked))
        if editing and ev.get("repeat_until"):
            try:
                bd_date.setDate(QDate.fromString(ev["repeat_until"], "yyyy-MM-dd"))
                bd_infinite.setChecked(False)
            except Exception:
                pass
        bd_row.addWidget(bd_label)
        bd_row.addWidget(bd_infinite)
        bd_row.addWidget(bd_date, 1)
        content.addWidget(bound_daily)

        # 每月：终止月
        bound_month = QWidget()
        bm_row = QHBoxLayout(bound_month)
        bm_row.setContentsMargins(0, 0, 0, 0)
        bm_row.setSpacing(8)
        bm_label = QLabel("终止月")
        bm_label.setStyleSheet(lbl_style)
        bm_infinite = QCheckBox("无限")
        bm_infinite.setStyleSheet("font-size: 13px; color: #333;")
        bm_infinite.setChecked(True)
        bm_date = QDateEdit()
        bm_date.setCalendarPopup(True)
        bm_date.setButtonSymbols(QAbstractSpinBox.NoButtons)
        bm_date.setDisplayFormat("yyyy-MM")
        bm_date.setStyleSheet(input_style)
        bm_date.setDate(date_edit.date())
        bm_date.setEnabled(False)
        bm_infinite.toggled.connect(lambda checked: bm_date.setEnabled(not checked))
        if editing and base_rep == "monthly" and ev.get("repeat_until"):
            try:
                u = datetime_module.date.fromisoformat(ev["repeat_until"])
                bm_date.setDate(QDate(u.year, u.month, 1))
                bm_infinite.setChecked(False)
            except Exception:
                pass
        bm_row.addWidget(bm_label)
        bm_row.addWidget(bm_infinite)
        bm_row.addWidget(bm_date, 1)
        content.addWidget(bound_month)

        # 每周：有锚点周 → 起始/结束周下拉（第 N 周 + 日期范围）；无锚点 → 直接结束日期
        bound_week = QWidget()
        bw_row = QHBoxLayout(bound_week)
        bw_row.setContentsMargins(0, 0, 0, 0)
        bw_row.setSpacing(8)
        combo_style = (
            "QComboBox { padding: 6px 8px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " font-size: 12px; background: #FFFFFF; }"
            " QComboBox:focus { border-color: #1A73E8; }"
            " QComboBox::drop-down { border: none; width: 22px; }"
        )

        # 模式 A：有锚点 → 第 N 周下拉
        week_anchor_box = QWidget()
        wa_row = QHBoxLayout(week_anchor_box)
        wa_row.setContentsMargins(0, 0, 0, 0)
        wa_row.setSpacing(8)
        ws_label = QLabel("起始周")
        ws_label.setStyleSheet(lbl_style)
        ws_combo = QComboBox()
        ws_combo.setStyleSheet(combo_style)
        we_label = QLabel("结束周")
        we_label.setStyleSheet(lbl_style)
        we_combo = QComboBox()
        we_combo.setStyleSheet(combo_style)

        def _fill_week_combo(cb, cur_val):
            cb.clear()
            cb.addItem("不限", 0)
            aw = self._anchor_week
            for n in range(1, 54):
                start = aw + datetime_module.timedelta(days=(n - 1) * 7)
                end = start + datetime_module.timedelta(days=6)
                cb.addItem(f"第{n}周 ({start.month}/{start.day}–{end.month}/{end.day})", n)
            if cur_val:
                idx = cb.findData(int(cur_val))
                if idx >= 0:
                    cb.setCurrentIndex(idx)

        # 模式 B：无锚点 → 结束日期（起始 = 事件日期）
        week_date_box = QWidget()
        wd_row = QHBoxLayout(week_date_box)
        wd_row.setContentsMargins(0, 0, 0, 0)
        wd_row.setSpacing(8)
        wu_label = QLabel("结束日期")
        wu_label.setStyleSheet(lbl_style)
        wu_infinite = QCheckBox("无限")
        wu_infinite.setStyleSheet("font-size: 13px; color: #333;")
        wu_infinite.setChecked(True)
        wu_date = QDateEdit()
        wu_date.setCalendarPopup(True)
        wu_date.setButtonSymbols(QAbstractSpinBox.NoButtons)
        wu_date.setDisplayFormat("yyyy-MM-dd")
        wu_date.setStyleSheet(input_style)
        wu_date.setDate(date_edit.date())
        wu_date.setEnabled(False)
        wu_infinite.toggled.connect(lambda checked: wu_date.setEnabled(not checked))
        if editing and ev.get("repeat_until"):
            try:
                wu_date.setDate(QDate.fromString(ev["repeat_until"], "yyyy-MM-dd"))
                wu_infinite.setChecked(False)
            except Exception:
                pass
        wd_row.addWidget(wu_label)
        wd_row.addWidget(wu_infinite)
        wd_row.addWidget(wu_date, 1)

        if self._anchor_week:
            _fill_week_combo(ws_combo, ev.get("week_start", 0) if editing else 0)
            _fill_week_combo(we_combo, ev.get("week_end", 0) if editing else 0)
            wa_row.addWidget(ws_label)
            wa_row.addWidget(ws_combo, 1)
            wa_row.addWidget(we_label)
            wa_row.addWidget(we_combo, 1)
            bw_row.addWidget(week_anchor_box)
        else:
            bw_row.addWidget(week_date_box)
        content.addWidget(bound_week)

        # 每年：起始年 / 终止年
        bound_year = QWidget()
        by_row = QHBoxLayout(bound_year)
        by_row.setContentsMargins(0, 0, 0, 0)
        by_row.setSpacing(8)
        ys_label = QLabel("起始年")
        ys_label.setStyleSheet(lbl_style)
        ys_combo = QComboBox()
        ys_combo.setStyleSheet(ws_combo.styleSheet())
        ye_label = QLabel("终止年")
        ye_label.setStyleSheet(lbl_style)
        ye_combo = QComboBox()
        ye_combo.setStyleSheet(ws_combo.styleSheet())
        base_year = date_edit.date().year()

        def _fill_year_combo(cb, cur_val):
            cb.clear()
            cb.addItem("不限", 0)
            for y in range(base_year - 5, base_year + 12):
                cb.addItem(str(y), y)
            if cur_val:
                idx = cb.findData(int(cur_val))
                if idx >= 0:
                    cb.setCurrentIndex(idx)

        _fill_year_combo(ys_combo, ev.get("year_start", 0) if editing else 0)
        _fill_year_combo(ye_combo, ev.get("year_end", 0) if editing else 0)
        by_row.addWidget(ys_label)
        by_row.addWidget(ys_combo, 1)
        by_row.addWidget(ye_label)
        by_row.addWidget(ye_combo, 1)
        content.addWidget(bound_year)

        # 按重复类型切换边界行显示
        def update_bounds(idx):
            if is_series_member:
                # 系列成员单条编辑：边界归系列管理，全部隐藏
                for _w in (bound_daily, bound_month, bound_week, bound_year):
                    _w.setVisible(False)
                return
            bound_daily.setVisible(idx == 1)      # 每天
            bound_month.setVisible(idx == 3)      # 每月
            bound_week.setVisible(idx == 2)       # 每周
            bound_year.setVisible(idx == 4)       # 每年

        repeat_edit.currentIndexChanged.connect(update_bounds)
        update_bounds(repeat_edit.currentIndex())

        # 提醒：提前 X 分钟/小时/天，触发系统通知
        remind_lbl = QLabel("提醒")
        remind_lbl.setStyleSheet(lbl_style)
        remind_row = QHBoxLayout()
        remind_row.setSpacing(8)
        remind_enable = QCheckBox("提前")
        remind_enable.setStyleSheet("font-size: 13px; color: #333;")
        remind_value = QSpinBox()
        remind_value.setRange(1, 999)
        remind_value.setValue(10)
        # 保留上下步进箭头（用户要求恢复：点箭头即可调数值）
        remind_value.setStyleSheet(
            "QSpinBox { padding: 6px 8px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " background: #FFFFFF; font-size: 13px; }"
            " QSpinBox:focus { border-color: #1A73E8; }"
        )
        remind_unit = QComboBox()
        remind_unit.addItems(["分钟", "小时", "天"])
        remind_unit.setStyleSheet(
            "QComboBox { padding: 6px 8px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " font-size: 13px; background: #FFFFFF; }"
            " QComboBox:focus { border-color: #1A73E8; }"
            " QComboBox::drop-down { border: none; width: 22px; }"
        )
        # 恢复已有提醒设置
        if editing:
            rm = ev.get("remind")
            if rm:
                remind_enable.setChecked(True)
                remind_value.setValue(int(rm.get("value", 10)))
                unit_idx = {"minute": 0, "hour": 1, "day": 2}.get(rm.get("unit", "minute"), 0)
                remind_unit.setCurrentIndex(unit_idx)
        else:
            remind_enable.setChecked(False)
        remind_hint = QLabel("系统通知")
        remind_hint.setStyleSheet("font-size: 11px; color: #999; background: transparent;")
        remind_row.addWidget(remind_enable)
        remind_row.addWidget(remind_value)
        remind_row.addWidget(remind_unit)
        remind_row.addWidget(remind_hint)
        remind_row.addStretch()
        content.addWidget(remind_lbl)
        content.addLayout(remind_row)

        # 颜色
        color_lbl = QLabel("标记颜色")
        color_lbl.setStyleSheet(lbl_style)
        preset_colors = ["#E81123", "#FF8C00", "#107C10", "#0078D7", "#881798", "#333333"]
        color_btns = []
        selected_color = [ev.get("color", preset_colors[3]) if editing else preset_colors[3]]

        def on_color_click(c):
            selected_color[0] = c
            for b, oc in zip(color_btns, preset_colors):
                if oc == c:
                    b.setStyleSheet(
                        f"QPushButton {{ background-color: {oc}; border-radius: 14px;"
                        f" border: 3px solid #FFFFFF; outline: 2px solid {oc}; }}"
                    )
                else:
                    b.setStyleSheet(
                        f"QPushButton {{ background-color: {oc}; border-radius: 14px;"
                        f" border: 2px solid rgba(0,0,0,0.08); }}"
                        " QPushButton:hover { border: 2px solid rgba(0,0,0,0.25); }"
                    )

        color_layout = QHBoxLayout()
        color_layout.setSpacing(10)
        for c in preset_colors:
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            btn.setCursor(Qt.PointingHandCursor)
            if c == selected_color[0]:
                btn.setStyleSheet(
                    f"QPushButton {{ background-color: {c}; border-radius: 14px;"
                    f" border: 3px solid #FFFFFF; outline: 2px solid {c}; }}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ background-color: {c}; border-radius: 14px;"
                    f" border: 2px solid rgba(0,0,0,0.08); }}"
                    " QPushButton:hover { border: 2px solid rgba(0,0,0,0.25); }"
                )
            btn.clicked.connect(lambda checked, clr=c: on_color_click(clr))
            color_layout.addWidget(btn)
            color_btns.append(btn)
        color_layout.addStretch()
        content.addWidget(color_lbl)
        content.addLayout(color_layout)

        # 备注
        note_lbl = QLabel("备注（日视图展开显示）")
        note_lbl.setStyleSheet(lbl_style)
        note_input = QTextEdit()
        note_input.setPlaceholderText("可留空")
        note_input.setFixedHeight(110)
        note_input.setStyleSheet(
            "QTextEdit { padding: 8px 12px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " font-size: 14px; background: #FFFFFF; }"
            " QTextEdit:focus { border-color: #1A73E8; }"
        )
        if editing:
            note_input.setPlainText(ev.get("note", ""))
        content.addWidget(note_lbl)
        content.addWidget(note_input)

        # 弹性空间：把按钮行推到底部，避免与备注栏重叠
        content.addStretch(1)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet(
            "QPushButton { padding: 8px 22px; border: 1px solid #D0D0D0; border-radius: 8px;"
            " background: #FFFFFF; font-size: 13px; color: #555; }"
            " QPushButton:hover { background: #F0F0F0; border-color: #B0B0B0; }"
        )
        cancel_btn.clicked.connect(dialog.reject)
        ok_btn = QPushButton("保存整个系列" if series else ("保存" if editing else "添加"))
        ok_btn.setStyleSheet(
            "QPushButton { padding: 8px 26px; border: none; border-radius: 8px;"
            " background: #1A73E8; font-size: 13px; color: #FFFFFF; font-weight: 600; }"
            " QPushButton:hover { background: #1765CC; }"
            " QPushButton:pressed { background: #1557B0; }"
        )
        ok_btn.clicked.connect(dialog.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        content.addLayout(btn_layout)

        if dialog.exec() == QDialog.Accepted:
            title = title_input.text().strip()
            if not title:
                return
            s_time = _read_time_selectors(start_h, start_m, "09:00")
            e_time = _read_time_selectors(end_h, end_m, "10:00")
            if _time_to_min(e_time) <= _time_to_min(s_time):
                QMessageBox.warning(self, "时间无效", "结束时间必须晚于开始时间。")
                return
            date_str = date_edit.date().toString("yyyy-MM-dd")
            rep = {0: "none", 1: "daily", 2: "weekly", 3: "monthly", 4: "yearly"}[
                repeat_edit.currentIndex()]
            # 重复边界（按类型读取对应控件）
            repeat_until = ""
            week_start = 0
            week_end = 0
            year_start = 0
            year_end = 0
            if rep == "daily" and not bd_infinite.isChecked():
                repeat_until = bd_date.date().toString("yyyy-MM-dd")
            if rep == "monthly" and not bm_infinite.isChecked():
                import calendar
                qm = bm_date.date()
                _last = calendar.monthrange(qm.year(), qm.month())[1]
                repeat_until = "%04d-%02d-%02d" % (qm.year(), qm.month(), _last)
            if rep == "weekly":
                if self._anchor_week:
                    week_start = int(ws_combo.currentData() or 0)
                    week_end = int(we_combo.currentData() or 0)
                else:
                    # 无锚点：直接结束日期（起始 = 事件日期），存 repeat_until
                    week_start = 0
                    week_end = 0
                    if not wu_infinite.isChecked():
                        repeat_until = wu_date.date().toString("yyyy-MM-dd")
            if rep == "yearly":
                year_start = int(ys_combo.currentData() or 0)
                year_end = int(ye_combo.currentData() or 0)
            # 提醒设置（未启用则为 None）
            remind = None
            if remind_enable.isChecked():
                unit = {0: "minute", 1: "hour", 2: "day"}[remind_unit.currentIndex()]
                remind = {"value": remind_value.value(), "unit": unit}
            if series:
                # 编辑整个系列：按原批次重建，已完成标记按日期保留
                _bid = ev.get("batch_id")
                done_map = {}
                for m in series_members:
                    _dk = m.get("date", "")
                    if _dk:
                        done_map[_dk] = bool(m.get("done", False))
                self._events = [e for e in self._events if e.get("batch_id") != _bid]
                template = {
                    "id": uuid.uuid4().hex[:8],
                    "title": title,
                    "date": date_str,
                    "start": s_time,
                    "end": e_time,
                    "note": note_input.toPlainText().strip(),
                    "color": selected_color[0],
                    "repeat": rep,
                    "remind": remind,
                    "repeat_until": repeat_until,
                    "week_start": week_start,
                    "week_end": week_end,
                    "year_start": year_start,
                    "year_end": year_end,
                    "done": False,
                    "done_dates": done_map,   # 物化时按日期映射回完成状态
                }
                if rep != "none":
                    self._events.extend(self._materialize_repeat(template, _bid))
                else:
                    # 系列改为不重复：仅保留起始日那一条为单次事件
                    single = dict(template)
                    single.pop("done_dates", None)
                    single["done"] = bool(done_map.get(date_str, False))
                    self._events.append(single)
            elif editing:
                ev["title"] = title
                ev["date"] = date_str
                ev["start"] = s_time
                ev["end"] = e_time
                ev["note"] = note_input.toPlainText().strip()
                ev["color"] = selected_color[0]
                ev["remind"] = remind
                if is_series_member:
                    # 系列成员单条编辑：只改这一条；重复规则与周期由系列管理，
                    # 保持 repeat=none / series_repeat / batch_id 不变
                    ev["repeat"] = "none"
                else:
                    ev["repeat"] = rep
                    ev["repeat_until"] = repeat_until
                    ev["week_start"] = week_start
                    ev["week_end"] = week_end
                    ev["year_start"] = year_start
                    ev["year_end"] = year_end
                    if rep != "none":
                        # 单次事件改为重复：以本条为起点物化新系列
                        bid = ev.get("batch_id") or ev["id"]
                        self._events = [e for e in self._events
                                        if e.get("batch_id") != bid
                                        and e.get("id") != ev["id"]]
                        template = dict(ev)
                        self._events.extend(self._materialize_repeat(template, bid))
                    else:
                        ev.pop("batch_id", None)   # 转回单次
                        ev.pop("done_dates", None)
                        ev.pop("series_repeat", None)
            else:
                new_ev = {
                    "id": uuid.uuid4().hex[:8],
                    "title": title,
                    "date": date_str,
                    "start": s_time,
                    "end": e_time,
                    "note": note_input.toPlainText().strip(),
                    "color": selected_color[0],
                    "repeat": rep,
                    "remind": remind,
                    "repeat_until": repeat_until,
                    "week_start": week_start,
                    "week_end": week_end,
                    "year_start": year_start,
                    "year_end": year_end,
                    "done": False,
                }
                if rep != "none":
                    # 重复事件：自动物化为未来各期实体条目（模板不保留）
                    batch_id = new_ev["id"]
                    self._events.extend(self._materialize_repeat(new_ev, batch_id))
                else:
                    self._events.append(new_ev)
            self._refresh_view()
            self._mark_dirty()
            self.save_data()

    # ---------- 持久化 ----------

    def _load_schedule(self):
        """从便签 JSON 的 schedule_data 字段恢复数据。"""
        if self.save_file and os.path.exists(self.save_file):
            try:
                with open(self.save_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sd = data.get("schedule_data", {})
                self._view_mode = sd.get("view_mode", "week")
                self._center_date = datetime_module.date.today()
                cd = sd.get("center_date", "")
                if cd:
                    try:
                        self._center_date = datetime_module.date.fromisoformat(cd)
                    except ValueError:
                        pass
                self._events = sd.get("events", [])
                self._migrate_series_repeat()   # 旧数据：补写系列重复规则
                # 起始周锚点（周一日期；空 = 未设置）
                aw = sd.get("anchor_week", "")
                if aw:
                    try:
                        self._anchor_week = datetime_module.date.fromisoformat(aw)
                    except ValueError:
                        self._anchor_week = None
                else:
                    self._anchor_week = None
            except Exception:
                pass
        self._refresh_view()

    def save_data(self):
        """保存时附加 schedule_data。"""
        if getattr(self, '_is_loading', False):
            return
        if self.width() < 250:
            return
        super().save_data()
        if self.save_file and os.path.exists(self.save_file):
            try:
                with open(self.save_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["schedule_data"] = {
                    "view_mode": self._view_mode,
                    "center_date": self._center_date.strftime("%Y-%m-%d"),
                    "anchor_week": (self._anchor_week.strftime("%Y-%m-%d")
                                    if self._anchor_week else ""),
                    "events": self._events,
                }
                with open(self.save_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
            except Exception:
                pass

    # ---------- 系统提醒 ----------

    def _next_occurrence(self, ev, today):
        """计算事件的下一次发生日期（考虑重复规则与边界，>= today）。

        返回 datetime.date 或 None（单次事件已过期 / 超出重复边界）。
        """
        try:
            base = datetime_module.date.fromisoformat(ev.get("date", ""))
        except ValueError:
            return None
        rep = ev.get("repeat", "none")
        if rep == "none":
            return base if base >= today else None
        # 从 base 逐期推进，找 >= today 且仍在重复边界内的下一期
        cur = base
        guard = 0
        while guard < 4000:
            if cur >= today and self._in_repeat_bounds(cur, ev):
                return cur
            if rep == "daily":
                cur += datetime_module.timedelta(days=1)
            elif rep == "weekly":
                cur += datetime_module.timedelta(days=7)
            elif rep == "monthly":
                # 平移到下月同日（若下月无此日则取月末）
                y, m = cur.year, cur.month
                if m == 12:
                    y, m = y + 1, 1
                else:
                    m += 1
                import calendar
                day = min(base.day, calendar.monthrange(y, m)[1])
                cur = datetime_module.date(y, m, day)
            elif rep == "yearly":
                y = cur.year + 1
                import calendar
                day = min(base.day, calendar.monthrange(y, base.month)[1])
                cur = datetime_module.date(y, base.month, day)
            else:
                return None
            guard += 1
            # 边界外推进防失控：超过 today 一年仍未命中（如每周的结束周已过）→ 视为无下次
            if cur > today + datetime_module.timedelta(days=370):
                return None
        return None

    def _check_reminders(self):
        """定时扫描：到达提醒时间点的事件触发系统通知（避免重复提醒）。

        提醒时间 = 事件开始时间 - 提前量；只提醒"未发生"的事件。
        """
        if not self._events:
            return
        now = datetime_module.datetime.now()
        today = now.date()
        fired_keys = set()
        for ev in self._events:
            rm = ev.get("remind")
            if not rm:
                continue
            occur = self._next_occurrence(ev, today)
            if occur is None:
                continue
            try:
                h, m = (ev.get("start", "09:00")).split(":")
                start_dt = datetime_module.datetime(
                    occur.year, occur.month, occur.day, int(h), int(m))
            except (ValueError, AttributeError):
                continue
            # 只提醒未开始的事件
            if now >= start_dt:
                continue
            value = int(rm.get("value", 10))
            unit = rm.get("unit", "minute")
            if unit == "minute":
                remind_dt = start_dt - datetime_module.timedelta(minutes=value)
            elif unit == "hour":
                remind_dt = start_dt - datetime_module.timedelta(hours=value)
            elif unit == "day":
                remind_dt = start_dt - datetime_module.timedelta(days=value)
            else:
                continue
            if remind_dt <= now:
                key = (ev.get("id"), occur.strftime("%Y-%m-%d"))
                if key not in self._remind_fired:
                    self._remind_fired.add(key)
                    unit_cn = {"minute": "分钟", "hour": "小时", "day": "天"}.get(unit, "")
                    global_signaler.schedule_remind_signal.emit(
                        ev.get("title", "日程提醒"),
                        f"{occur.strftime('%m月%d日')} {ev.get('start', '')} · "
                        f"提前{value}{unit_cn}提醒"
                    )
            fired_keys.add((ev.get("id"), occur.strftime("%Y-%m-%d")))
        # 清理已过期（事件已开始/过期）的触发记录，避免内存增长
        stale = [k for k in self._remind_fired if k not in fired_keys]
        for k in stale:
            self._remind_fired.discard(k)


class BangumiScheduleWindow(AniNoteWindow):
    """新番便签窗口：原生网格展示追番日历。

    以「今天 ± 偏移」为中心的滑动窗口视图，左右箭头每次平移一天
    （偏移限制在 ±4 天）；缩放联动决定显示天数（3 / 5 / 7，默认 3）；
    点击番剧名标记看过（变灰）。数据来自 Bangumi 周循环日历。
    """

    WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

    # 后台反向同步线程 → 主线程的错误提示（成功静默，失败才提示）
    sync_failed = Signal(str)

    def __init__(self, note_id=None):
        nid = note_id if note_id else "bangumi_schedule"
        super().__init__(note_id=nid)

        self.setMinimumSize(420, 320)  # 恢复原始最小尺寸
        self.text_edit.hide()
        self.format_panel.hide()

        # 状态
        self._center_offset = 0      # 视图中心相对今天的偏移（±4）
        self._visible_days = 3       # 缩放联动 3/5/7
        self._schedule = {}          # {weekday_idx: [番剧名, ...]}
        self._watched = {}           # {date_str: [番剧名, ...]} 手动标记看过
        self._unwatched = {}         # {date_str: [番剧名, ...]} 手动取消（覆盖自动灰）
        self._status_text = None     # 加载中/错误信息（非 None 时占据首行）
        self._rebulding = False      # 防 resizeEvent 递归重建
        self._show_unwatched_only = False  # "只看未看"过滤开关
        self.sync_failed.connect(self._on_sync_failed)

        # 保留编辑工具栏（用户可自行锁定便签），并添加刷新按钮
        self.refresh_btn = QPushButton(icon("refresh"), self.header)
        set_icon_font(self.refresh_btn, 14)
        self.refresh_btn.setToolTip("立即同步新番日历")
        self.refresh_btn.setFixedSize(22, 22)
        self.refresh_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; font-size: 14px; }"
            " QPushButton:hover { background-color: rgba(0,0,0,0.1); border-radius: 4px; }"
        )
        self.refresh_btn.clicked.connect(lambda: global_signaler.force_sync_bangumi_signal.emit())
        self.header.title_layout.insertWidget(self.header.title_layout.count() - 1, self.refresh_btn)

        # 注意：不再强制 is_locked=False。父类 __init__ 已通过 load_data()
        # 恢复存档的锁定状态——首次创建默认解锁（父类默认 False），
        # 用户手动锁定后重启应保持锁定。网格模式下番剧标记走 ClickableLabel
        # 独立点击事件，锁定与否均不影响标记。

        # 网格（插入到 text_edit 原位置）
        self._build_schedule_grid()

        # 恢复数据
        self._load_bangumi_state()

        # 首次创建 / 旧数据：浅蓝主题（替代父类 _init_bangumi_mode 的外观职责）
        if not (self.save_file and os.path.exists(self.save_file)):
            self.is_always_on_top = False
            self.apply_window_states()   # 属性改了必须同步窗口 flag，否则置顶状态错乱
            self.bg_color = [235, 245, 255, 242]
            self._apply_bg_color()
        self._apply_lock_ui()

        # 首次创建 / 旧数据：设置默认标题与提示
        self._init_bangumi_default_title()
        if not self._schedule and not self._status_text:
            self._status_text = "点击右上角「⟳」按钮同步新番"

        # 初始渲染
        self._refresh_view()

        # 旧版本便签没有 bangumi_schedule_data：无数据时自动补一次同步
        QTimer.singleShot(500, self._sync_if_empty)

        QTimer.singleShot(0, self._show_if_not_hidden)

    def _init_bangumi_mode(self):
        """网格模式不需要父类的 HTML 只读初始化（首创建外观由子类控制）。"""
        pass

    def _apply_lock_ui(self):
        """网格模式：工具栏可见性交给父类逻辑（跟随锁定状态，用户可自行锁定）；
        仅格式面板强制隐藏——网格无文本可编辑，显示它会挤掉下方的番剧表格。
        """
        super()._apply_lock_ui()
        self.format_panel.hide()

    def _init_bangumi_default_title(self):
        """首次创建或存档标题为"未命名便签"时，使用新番便签专用标题。"""
        cur = self.header.title_edit.text().strip()
        if not cur or cur.startswith("未命名便签"):
            self.header.title_edit.setText("新番追番日历")

    # 网格模式无文本编辑区：字体格式操作一律忽略（无 text_edit，调用会崩）。
    # 背景底色 / 透明度是便签级功能，保留父类实现（用户可正常调节便签颜色与透明）。
    def change_font_family(self, font):
        pass

    def change_font_size(self, size):
        pass

    def change_font_color_direct(self, hex_color):
        pass

    def _sync_if_empty(self):
        if not self._schedule:
            global_signaler.force_sync_bangumi_signal.emit()

    def _show_if_not_hidden(self):
        if not getattr(self, 'is_hidden', False):
            # 只显示不抢焦点：避免同步弹窗时新便签盖住模态提示框导致无法点击
            self.animated_show()

    # ---------- 网格构建 ----------

    def _build_schedule_grid(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: transparent; width: 5px; margin: 0; }
            QScrollBar::handle:vertical { background: #D0D0D0; border-radius: 2px; min-height: 20px; }
            QScrollBar::handle:vertical:hover { background: #A0A0A0; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)
        # 滚动条自动隐藏：未滚动时透明，滚动时显示，停 1.2s 后隐藏（对齐 MD 便签）
        setup_auto_hide_scrollbar(
            scroll.verticalScrollBar(), scroll.styleSheet(),
            "QScrollBar:vertical { background: transparent; width: 5px; margin: 0; }"
            " QScrollBar::handle:vertical { background: transparent; border-radius: 2px; min-height: 20px; }"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            " QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }",
        )

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(container)
        self._grid.setContentsMargins(0, 5, 0, 5)
        self._grid.setVerticalSpacing(4)
        self._grid.setHorizontalSpacing(6)

        scroll.setWidget(container)  # 关键：container 成为滚动区内容（之前漏写导致表格不可见）

        frame_layout = self.bg_frame.layout()
        idx = frame_layout.indexOf(self.editor_host)
        if idx < 0:
            idx = frame_layout.indexOf(self.text_edit)
        frame_layout.insertWidget(idx, scroll)
        self.editor_host.hide()
        self._tracker_scroll = scroll
        self._tracker_container = container

    def _clear_grid(self):
        """清空网格全部控件。"""
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    # ---------- 视图计算 ----------

    def _visible_dates(self):
        """返回当前视图显示的日期对象列表（连续 _visible_days 天，今天偏移居中）。"""
        today = datetime_module.date.today()
        center = today + datetime_module.timedelta(days=self._center_offset)
        half = (self._visible_days - 1) // 2
        return [center + datetime_module.timedelta(days=i - half) for i in range(self._visible_days)]

    # ---------- 渲染 ----------

    def _refresh_view(self):
        if self._rebulding:
            return
        self._rebulding = True
        try:
            self._clear_grid()
            days = self._visible_days
            today = datetime_module.date.today()

            # 重置列/行配置：QGridLayout 的 stretch 是累积的，宽→窄后旧列
            # stretch 残留会把宽度分走导致内容拥挤 + 右侧留白，必须先清零
            for c in range(0, 9):
                self._grid.setColumnStretch(c, 0)
                self._grid.setColumnMinimumWidth(c, 0)
            for r in range(0, 40):
                self._grid.setRowStretch(r, 0)

            # 列配置：col0 ◀ / col1..N 日期列 / colN+1 ▶
            self._grid.setColumnMinimumWidth(0, 34)
            self._grid.setColumnStretch(0, 0)
            for c in range(1, days + 1):
                self._grid.setColumnMinimumWidth(c, 60)
                self._grid.setColumnStretch(c, 1)
            self._grid.setColumnMinimumWidth(days + 1, 34)
            self._grid.setColumnStretch(days + 1, 0)

            # ── 状态行（加载中/错误）──
            if self._status_text:
                status = QLabel(self._status_text)
                status.setWordWrap(True)
                status.setAlignment(Qt.AlignCenter)
                status.setStyleSheet(
                    "color: #666; font-size: 13px; padding: 8px;"
                    " background: transparent; border: none;"
                )
                self._grid.addWidget(status, 0, 0, 1, days + 2)
                self._grid.setRowStretch(1, 1)
                return

            # ── 第 0 行：只看未看按钮（左） + 数据来源 Bangumi 链接（居中）──
            row0 = QWidget()
            row0_layout = QHBoxLayout(row0)
            row0_layout.setContentsMargins(4, 0, 4, 0)
            row0_layout.setSpacing(0)
            self._unwatched_btn = QPushButton("只看未看")
            self._unwatched_btn.setCheckable(True)
            self._unwatched_btn.setChecked(self._show_unwatched_only)
            self._unwatched_btn.setCursor(Qt.PointingHandCursor)
            self._unwatched_btn.setStyleSheet(
                "QPushButton { border: none; border-radius: 6px; padding: 2px 8px;"
                " font-size: 11px; color: #888; background: transparent; }"
                "QPushButton:hover { background: rgba(0,0,0,0.06); color: #333; }"
                "QPushButton:checked { background: #E8F4FD; color: #0078D7; font-weight: bold; }"
            )
            self._unwatched_btn.clicked.connect(self._toggle_unwatched_filter)
            row0_layout.addWidget(self._unwatched_btn)

            src = QLabel(
                "<a href='https://bgm.tv/calendar' style='color:#0078D7; "
                "text-decoration:none;'>数据来源：Bangumi</a>"
            )
            src.setOpenExternalLinks(True)
            src.setAlignment(Qt.AlignCenter)
            src.setStyleSheet(
                "font-size: 11px; color: #999; background: transparent;"
                " padding: 0 6px 2px; border: none;"
            )
            row0_layout.addStretch(1)
            row0_layout.addWidget(src, 1)
            row0_layout.addStretch(1)
            self._grid.addWidget(row0, 0, 0, 1, days + 2)

            # ── 表头：◀ / 日期 / ▶（第 1 行，整行淡色底）──
            btn_style = (
                "QPushButton { border: none; background: transparent; font-size: 18px; "
                "color: #888; padding: 4px 8px; }"
                "QPushButton:hover { color: #333; background: rgba(0,0,0,0.05); border-radius: 4px; }"
            )
            prev = QPushButton("◀")
            prev.setStyleSheet(btn_style)
            prev.setToolTip("往前一天")
            prev.clicked.connect(self._prev_day)
            self._grid.addWidget(prev, 1, 0, Qt.AlignLeft | Qt.AlignVCenter)

            next_btn = QPushButton("▶")
            next_btn.setStyleSheet(btn_style)
            next_btn.setToolTip("往后一天")
            next_btn.clicked.connect(self._next_day)
            self._grid.addWidget(next_btn, 1, days + 1, Qt.AlignCenter)

            dates = self._visible_dates()
            for i, d in enumerate(dates):
                lbl = QLabel(f"{self.WEEKDAYS[d.weekday()]}\n{d.strftime('%d')}")
                lbl.setAlignment(Qt.AlignCenter)
                if d == today:
                    lbl.setStyleSheet(
                        "color: #E67E22; font-size: 14px; font-weight: bold; padding: 3px;"
                        " background: #FFF3E0; border-radius: 6px;"
                    )
                else:
                    lbl.setStyleSheet(
                        "color: #5F6B7A; font-size: 14px; font-weight: bold; padding: 3px;"
                        " background: #E8EFF6; border-radius: 6px;"
                    )
                self._grid.addWidget(lbl, 1, i + 1)

            # ── 番剧行（每列竖排可点击按钮）──
            col_lists = [self._schedule.get(d.weekday(), []) for d in dates]
            if self._show_unwatched_only:
                # "只看未看"：过滤掉已看（手动 + 自动灰）的条目
                col_lists = [
                    [it for it in lst if not self._is_item_watched(it, d)]
                    for lst, d in zip(col_lists, dates)
                ]
            max_items = max((len(lst) for lst in col_lists), default=0)

            for i in range(max_items):
                for ci, lst in enumerate(col_lists):
                    if i >= len(lst):
                        continue
                    item = lst[i]
                    # 兼容旧数据：纯字符串 = 无集数信息；新结构 dict
                    name = item["name"] if isinstance(item, dict) else str(item)
                    date_key = dates[ci].strftime("%Y-%m-%d")
                    # 看过判定：手动点击标记 + 自动灰（统一走 _is_item_watched）
                    watched = self._is_item_watched(item, dates[ci])
                    name_color = "#0078D7" if watched else "#555555"

                    ep_badge = ""
                    if isinstance(item, dict):
                        ep = item.get("ep_status", 0)
                        total = item.get("total_eps", 0)
                        # 徽标无底色（只蓝字）：避免便签调低透明度时底块发白显得突兀
                        if total > 0:
                            ep_badge = (
                                f" <span style='color:#0078D7; font-size:12px; font-weight:bold;'>"
                                f"{ep}/{total}</span>"
                            )
                        elif ep > 0:
                            # 未录入总集数（如碧蓝之海第三季）：只显示已看集数
                            ep_badge = (
                                f" <span style='color:#0078D7; font-size:12px; font-weight:bold;'>"
                                f"已看{ep}</span>"
                            )
                    lbl = ClickableLabel(
                        f"<span style='color:{name_color}; font-size:15px;'>{name}</span>{ep_badge}"
                    )
                    lbl.setWordWrap(True)
                    lbl.setCursor(Qt.PointingHandCursor)
                    lbl.setStyleSheet(
                        "QLabel { background: transparent; border: none;"
                        " border-radius: 6px; padding: 5px 6px; }"
                        "QLabel:hover { background: rgba(0,0,0,0.06); }"
                    )
                    lbl.clicked.connect(lambda it=item, k=date_key: self._toggle_watched(it, k))
                    lbl._bgm_item = item          # 右键菜单数据
                    lbl._bgm_window = self
                    self._grid.addWidget(lbl, i + 2, ci + 1)

            self._grid.setRowStretch(max_items + 2, 1)
        finally:
            self._rebulding = False

    # ---------- 交互 ----------

    def _prev_day(self):
        if self._center_offset > -4:
            self._center_offset -= 1
            self._refresh_view()

    def _next_day(self):
        if self._center_offset < 4:
            self._center_offset += 1
            self._refresh_view()

    def _toggle_watched(self, item, date_key):
        """点击番剧名：状态开关——显示已看 → 取消看过；显示未看 → 标记看过。

        开关语义（用户确认的方案）：不论当前是手动标记还是自动灰，
        点一下严格切换到另一个显示状态（蓝→灰→蓝→灰…）。
        取消时一律记入 _unwatched，阻止自动灰"复活"，保证序列严格交替。

        item: 番剧数据 dict（含 subject_id）或旧版纯字符串。
        date_key: 该番播出日 "YYYY-MM-DD"（如周二列对应 2026-08-18）。
        """
        name = item["name"] if isinstance(item, dict) else str(item)

        # 当前显示状态：手动标记 + 自动灰（该日本季集号 ep <= ep_status），手动取消优先
        currently_watched = name in self._watched.get(date_key, [])
        if not currently_watched and isinstance(item, dict):
            if name not in self._unwatched.get(date_key, []):
                _ep = item.get("ep_status", 0)
                if _ep > 0:
                    _ep_num = (item.get("episodes") or {}).get(date_key.replace("-", ""))
                    if _ep_num is not None and _ep_num <= _ep:
                        currently_watched = True

        if currently_watched:
            # 取消看过：移除手动标记（若存在），一律记入手动取消列表
            # （覆盖自动灰，避免下次渲染自动灰"复活"导致序列错乱）
            lst = self._watched.get(date_key, [])
            if name in lst:
                lst.remove(name)
                if not lst:
                    del self._watched[date_key]
            self._unwatched.setdefault(date_key, []).append(name)
            mark = False
        else:
            # 标记看过：加入手动标记，并清除手动取消记录
            lst = self._watched.setdefault(date_key, [])
            if name not in lst:
                lst.append(name)
            ulst = self._unwatched.get(date_key, [])
            if name in ulst:
                ulst.remove(name)
                if not ulst:
                    del self._unwatched[date_key]
            mark = True

        self._refresh_view()
        self._mark_dirty()

        # 反向同步：仅新结构数据（有 subject_id）才可能回写 Bangumi
        if isinstance(item, dict) and item.get("subject_id"):
            threading.Thread(
                target=self._sync_back, args=(item, date_key, mark), daemon=True
            ).start()

    # ---------- 判定 / 过滤 ----------

    def _is_item_watched(self, item, date):
        """某番剧在指定日期是否显示为已看（手动标记 or 自动灰，手动取消优先）。"""
        name = item["name"] if isinstance(item, dict) else str(item)
        dk = date.strftime("%Y-%m-%d")
        if name in self._watched.get(dk, []):
            return True
        if name in self._unwatched.get(dk, []):
            return False
        if isinstance(item, dict):
            ep = item.get("ep_status", 0)
            if ep > 0:
                # episodes 映射值 = 本季内集号 ep（与 ep_status 同基准；
                # sort 是绝对编号，跨季番剧可能从 13 起，不能用于比较）
                n = (item.get("episodes") or {}).get(dk.replace("-", ""))
                if n is not None and n <= ep:
                    return True
        return False

    def _toggle_unwatched_filter(self, checked):
        """"只看未看"过滤开关。"""
        self._show_unwatched_only = checked
        self._refresh_view()

    # ---------- 右键菜单（番剧条目）----------

    def _show_item_menu(self, item, global_pos):
        """番剧名右键菜单：在 Bangumi 打开 / 集数标记（样式对齐便签右键菜单）。"""
        menu = QMenu(self)
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setStyleSheet(
            "QMenu {"
            " background-color: #FAFAFA;"
            " border: 1px solid #E0E0E0;"
            " border-radius: 10px;"
            " padding: 6px;"
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
        act_open = menu.addAction("在 Bangumi 打开")
        act_open.triggered.connect(
            lambda: webbrowser.open(f"https://bgm.tv/subject/{item['subject_id']}")
        )
        act_eps = menu.addAction("集数标记…")
        act_eps.triggered.connect(lambda: self._show_episode_dialog(item))
        menu.exec(global_pos)

    # ---------- 集数标记弹窗 ----------

    def _fetch_episode_list(self, subject_id, proxy_str=""):
        """拉取某条目完整剧集表 [{id, ep, sort, name, airdate}]（失败返回 []）。"""
        import requests
        headers = {
            "User-Agent": f"HunterHasCome/AniNote/{VERSION} (https://github.com/TurboHunter-CN/AniNote)",
            "X-Contact": "Bilibili: https://space.bilibili.com/499162799",
        }
        proxies = None
        if proxy_str:
            clean_proxy = proxy_str.replace("http://", "").replace("https://", "")
            proxies = {"http": f"http://{clean_proxy}", "https": f"http://{clean_proxy}"}
        try:
            r = requests.get(
                "https://api.bgm.tv/v0/episodes",
                params={"subject_id": subject_id, "limit": 100},
                headers=headers, proxies=proxies, timeout=15,
            )
            if r.status_code != 200:
                return []
            out = []
            for ep in r.json().get("data", []):
                out.append({
                    "id": ep.get("id"),
                    "ep": ep.get("ep"),
                    "sort": ep.get("sort"),
                    "name": ep.get("name_cn") or ep.get("name") or "",
                    "airdate": ep.get("airdate") or "",
                })
            return out
        except Exception:
            return []

    def _show_episode_dialog(self, item):
        """弹出该番剧的集数标记窗口。"""
        if not isinstance(item, dict) or not item.get("subject_id"):
            return
        dlg = EpisodeDialog(self, item)
        dlg.exec()

    # ---------- 反向同步（点击 → Bangumi 回写）----------

    def _sync_back(self, item, date_key, mark):
        """后台线程：按播出日反查该番剧集 → 调 Bangumi 标记/取消看过。

        失败降级策略（借鉴 Animeko 的同步可靠性设计）：
        - 拉剧集表失败 / PATCH 失败 → 自动重试 1 次（间隔 2 秒，覆盖网络瞬时抖动）
        - 重试后仍查不到剧集表 / 当天无匹配集 → 静默降级为本地标记
        - 重试后仍失败（未授权 / 接口报错）→ 通过 sync_failed 信号提示用户
        """
        try:
            import bangumi_oauth
            cfg = load_config()
            proxy = cfg.get("api_proxy", "")
            sid = item["subject_id"]
            nm = item.get("name") or item.get("subject_id") or "该番剧"

            # 拉剧集表（失败重试 1 次）
            ep_ids = self._find_episode_ids_by_date(sid, date_key, proxy)
            if not ep_ids:
                time.sleep(2)
                ep_ids = self._find_episode_ids_by_date(sid, date_key, proxy)
            if not ep_ids:
                return  # 无匹配剧集：本地标记已生效，Bangumi 侧无法精确到集，静默

            # PATCH 回写（失败重试 1 次）
            ok, msg = bangumi_oauth.mark_episodes_watched(
                sid, ep_ids, cfg, proxy_str=proxy, watched=mark
            )
            if not ok:
                time.sleep(2)
                ok, msg = bangumi_oauth.mark_episodes_watched(
                    sid, ep_ids, cfg, proxy_str=proxy, watched=mark
                )
            if not ok:
                self.sync_failed.emit(f"{nm}：{msg}")
        except Exception as e:
            nm = item.get("name") or item.get("subject_id") or "该番剧"
            self.sync_failed.emit(f"{nm}：{type(e).__name__}: {e}")

    def _find_episode_ids_by_date(self, subject_id, date_key, proxy_str=""):
        """按播出日反查某条目当天播出的剧集 id 列表（无匹配返回 []）。

        兼容 airdate 的 "2026-08-18" / "2026-8-18" 两种格式（统一去横线比较）。
        """
        import requests
        headers = {
            "User-Agent": f"HunterHasCome/AniNote/{VERSION} (https://github.com/TurboHunter-CN/AniNote)",
            "X-Contact": "Bilibili: https://space.bilibili.com/499162799",
        }
        proxies = None
        if proxy_str:
            clean_proxy = proxy_str.replace("http://", "").replace("https://", "")
            proxies = {"http": f"http://{clean_proxy}", "https": f"http://{clean_proxy}"}
        r = requests.get(
            "https://api.bgm.tv/v0/episodes",
            params={"subject_id": subject_id, "limit": 100},
            headers=headers, proxies=proxies, timeout=15,
        )
        if r.status_code != 200:
            return []
        target = date_key.replace("-", "")
        out = []
        for ep in r.json().get("data", []):
            ad = str(ep.get("airdate") or "").replace("-", "")
            if ad == target:
                out.append(ep.get("id"))
        return out

    def _on_sync_failed(self, msg):
        """主线程槽：反向同步失败的非模态提示（不阻塞交互）。"""
        mb = QMessageBox(
            QMessageBox.Warning, "同步到 Bangumi 失败", msg, QMessageBox.Ok, self
        )
        mb.setWindowModality(Qt.NonModal)
        mb.show()

    # ---------- 外部数据接口（app.py 调用）----------

    def _show_loading(self):
        self._status_text = "⏳ 正在跨次元连接 Bangumi...<br>拉取最新番剧数据"
        self._refresh_view()

    def _show_error(self, msg):
        self._status_text = str(msg)
        self._refresh_view()

    def _apply_schedule(self, schedule):
        """同步成功：更新番剧数据并渲染。"""
        self._status_text = None
        self._schedule = schedule if isinstance(schedule, dict) else {}
        if not self._schedule:
            self._status_text = "你目前在 Bangumi 上还没有标记「在看」的番剧哦~"
        # 更新标题为当天日期 + 完整星期名
        now = datetime_module.datetime.now()
        full_weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        title_str = f"{now.month}月{now.day}日 {full_weekdays[now.weekday()]}新番更新"
        self.header.title_edit.setText(title_str)
        self._refresh_view()
        self._mark_dirty()

    # ---------- 缩放联动 ----------

    def resizeEvent(self, event):
        super().resizeEvent(event)
        avail = self.bg_frame.width() - 24
        days = 3
        if avail >= 820:
            days = 7
        elif avail >= 560:
            days = 5
        if days != self._visible_days:
            self._visible_days = days
            self._refresh_view()

    # ---------- 持久化 ----------

    def _load_bangumi_state(self):
        """从便签 JSON 恢复番剧数据与标记。

        注意：JSON 序列化会把 int 键（星期几）转成字符串，读取时需转回 int。
        """
        if self.save_file and os.path.exists(self.save_file):
            try:
                with open(self.save_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("bangumi_schedule_data", {}) or {}
                self._schedule = {int(k): v for k, v in raw.items()}
                self._watched = data.get("bangumi_watched", {}) or {}
                self._unwatched = data.get("bangumi_unwatched", {}) or {}
            except Exception:
                pass

    def save_data(self):
        """保存时附加番剧数据与看过标记。"""
        if getattr(self, '_is_loading', False):
            return
        if self.width() < 250:
            return
        if not os.path.exists(SAVE_DIR):
            os.makedirs(SAVE_DIR)

        title = self.header.title_edit.text()
        new_path = make_save_path(title, exclude_path=self.save_file)
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        if self.save_file and os.path.exists(self.save_file) and os.path.abspath(self.save_file) != os.path.abspath(new_path):
            try:
                os.remove(self.save_file)
            except OSError:
                pass
        self.save_file = new_path

        html = self.text_edit.toHtml()
        note_dir = os.path.dirname(self.save_file).replace('\\', '/')
        _cleanup_orphan_images(note_dir, html)

        data = {
            "note_id": self.note_id,
            "title": title,
            "html_content": html,
            "x": self.x(), "y": self.y(),
            "width": self.width(), "height": self._persist_height(),
            "is_locked": self.is_locked,
            "is_always_on_top": getattr(self, 'is_always_on_top', True),
            "is_hidden": getattr(self, 'is_hidden', False),
            "is_collapsed": self._is_collapsed_for_save(),
            "stack_id": getattr(self, 'stack_id', ''),
            "stack_pos": int(getattr(self, 'stack_pos', 0) or 0),
            "bg_color": self.bg_color,
            "note_hotkey": getattr(self, '_note_hotkey', ''),
            "bangumi_schedule_data": self._schedule,
            "bangumi_watched": self._watched,
            "bangumi_unwatched": self._unwatched,
        }
        with open(self.save_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)


class EpisodeDialog(QDialog):
    """集数标记窗口：展示某番剧全部剧集，每集可独立勾选标记/取消，可一键全部看过。

    数据在后台线程拉取（剧集表优先用便签刷新时预缓存的 episode_list，避免重复请求；
    每集已看状态实时拉取），完成后经信号填充 UI；勾选变化即时 PATCH 回写 Bangumi。
    """

    data_ready = Signal(object)   # (episodes_list, watched_ids_or_None)
    watched_ready = Signal(object)  # 已看状态异步到达（watched_ids_set）
    patch_all_done = Signal()     # "全部看过"完成 → 主线程全勾选
    refresh_request = Signal()    # 标记变更 → 主窗口 _refresh_view

    # 风格统一：对齐控制面板——无边框圆角窗口（#FAFAFA 底 12px 圆角 + 白色圆角卡片 + 自定义标题栏）
    _QSS = """
        QFrame#dlg_bg { background: #FAFAFA; border-radius: 12px; border: 1px solid #EAEAEA; }
        QLabel#dlg_title { font-size: 15px; font-weight: bold; color: #333;
                           background: transparent; border: none; }
        QLabel#dlg_loading { color: #888; font-size: 13px; background: transparent; }
        QFrame#dlg_card { background: white; border-radius: 10px; border: 1px solid #EAEAEA; }
        QCheckBox { font-size: 13px; color: #444; spacing: 8px;
                    padding: 3px 6px; border-radius: 6px; background: transparent; }
        QCheckBox:hover { background: #E8F0FE; }
        QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px;
                               border: 1px solid #C0C8D0; background: white; }
        QCheckBox::indicator:hover { border-color: #0078D7; }
        QCheckBox::indicator:checked { background: #0078D7; border-color: #0078D7; }
        QScrollArea { border: none; background: transparent; }
        QScrollArea > QWidget > QWidget { background: transparent; }
        QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }
        QScrollBar::handle:vertical { background: #C0C0C0; border-radius: 3px; min-height: 20px; }
        QScrollBar::handle:vertical:hover { background: #A0A0A0; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QPushButton#dlg_all { padding: 8px; border: none; border-radius: 6px;
                              background: #0078D7; color: white; font-weight: bold; }
        QPushButton#dlg_all:hover { background: #005BA1; }
        QPushButton#dlg_all:disabled { background: #A0C8E8; }
    """

    def __init__(self, window, item, parent=None):
        super().__init__(parent)
        self._window = window
        self._item = item
        self._eps = []
        self._boxes = {}          # ep_id -> QCheckBox
        self._patch_busy = False  # 防并发 PATCH

        # 无边框 + 透明背景（圆角由 dlg_bg 承载），对齐控制面板外观
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(400, 480)
        self.setStyleSheet(self._QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        self.bg = QFrame()
        self.bg.setObjectName("dlg_bg")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 40))
        shadow.setOffset(0, 6)
        self.bg.setGraphicsEffect(shadow)
        outer.addWidget(self.bg)

        layout = QVBoxLayout(self.bg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 自定义标题栏（对齐控制面板 CustomTitleBar，可拖拽 + 关闭按钮）──
        bar = QFrame()
        bar.setStyleSheet("background: transparent;")
        bar.setFixedHeight(45)
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(20, 0, 10, 0)
        bar_layout.setSpacing(0)

        title = QLabel(item.get("name", ""))
        title.setObjectName("dlg_title")
        # 字重走 QFont 真实字面（QSS 的 font-weight 会触发 Qt 合成）
        title.setStyleSheet("color: #333;")
        title.setFont(fonts_mod.make_font(px=15, weight="bold"))
        bar_layout.addWidget(title)
        bar_layout.addStretch()

        close_btn = QPushButton(icon("close"))
        set_icon_font(close_btn, 16)
        close_btn.setFixedSize(36, 30)
        close_btn.setStyleSheet(
            "QPushButton { border: none; border-radius: 6px; background-color: transparent;"
            " font-size: 14px; color: #555; }"
            " QPushButton:hover { background-color: #E81123; color: white; }"
        )
        close_btn.clicked.connect(self.reject)
        bar_layout.addWidget(close_btn)
        layout.addWidget(bar)

        # 标题栏拖拽移动（仿控制面板 CustomTitleBar）
        bar._drag_pos = None
        def _bar_press(e):
            if e.button() == Qt.LeftButton:
                bar._drag_pos = e.globalPosition().toPoint() - self.pos()
                e.accept()
        def _bar_move(e):
            if bar._drag_pos is not None:
                self.move(e.globalPosition().toPoint() - bar._drag_pos)
                e.accept()
        def _bar_release(e):
            bar._drag_pos = None
        bar.mousePressEvent = _bar_press
        bar.mouseMoveEvent = _bar_move
        bar.mouseReleaseEvent = _bar_release

        # ── 内容区 ──
        content = QVBoxLayout()
        content.setContentsMargins(16, 6, 16, 14)
        content.setSpacing(10)
        layout.addLayout(content, 1)

        self._loading = QLabel("正在加载剧集列表…")
        self._loading.setObjectName("dlg_loading")
        self._loading.setAlignment(Qt.AlignCenter)
        content.addWidget(self._loading, 1)

        self._card = QFrame()
        self._card.setObjectName("dlg_card")
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(6, 6, 6, 6)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        # 滚动条自动隐藏：未滚动时透明，滚动时显示（对齐 MD 便签）
        setup_auto_hide_scrollbar(
            self._scroll.verticalScrollBar(), self._QSS,
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            " QScrollBar::handle:vertical { background: transparent; border-radius: 3px; min-height: 20px; }"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            " QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }",
        )
        card_layout.addWidget(self._scroll)
        self._card.hide()
        content.addWidget(self._card, 1)

        self._btn_all = QPushButton("全部看过")
        self._btn_all.setObjectName("dlg_all")
        self._btn_all.clicked.connect(self._mark_all)
        self._btn_all.hide()
        content.addWidget(self._btn_all)

        self.data_ready.connect(self._fill)
        self.watched_ready.connect(self._apply_watched)
        self.patch_all_done.connect(self._on_patch_all_done)
        self.refresh_request.connect(lambda: self._window._refresh_view())

        threading.Thread(target=self._load, daemon=True).start()

    # ---------- 数据加载 ----------

    def _load(self):
        try:
            cfg = load_config()
            proxy = cfg.get("api_proxy", "")
            sid = self._item["subject_id"]
            # 优先用便签刷新时预缓存的剧集表，避免每次打开弹窗都请求接口
            eps = self._item.get("episode_list") or []
            if not eps:
                eps = self._window._fetch_episode_list(sid, proxy)
            # 第一步：列表立即显示（watched_ids=None 表示勾选状态稍后到达）
            self.data_ready.emit((eps, None))
            # 第二步：后台拉每集已看状态（会变的数据不能缓存），到达后异步更新勾选
            watched_ids = set()
            try:
                import bangumi_oauth
                coll = bangumi_oauth.fetch_episode_collection(sid, cfg, proxy) or {}
                for eid, t in coll.items():
                    if t == bangumi_oauth.EP_COLLECT_TYPE_WATCHED:
                        watched_ids.add(eid)
            except Exception:
                pass
            self.watched_ready.emit(watched_ids)
        except Exception:
            self.data_ready.emit(([], set()))

    def _fill(self, payload):
        eps, watched_ids = payload
        self._loading.hide()
        self._btn_all.show()
        if not eps:
            self._loading.setText("该条目没有可标记的剧集")
            self._loading.show()
            self._card.hide()
            self._btn_all.hide()
            return

        container = QWidget()
        box_layout = QVBoxLayout(container)
        box_layout.setContentsMargins(8, 8, 8, 8)
        box_layout.setSpacing(4)

        for ep in eps:
            eid = ep.get("id")
            label = f"第{ep.get('ep') or ep.get('sort')}集"
            if ep.get("airdate"):
                label += f"  ·  {ep['airdate']}"
            cb = QCheckBox(label)
            # watched_ids 为 None 时先不勾（状态异步到达后由 _apply_watched 更新）
            if watched_ids is not None:
                cb.setChecked(eid in watched_ids)
            cb.toggled.connect(lambda checked, e=eid: self._toggle_ep(e, checked))
            box_layout.addWidget(cb)
            self._boxes[eid] = cb
        box_layout.addStretch(1)
        self._scroll.setWidget(container)
        self._card.show()
        self._scroll.show()

    def _apply_watched(self, watched_ids):
        """已看状态异步到达：批量更新勾选（blockSignals 避免触发 PATCH）。"""
        if not watched_ids:
            return
        for eid, cb in self._boxes.items():
            checked = eid in watched_ids
            if cb.isChecked() != checked:
                cb.blockSignals(True)
                cb.setChecked(checked)
                cb.blockSignals(False)

    # ---------- 交互 ----------

    def _toggle_ep(self, ep_id, checked):
        """单集勾选变化 → 即时 PATCH（后台线程）。"""
        if self._patch_busy:
            return
        self._patch_busy = True

        def work():
            try:
                import bangumi_oauth
                cfg = load_config()
                proxy = cfg.get("api_proxy", "")
                bangumi_oauth.mark_episodes_watched(
                    self._item["subject_id"], [ep_id], cfg,
                    proxy_str=proxy, watched=checked,
                )
                self.refresh_request.emit()
            except Exception:
                pass
            finally:
                self._patch_busy = False
        threading.Thread(target=work, daemon=True).start()

    def _mark_all(self):
        """全部看过：批量 PATCH 所有集。"""
        ids = [ep["id"] for ep in self._eps if ep.get("id")]
        if not ids:
            return

        def work():
            try:
                import bangumi_oauth
                cfg = load_config()
                proxy = cfg.get("api_proxy", "")
                bangumi_oauth.mark_episodes_watched(
                    self._item["subject_id"], ids, cfg,
                    proxy_str=proxy, watched=True,
                )
                self.patch_all_done.emit()
                self.refresh_request.emit()
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _on_patch_all_done(self):
        """全部看过成功后，把弹窗内所有勾选框设为已勾。"""
        for cb in self._boxes.values():
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)


# ---------- 全局操作 ----------

def create_global_new_note():
    """全局新建便签（由快捷键触发），放在上一个便签的右下方。"""
    note = AniNoteWindow()
    note.setWindowIcon(app_window_icon())
    if ACTIVE_NOTES and len(ACTIVE_NOTES) > 1:
        ref_note = ACTIVE_NOTES[-2]
        note.move(ref_note.x() + 40, ref_note.y() + 40)
    note.animated_show()
    note.activateWindow()
    note.setFocus()
    note.save_data()
    global_signaler.note_updated_signal.emit()


def create_global_new_habit():
    """全局新建事务追踪器（由控制面板按钮触发）。"""
    tracker = HabitTrackerWindow()
    tracker.setWindowIcon(app_window_icon())
    if ACTIVE_NOTES and len(ACTIVE_NOTES) > 1:
        ref_note = ACTIVE_NOTES[-2]
        tracker.move(ref_note.x() + 40, ref_note.y() + 40)
    tracker.header.title_edit.setText("事务追踪器")
    tracker.resize(580, 450)
    tracker.show()
    tracker.activateWindow()
    tracker.setFocus()
    tracker.save_data()
    global_signaler.note_updated_signal.emit()


def create_global_new_schedule():
    """全局新建日程表（由控制面板按钮触发）。"""
    sched = ScheduleWindow()
    sched.setWindowIcon(app_window_icon())
    if ACTIVE_NOTES and len(ACTIVE_NOTES) > 1:
        ref_note = ACTIVE_NOTES[-2]
        sched.move(ref_note.x() + 40, ref_note.y() + 40)
    sched.header.title_edit.setText("日程表")
    sched.resize(760, 520)
    sched.show()
    sched.activateWindow()
    sched.setFocus()
    sched.save_data()
    global_signaler.note_updated_signal.emit()


def toggle_all_notes():
    """切换便签的全局可见性。

    隐藏时：记录当前所有可见的便签，随后隐藏它们。
    显示时：只恢复上次被此操作隐藏的便签，之前已单独隐藏的保持不动。
    """
    global _TOGGLE_HIDDEN_NOTES

    if not ACTIVE_NOTES:
        create_global_new_note()
        return

    any_visible = any(note.isVisible() for note in ACTIVE_NOTES)

    if any_visible:
        _TOGGLE_HIDDEN_NOTES.clear()
        for note in ACTIVE_NOTES:
            if note.isVisible():
                _TOGGLE_HIDDEN_NOTES.add(note.note_id)
                note.header.title_edit.clearFocus()
                note.text_edit.clearFocus()
                note.is_hidden = True
                note.save_data()
                note.animated_hide()
    else:
        for note in ACTIVE_NOTES:
            if note.note_id in _TOGGLE_HIDDEN_NOTES:
                note.is_hidden = False
                note.save_data()
                note.animated_show()
                note.activateWindow()
        _TOGGLE_HIDDEN_NOTES.clear()


def show_all_notes():
    """强制显示所有便签，无视之前的隐藏状态。"""
    for note in ACTIVE_NOTES:
        if not note.isVisible():
            note.is_hidden = False
            note.save_data()
            note.animated_show()
            note.activateWindow()
