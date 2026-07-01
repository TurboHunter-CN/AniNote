"""
AniNote 核心引擎 — 便签窗口、富文本编辑、配置管理。

提供可拖拽、可缩放的无边框便签窗口，支持富文本编辑、待办事项切换、
全局热键通信以及 JSON 持久化存储。
"""

import sys
import json
import os
import uuid
import datetime as datetime_module

VERSION = "3.1"

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QFrame, QMenu, QGraphicsDropShadowEffect, QPushButton,
    QColorDialog, QMessageBox, QSizePolicy, QFontDialog,
    QLineEdit, QTextEdit, QTextBrowser, QLabel, QDialog, QSlider, QStackedWidget,
    QFontComboBox, QSpinBox, QScrollArea, QGridLayout, QRadioButton, QDateEdit,
)
from PySide6.QtCore import Qt, QObject, Signal, QTimer, QDate
from PySide6.QtGui import QColor, QFont, QCursor, QTextCursor, QDesktopServices

from icons import icon, set_icon_font

# ---------- 路径初始化 ----------

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SAVE_DIR = os.path.join(BASE_DIR, "notes_data")
CONFIG_FILE = os.path.join(BASE_DIR, "aninote_config.json")
ACTIVE_NOTES = []
_TOGGLE_HIDDEN_NOTES = set()   # 记录被「全局隐藏」操作隐藏的便签 ID，用于恢复时只显示这些

DEFAULT_CONFIG = {
    "toggle_hotkey": "alt+n",
    "new_hotkey": "alt+m",
    "show_all_hotkey": "alt+shift+n",
    "disable_all_hotkey": "ctrl+shift+a",
    "font_family": "Microsoft YaHei",
    "autostart": True,
    "skin": "极简模式",
    "is_first_run": True,
    "save_dir": "default",          # "default" 表示使用程序同级目录，保证便携性
    "bangumi_uid": "",
    "enable_bangumi": False,
    "api_proxy": "",
    "last_bangumi_sync": "",        # 上次同步日期（YYYY-MM-DD），用于每日仅自动刷新一次
    "export_dir": "default",        # "default" 表示使用程序同级目录下的「导出的便签文本」文件夹
}


def load_config():
    """加载配置文件，缺失字段自动回退到默认值。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_CONFIG


def save_config(cfg):
    """将配置字典写入 JSON 文件。"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)


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


# ---------- 全局信号中枢 ----------

class GlobalSignaler(QObject):
    """应用级信号中转站，解耦模块间的调用关系。"""
    toggle_signal = Signal()
    new_note_signal = Signal()
    show_all_signal = Signal()
    note_updated_signal = Signal()
    open_panel_signal = Signal()
    config_changed_signal = Signal()
    force_sync_bangumi_signal = Signal()


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


# ---------- 富文本编辑器 ----------

class NoteTextEdit(QTextBrowser):
    """便签正文编辑器，支持内联待办事项点击切换。"""

    def __init__(self, parent_window):
        super().__init__(parent_window.bg_frame)
        self.parent_window = parent_window
        self.setReadOnly(False)
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


# ---------- 标题栏 ----------

class HeaderBar(QWidget):
    """便签标题栏，支持拖动窗口和格式化工具栏。"""

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
        self.title_edit.setStyleSheet("""
            QLineEdit { border: none; background: transparent; font-size: 18px;
                        font-weight: bold; color: #222; font-family: 'Microsoft YaHei'; padding: 2px; }
            QLineEdit:focus { background: rgba(255, 255, 255, 0.5); border-radius: 4px; }
        """)
        self.title_edit.setPlaceholderText("请输入便签标题...")
        self.title_edit.textChanged.connect(lambda: self.parent_window._mark_dirty())
        self.title_edit.editingFinished.connect(lambda: global_signaler.note_updated_signal.emit())
        self.title_layout.addWidget(self.title_edit)

        self.drag_handle = QLabel("⋮⋮")
        self.drag_handle.setToolTip("按住此处或空白处拖动便签")
        self.drag_handle.setStyleSheet("color: #aaa; font-weight: bold; font-size: 16px; padding: 0 5px;")
        self.drag_handle.setCursor(Qt.OpenHandCursor)
        self.title_layout.addWidget(self.drag_handle)

        self.title_layout.addStretch()
        layout.addLayout(self.title_layout)

        self.toolbar_container = QWidget()
        self.toolbar_layout = QHBoxLayout(self.toolbar_container)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(5)
        layout.addWidget(self.toolbar_container)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.parent_window.is_locked:
            self._is_dragging = True
            self._drag_pos = event.globalPosition().toPoint() - self.parent_window.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._is_dragging and not self.parent_window.is_locked:
            self.parent_window.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._is_dragging = False
        self.parent_window.save_data()


# ---------- 缩放手柄 ----------

class ResizeHandle(QWidget):
    """右下角拖拽缩放控件。"""

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.parent_window = parent_window
        self.setFixedSize(20, 20)
        self.setCursor(Qt.SizeFDiagCursor)
        self.setStyleSheet("background-color: rgba(0, 0, 0, 0.05); border-bottom-right-radius: 12px;")
        self._is_resizing = False
        self._start_pos = None
        self._start_size = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.parent_window.is_locked:
            self._is_resizing = True
            self._start_pos = event.globalPosition().toPoint()
            self._start_size = self.parent_window.size()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._is_resizing and not self.parent_window.is_locked:
            delta = event.globalPosition().toPoint() - self._start_pos
            new_w = max(self.parent_window.minimumWidth(), self._start_size.width() + delta.x())
            new_h = max(self.parent_window.minimumHeight(), self._start_size.height() + delta.y())
            self.parent_window.resize(new_w, new_h)
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
        
        component_style = """
            QWidget { border: 1px solid #D1D1D1; border-radius: 6px; background-color: #FFFFFF; color: #333333; font-family: 'Microsoft YaHei'; font-size: 13px; padding-left: 5px; }
            QWidget:hover { border: 1px solid #0078D7; }
            QWidget:focus { border: 1px solid #0078D7; background-color: #FCFCFC; }
        """

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
            btn.setStyleSheet(f"background-color: {c}; border-radius: 12px; border: 1px solid #ccc;")
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
            btn.setStyleSheet(f"background-color: rgb({r}, {g}, {b}); border-radius: 12px; border: 1px solid #ccc;")
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

        self.note_id = note_id if note_id else uuid.uuid4().hex
        self.save_file = os.path.join(SAVE_DIR, f"{self.note_id}.json")

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(320, 280)
        self.bg_color = [255, 249, 196, 242]

        self.is_locked = False
        self.is_always_on_top = True
        self._deleted = False   # 标记为已删除，防止 closeEvent 重新写盘

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

        frame_layout = QVBoxLayout(self.bg_frame)
        frame_layout.setContentsMargins(12, 12, 12, 12)
        frame_layout.setSpacing(5)

        self.header = HeaderBar(self)
        frame_layout.addWidget(self.header)

        self.format_panel = FormatPanel(self)
        frame_layout.addWidget(self.format_panel)

        self._build_toolbar()

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

        self.text_edit = NoteTextEdit(self)
        self.text_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.text_edit.setStyleSheet(
            f"QTextEdit {{ border: none; background: transparent; font-size: 18px; "
            f"font-family: '{cfg['font_family']}'; color: #333333; }}"
        )
        self.text_edit.textChanged.connect(self._mark_dirty)
        frame_layout.addWidget(self.text_edit)

        # 右下角缩放手柄
        size_grip_layout = QHBoxLayout()
        size_grip_layout.setContentsMargins(0, 0, 0, 0)
        size_grip_layout.addStretch()
        self.size_grip = ResizeHandle(self)
        size_grip_layout.addWidget(self.size_grip)
        frame_layout.addLayout(size_grip_layout)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.text_edit.setContextMenuPolicy(Qt.CustomContextMenu)
        self.text_edit.customContextMenuRequested.connect(self.show_context_menu)

        self.load_data()

        # Bangumi 新番便签特殊初始化
        if self.note_id == "bangumi_schedule":
            self._init_bangumi_mode()

        self.apply_window_states()
        self._apply_bg_color()

        if self not in ACTIVE_NOTES:
            ACTIVE_NOTES.append(self)
    
    # --- 字体与颜色控制接口 ---
    def change_font_family(self, font):
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
        self.header.toolbar_layout.addStretch()

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
        if not os.path.exists(self.save_file):
            self.is_always_on_top = False
            self.bg_color = [235, 245, 255, 242]
            self._apply_bg_color()

        self.text_edit.setReadOnly(True)
        self.text_edit.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        self.text_edit.setOpenExternalLinks(True)
        self.header.title_edit.setReadOnly(True)
        self.header.title_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
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
        new_note.show()
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
        if os.path.exists(self.save_file):
            os.remove(self.save_file)
        self.close()
        global_signaler.note_updated_signal.emit()

    def show_context_menu(self, pos):
        """构建并显示右键上下文菜单。"""
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setStyleSheet(
            "QMenu {"
            " background-color: #FFFFFF;"
            " border: 1px solid #DDDDDD;"
            " border-radius: 6px;"
            " padding: 3px;"
            " }"
            " QMenu::item {"
            " padding: 5px 18px;"
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

        lock_action = menu.addAction(
            "解除锁定" if self.is_locked else "锁定便签 (防误触)"
        )
        top_action = menu.addAction(
            "取消置顶" if self.is_always_on_top else "置顶"
        )
        open_panel_action = menu.addAction("打开控制台")
        menu.addSeparator()

        hide_single_action = menu.addAction("暂时隐藏此便签")
        del_action = menu.addAction("删除此便签")
        menu.addSeparator()

        cfg = load_config()
        hide_action = menu.addAction(
            f"隐藏全部便签 ({cfg['toggle_hotkey'].upper()})"
        )
        show_all_action = menu.addAction("显示全部便签")

        action = menu.exec(self.mapToGlobal(pos))

        if action == lock_action:
            self._toggle_lock()
        elif action == top_action:
            self._toggle_always_on_top()
        elif action == hide_single_action:
            self.is_hidden = True
            self.save_data()
            self.hide()
        elif action == open_panel_action:
            global_signaler.open_panel_signal.emit()
        elif action == del_action:
            self.delete_note()
        elif action == hide_action:
            toggle_all_notes()
        elif action == show_all_action:
            show_all_notes()

    def _apply_lock_ui(self):
        """将当前 is_locked 状态同步到所有 UI 控件。消除 load_data 与 _toggle_lock 的重复代码。"""
        locked = self.is_locked
        self.header.toolbar_container.setVisible(not locked)
        self.header.drag_handle.setVisible(not locked)
        self.size_grip.setVisible(not locked)
        self.header.title_edit.setReadOnly(locked)
        self.text_edit.setReadOnly(locked)
        if locked:
            self.text_edit.setTextInteractionFlags(Qt.NoTextInteraction)
            self.header.title_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self.text_edit.clearFocus()
            self.header.title_edit.clearFocus()
        else:
            self.text_edit.setTextInteractionFlags(Qt.TextEditorInteraction)
            self.header.title_edit.setAttribute(Qt.WA_TransparentForMouseEvents, False)
    
    def _apply_bg_color(self):
        """将当前的背景色和透明度动态渲染到便签底板上。"""
        r, g, b, a = self.bg_color
        # 转换 Alpha 通道：Qt 取值 0-255，CSS rgba 需要 0.0-1.0
        alpha_css = a / 255.0
        
        # 保留新番便签特有的边框
        border_css = "border: 2px solid #b3d7ff;" if self.note_id == "bangumi_schedule" else ""
        
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
        data = {
            "title": self.header.title_edit.text(),
            "html_content": self.text_edit.toHtml(),
            "x": self.x(), "y": self.y(),
            "width": self.width(), "height": self.height(),
            "is_locked": self.is_locked,
            "is_always_on_top": getattr(self, 'is_always_on_top', True),
            "is_hidden": getattr(self, 'is_hidden', False),
            "bg_color": self.bg_color,
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

    def load_data(self):
        """从 JSON 文件恢复便签状态。"""
        self._is_loading = True     # 护盾：加载期间禁止自动保存
        if os.path.exists(self.save_file):
            try:
                with open(self.save_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.header.title_edit.setText(data.get("title", get_new_note_title()))
                    self.text_edit.setHtml(data.get("html_content", ""))
                    x = data.get("x", 100)
                    y = data.get("y", 100)
                    w = max(data.get("width", 320), 300)
                    h = max(data.get("height", 320), 280)
                    self.setGeometry(x, y, w, h)
                    self.is_locked = data.get("is_locked", False)
                    self.is_hidden = data.get("is_hidden", False)
                    self.is_always_on_top = data.get("is_always_on_top", True)
                    self.bg_color = data.get("bg_color", [255, 249, 196, 242])
                    self.format_panel.opacity_slider.setValue(int(round(self.bg_color[3] / 2.55)))
                    self._apply_lock_ui()
            except Exception as e:
                print(f"[AniNote] 加载便签 {self.note_id} 失败: {e}")
        else:
            self.header.title_edit.setText(get_new_note_title())

        self._is_loading = False

    def update_button_hints(self):
        """配置变更后更新工具栏按钮的快捷键提示。"""
        cfg = load_config()
        self.new_note_btn.setToolTip(f"新建 ({cfg.get('new_hotkey', 'alt+m').upper()})")


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
            self.show()
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

        # 插入到 text_edit 原来的位置
        frame_layout = self.bg_frame.layout()
        idx = frame_layout.indexOf(self.text_edit)
        frame_layout.insertWidget(idx, scroll)

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
        for h in self._habits:
            if h["id"] == hab_id:
                h.setdefault("records", {})[date_key] = checked
                break
        self._refresh_habit_list()
        self.save_data()

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

        dialog = QDialog(self)
        dialog.setWindowTitle("新建事务")
        dialog.setFixedSize(380, 360)
        dialog.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        # 名称
        name_lbl = QLabel("事务名称：")
        name_lbl.setStyleSheet("font-size: 13px; color: #333;")
        name_input = QLineEdit()
        name_input.setPlaceholderText("例如：早睡早起")
        name_input.setStyleSheet("padding: 8px; border: 1px solid #ccc; border-radius: 5px; font-size: 14px;")

        # 颜色
        color_lbl = QLabel("标记颜色：")
        color_lbl.setStyleSheet("font-size: 13px; color: #333; margin-top: 4px;")
        preset_colors = ["#E81123", "#FF8C00", "#107C10", "#0078D7", "#881798", "#333333"]
        color_btns = []
        selected_color = [preset_colors[0]]

        def on_color_click(c):
            selected_color[0] = c
            for b, oc in zip(color_btns, preset_colors):
                highlight = "3px solid #0078D7" if oc == c else "1px solid #ccc"
                b.setStyleSheet(
                    f"QPushButton {{ background-color: {oc}; border-radius: 11px; "
                    f"border: {highlight}; }}"
                )

        color_layout = QHBoxLayout()
        color_layout.setSpacing(6)
        for c in preset_colors:
            btn = QPushButton()
            btn.setFixedSize(22, 22)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"background-color: {c}; border-radius: 11px; border: 1px solid #ccc;")
            btn.clicked.connect(lambda checked, clr=c: on_color_click(clr))
            color_layout.addWidget(btn)
            color_btns.append(btn)
        color_layout.addStretch()

        # 模式选择
        mode_lbl = QLabel("打卡模式：")
        mode_lbl.setStyleSheet("font-size: 13px; color: #333; margin-top: 4px;")
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
        param_input.setStyleSheet("padding: 4px; border: 1px solid #ccc; border-radius: 4px;")
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
        date_edit.setDate(QDate.currentDate())
        date_edit.setDisplayFormat("yyyy-MM-dd")
        date_edit.setStyleSheet("padding: 4px; border: 1px solid #ccc; border-radius: 4px; font-size: 13px;")
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
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(dialog.reject)
        ok_btn = QPushButton("添加")
        ok_btn.setStyleSheet("background: #0078D7; color: white; padding: 6px 20px; border-radius: 5px; font-weight: bold;")
        ok_btn.clicked.connect(dialog.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)

        layout.addWidget(name_lbl)
        layout.addWidget(name_input)
        layout.addWidget(color_lbl)
        layout.addLayout(color_layout)
        layout.addWidget(mode_lbl)
        layout.addLayout(mode_layout)
        layout.addWidget(param_widget)
        layout.addLayout(btn_layout)

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
        if os.path.exists(self.save_file):
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

        data = {
            "title": self.header.title_edit.text(),
            "html_content": self.text_edit.toHtml(),
            "x": self.x(), "y": self.y(),
            "width": self.width(), "height": self.height(),
            "is_locked": self.is_locked,
            "is_always_on_top": getattr(self, 'is_always_on_top', True),
            "is_hidden": getattr(self, 'is_hidden', False),
            "bg_color": self.bg_color,
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


# ---------- 全局操作 ----------

def create_global_new_note():
    """全局新建便签（由快捷键触发），放在上一个便签的右下方。"""
    note = AniNoteWindow()
    if ACTIVE_NOTES and len(ACTIVE_NOTES) > 1:
        ref_note = ACTIVE_NOTES[-2]
        note.move(ref_note.x() + 40, ref_note.y() + 40)
    note.show()
    note.activateWindow()
    note.setFocus()
    note.save_data()
    global_signaler.note_updated_signal.emit()


def create_global_new_habit():
    """全局新建事务追踪器（由控制面板按钮触发）。"""
    tracker = HabitTrackerWindow()
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
                note.hide()
    else:
        for note in ACTIVE_NOTES:
            if note.note_id in _TOGGLE_HIDDEN_NOTES:
                note.is_hidden = False
                note.save_data()
                note.show()
                note.activateWindow()
        _TOGGLE_HIDDEN_NOTES.clear()


def show_all_notes():
    """强制显示所有便签，无视之前的隐藏状态。"""
    for note in ACTIVE_NOTES:
        if not note.isVisible():
            note.is_hidden = False
            note.save_data()
            note.show()
            note.activateWindow()
