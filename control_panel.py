"""
AniNote 控制面板 — 便签墙管理、系统设置、关于页面。

提供便签卡片的流式布局、搜索过滤、全局设置修改（存储路径、
字体、快捷键、Bangumi 集成、自启等），以及与主引擎的双向通信。
"""


import os
import json
import time
import threading
import webbrowser

from icons import icon, set_icon_font

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QStackedWidget, QLabel, QPushButton, QGridLayout,
    QFormLayout, QLineEdit, QCheckBox, QComboBox,
    QScrollArea, QFrame, QToolTip, QFontComboBox,

    QGraphicsDropShadowEffect, QSizeGrip, QMessageBox, QMenu, QFileDialog,
)
from PySide6.QtGui import QFont, QColor, QTextDocument, QCursor, QPainter
from PySide6.QtCore import (
    Qt, Signal, Property, QPropertyAnimation, QEasingCurve, QRect, QTimer, QSize,
)

from main import load_config, save_config, SAVE_DIR
import bangumi_oauth as bgm_oauth


# ==========================================
#  滑动胶囊开关 (Toggle Switch)
# ==========================================

class ToggleSwitch(QCheckBox):
    """自定义开关控件：以胶囊滑轨 + 滚珠动画替代原生勾选框。

    通过 QPropertyAnimation 驱动 position 属性实现 200ms 平滑过渡。
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self._position = 0.0
        self.animation = QPropertyAnimation(self, b"position")
        self.animation.setEasingCurve(QEasingCurve.InOutQuad)
        self.animation.setDuration(200)
        self.stateChanged.connect(self.setup_animation)

    @Property(float)
    def position(self):
        return self._position

    @position.setter
    def position(self, pos):
        self._position = pos
        self.update()

    def setup_animation(self, state):
        self.animation.stop()
        self.animation.setEndValue(1.0 if state else 0.0)
        self.animation.start()

    def sizeHint(self):
        """按文本实际宽度计算控件宽度，避免文字被裁剪。"""
        text_w = self.fontMetrics().horizontalAdvance(self.text())
        return QSize(42 + 12 + text_w, max(self.fontMetrics().height() + 8, 26))

    def showEvent(self, event):
        # 面板打开时重置滚珠位置，避免动画残留
        self._position = 1.0 if self.isChecked() else 0.0
        super().showEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        h, w = 22, 42
        y = (self.height() - h) // 2
        x = 0

        # 轨道
        track_color = QColor("#0078D7") if self.isChecked() else QColor("#cccccc")
        p.setBrush(track_color)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(x, y, w, h, h / 2, h / 2)

        # 滚珠
        circle_color = QColor("white")
        circle_radius = h - 4
        circle_x = x + 2 + self._position * (w - circle_radius - 4)
        p.setBrush(circle_color)
        p.drawEllipse(int(circle_x), int(y + 2), int(circle_radius), int(circle_radius))

        # 标签文字
        p.setPen(QColor("#333"))
        p.setFont(self.font())
        text_rect = QRect(w + 10, 0, self.width() - w - 10, self.height())
        p.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, self.text())


# ==========================================
#  流式网格布局
# ==========================================

class FlowWidget(QWidget):
    """自适应网格容器：根据自身宽度动态计算列数，自动排列子控件。"""

    ITEM_WIDTH = 175

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setSpacing(15)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.items = []

    def add_item(self, widget):
        self.items.append(widget)
        self.rearrange()

    def clear_items(self):
        for widget in self.items:
            self.grid.removeWidget(widget)
            widget.deleteLater()
        self.items = []

    def resizeEvent(self, event):
        self.rearrange()
        super().resizeEvent(event)

    def rearrange(self):
        if not self.items:
            return
        w = self.width()
        cols = max(1, (w - 20) // self.ITEM_WIDTH)
        for i, widget in enumerate(self.items):
            self.grid.addWidget(widget, i // cols, i % cols)


# ==========================================
#  便签预览卡片
# ==========================================

class NoteCard(QFrame):
    """便签墙上的预览卡片，展示标题与正文摘要。

    信号:
        clicked(str): 用户点击卡片，携带 note_id。
        delete_clicked(str): 用户点击删除按钮。
        set_top_clicked(str, bool): 用户通过右键菜单切换置顶。
    """

    clicked = Signal(str)
    delete_clicked = Signal(str)
    set_top_clicked = Signal(str, bool)
    export_clicked = Signal(str)
    set_hotkey_clicked = Signal(str)  # note_id

    def __init__(self, note_info, parent=None):
        super().__init__(parent)
        self.note_id = note_info["id"]
        self.is_top = note_info["is_top"]
        self._note_hotkey = note_info.get("note_hotkey", "")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(160, 160)
        r, g, b = note_info.get("bg_color", [255, 249, 196])[:3]
        self.setStyleSheet(
            f"QFrame {{ background-color: rgb({r}, {g}, {b}); border-radius: 10px;"
            f" border: 1px solid rgba(0,0,0,0.1); }}"
            f" QFrame:hover {{ border: 2px solid #0078D7; }}"
        )

        card_layout = QVBoxLayout(self)
        card_layout.setContentsMargins(10, 10, 10, 10)
        card_layout.setSpacing(5)

        title_lbl = QLabel(note_info["title"])
        title_lbl.setStyleSheet(
            "font-weight: bold; font-size: 14px; color: #222;"
            " border: none; background: transparent;"
        )
        title_lbl.setFixedHeight(20)
        card_layout.addWidget(title_lbl)

        preview = QLabel(note_info["text"])
        preview.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        preview.setWordWrap(True)
        preview.setStyleSheet(
            "border: none; background: transparent; color: #555; font-size: 13px;"
        )
        preview.setFixedHeight(75)

        # 防止子控件拦截鼠标事件，确保点击穿透到卡片
        preview.setAttribute(Qt.WA_TransparentForMouseEvents)
        title_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        card_layout.addWidget(preview)

        bottom_layout = QHBoxLayout()
        if self.is_top:
            top_icon = QLabel(icon("push_pin"))
            set_icon_font(top_icon, 14)
            top_icon.setStyleSheet("background: transparent; border: none; font-size: 14px; color: #555;")
            bottom_layout.addWidget(top_icon)

        bottom_layout.addStretch()
        self.del_btn = QPushButton(icon("delete"))
        self.del_btn.setFixedSize(26, 26)
        self.del_btn.setToolTip("删除此便签")
        set_icon_font(self.del_btn, 16)
        self.del_btn.setStyleSheet(
            "QPushButton { border: none; background: rgba(255,0,0,0.1); border-radius: 5px;"
            " color: #555; }"
            " QPushButton:hover { background: rgba(255,0,0,0.4); }"
        )
        self.del_btn.clicked.connect(lambda: self.delete_clicked.emit(self.note_id))
        bottom_layout.addWidget(self.del_btn)
        card_layout.addLayout(bottom_layout)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.note_id)

    def show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WA_TranslucentBackground)
        # 不使用 set_icon_font 以免 Material Symbols 拉丁字符替换系统字体
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
        )
        top_action = menu.addAction(
            "置于底部 (贴在桌面)"
            if self.is_top
            else "置于最顶部"
        )
        export_action = menu.addAction("导出为 Word 文档 (.doc)")
        hk_label = f"设置便签快捷键 ({self._note_hotkey.upper()})" if self._note_hotkey else "设置便签快捷键"
        hotkey_action = menu.addAction(hk_label)
        action = menu.exec(self.mapToGlobal(pos))
        if action == top_action:
            self.is_top = not self.is_top
            self.set_top_clicked.emit(self.note_id, self.is_top)
        elif action == export_action:
            self.export_clicked.emit(self.note_id)
        elif action == hotkey_action:
            self.set_hotkey_clicked.emit(self.note_id)


# ==========================================
#  自定义标题栏
# ==========================================

class CustomTitleBar(QWidget):
    """无边框窗口的自定义标题栏，支持拖拽和最大化/最小化/关闭。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.setFixedHeight(45)
        self.setStyleSheet("background-color: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 10, 0)
        title_label = QLabel("AniNote")
        title_label.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #333;"
            " font-family: 'Microsoft YaHei';"
        )
        layout.addWidget(title_label)
        layout.addStretch()

        btn_style = (
            "QPushButton { border: none; border-radius: 6px; background-color: transparent;"
            " font-size: 14px; color: #555; }"
            " QPushButton:hover { background-color: rgba(0,0,0,0.08); color: #000; }"
        )
        close_style = (
            "QPushButton { border: none; border-radius: 6px; background-color: transparent;"
            " font-size: 14px; color: #555; }"
            " QPushButton:hover { background-color: #E81123; color: white; }"
        )

        self.min_btn = QPushButton("—")
        self.min_btn.setFixedSize(36, 30)
        self.min_btn.setStyleSheet(btn_style)
        self.min_btn.clicked.connect(self.parent_window.showMinimized)

        self.max_btn = QPushButton(icon("crop_square"))
        self.max_btn.setFixedSize(36, 30)
        set_icon_font(self.max_btn, 16)
        self.max_btn.setStyleSheet(btn_style)
        self.max_btn.clicked.connect(self.toggle_maximize)

        self.close_btn = QPushButton(icon("close"))
        self.close_btn.setFixedSize(36, 30)
        set_icon_font(self.close_btn, 16)
        self.close_btn.setStyleSheet(close_style)
        self.close_btn.clicked.connect(self.parent_window.close)

        layout.addWidget(self.min_btn)
        layout.addWidget(self.max_btn)
        layout.addWidget(self.close_btn)
        self._is_dragging = False
        self._start_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._is_dragging = True
            self._start_pos = event.globalPosition().toPoint() - self.parent_window.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._is_dragging and not self.parent_window.isMaximized():
            self.parent_window.move(event.globalPosition().toPoint() - self._start_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._is_dragging = False

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggle_maximize()

    def toggle_maximize(self):
        if self.parent_window.isMaximized():
            self.parent_window.showNormal()
        else:
            self.parent_window.showMaximized()


# ==========================================
#  控制面板主窗口
# ==========================================

class ControlPanel(QWidget):
    """AniNote 总控制台，包含便签墙、设置、关于三个页面。

    信号:
        request_open_note(str): 请求打开指定便签。
        request_new_note(): 请求新建便签。
        request_delete_note(str): 请求删除指定便签。
        request_set_top(str, bool): 请求切换便签置顶状态。
        settings_changed(dict): 设置变更时发射完整配置字典。
    """

    request_open_note = Signal(str)
    request_new_note = Signal()
    request_new_habit = Signal()
    request_delete_note = Signal(str)
    request_set_top = Signal(str, bool)
    request_export_note = Signal(str)
    request_set_note_hotkey = Signal(str)
    settings_changed = Signal(dict)
    request_check_update = Signal()
    oauth_done_signal = Signal(bool, str)   # Bangumi 授权完成 (成功, 消息)

    # 导航按钮默认/选中样式（QPushButton 保留用于兼容）
    NAV_STYLE_NORMAL = (
        "QPushButton { text-align: left; padding-left: 15px; border: none;"
        " border-radius: 8px; background-color: transparent; font-size: 14px; color: #555; }"
        " QPushButton:hover { background-color: #F0F4F8; color: #0078D7; }"
    )
    NAV_STYLE_ACTIVE = (
        "QPushButton { text-align: left; padding-left: 15px; border: none;"
        " border-radius: 8px; background-color: #E6F2FF; font-size: 14px;"
        " font-weight: bold; color: #0078D7; }"
    )
    # QLabel 版（Material Icon 导航按钮用）
    NAV_LBL_NORMAL = (
        "QLabel { padding: 10px 15px; color: #555; background: transparent;"
        " border-radius: 8px; }"
    )
    NAV_LBL_HOVER = (
        "QLabel { padding: 10px 15px; color: #0078D7; background-color: #F0F4F8;"
        " border-radius: 8px; }"
    )
    NAV_LBL_ACTIVE = (
        "QLabel { padding: 10px 15px; color: #0078D7; background-color: #E6F2FF;"
        " border-radius: 8px; font-weight: bold; }"
    )

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowSystemMenuHint | Qt.WindowMinimizeButtonHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(900, 600)
        self.setMinimumSize(800, 500)

        # Bangumi OAuth 状态（授权中可取消）
        self._oauth_cancel_event = None
        self._oauth_busy = False

        wrapper_layout = QVBoxLayout(self)
        wrapper_layout.setContentsMargins(15, 15, 15, 15)

        self.bg_frame = QFrame(self)
        self.bg_frame.setObjectName("bg_frame")
        self.bg_frame.setStyleSheet(
            "QFrame#bg_frame { background-color: #FAFAFA;"
            " border-radius: 12px; border: 1px solid #EAEAEA; }"
        )
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 40))
        shadow.setOffset(0, 6)
        self.bg_frame.setGraphicsEffect(shadow)
        wrapper_layout.addWidget(self.bg_frame)

        bg_layout = QVBoxLayout(self.bg_frame)
        bg_layout.setContentsMargins(0, 0, 0, 0)
        bg_layout.setSpacing(0)
        self.title_bar = CustomTitleBar(self)
        bg_layout.addWidget(self.title_bar)

        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(15, 5, 15, 15)
        content_layout.setSpacing(15)

        # 侧边栏导航
        self.sidebar = QFrame()
        self.sidebar.setFixedWidth(180)
        self.sidebar.setStyleSheet(
            "background-color: white; border-radius: 10px; border: 1px solid #EAEAEA;"
        )
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(10, 20, 10, 20)
        sidebar_layout.setSpacing(10)

        self.nav_buttons = []

        def _make_nav_btn(icon_name, label, index):
            btn = self._icon_label(icon_name, label, icon_size=17, text_size=15,
                                   base_style=self.NAV_LBL_NORMAL,
                                   hover_style=self.NAV_LBL_HOVER)
            btn.setFixedHeight(45)
            btn.mousePressEvent = lambda e, i=index, b=btn: self.switch_page(i, b)
            sidebar_layout.addWidget(btn)
            self.nav_buttons.append(btn)
            return btn

        self.btn_notes = _make_nav_btn("description", "便签墙管理", 0)
        self.btn_settings = _make_nav_btn("settings", "系统与个性化", 1)
        self.btn_about = _make_nav_btn("info", "关于 AniNote", 2)
        sidebar_layout.addStretch()

        self.content_area = QStackedWidget()
        self.content_area.setStyleSheet(
            "background-color: white; border-radius: 10px; border: 1px solid #EAEAEA;"
        )

        self.init_notes_page()
        self.init_settings_page()
        self.init_about_page()

        content_layout.addWidget(self.sidebar)
        content_layout.addWidget(self.content_area)
        bg_layout.addLayout(content_layout)

        # 右下角窗口缩放
        size_grip_layout = QHBoxLayout()
        size_grip_layout.setContentsMargins(0, 0, 0, 0)
        size_grip_layout.addStretch()
        self.size_grip = QSizeGrip(self.bg_frame)
        self.size_grip.setStyleSheet("width: 15px; height: 15px; background: transparent;")
        size_grip_layout.addWidget(self.size_grip)
        bg_layout.addLayout(size_grip_layout)

        self.switch_page(0, self.btn_notes)

        # 搜索防抖定时器：300ms 无输入后才触发过滤
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.refresh_notes_wall)

    def _icon_label(self, name, label, icon_size=18, text_size=13,
                     base_style=None, hover_style=None):
        """创建一个带 Material Icon 的 QLabel（table 布局保证对齐）。
        自动注册悬停效果，由 eventFilter 处理。
        """
        html = (
            f'<table style="border:none;margin:0;padding:0;border-collapse:collapse;">'
            f'<tr>'
            f'<td style="vertical-align:middle;padding-top:1px;padding-right:2px;">'
            f'<span style="font-family:\'Material Symbols Outlined\';'
            f'font-size:{icon_size}px;">{icon(name)}</span></td>'
            f'<td style="vertical-align:middle;padding-top:1px;">'
            f'<span style="font-size:{text_size}px;">{label}</span></td>'
            f'</tr></table>'
        )
        lbl = QLabel(html)
        lbl.setCursor(Qt.PointingHandCursor)
        if base_style is None:
            base_style = "QLabel { padding: 2px 10px; color: #333; background: transparent; border-radius: 6px; }"
        if hover_style is None:
            hover_style = "QLabel { padding: 2px 10px; color: #333; background-color: rgba(0,0,0,0.06); border-radius: 6px; }"
        lbl.setProperty("hover_base_style", base_style)
        lbl.setProperty("hover_hover_style", hover_style)
        lbl.setStyleSheet(base_style)
        lbl.installEventFilter(self)
        if not hasattr(self, '_hover_labels'):
            self._hover_labels = set()
        self._hover_labels.add(lbl)
        return lbl

    def switch_page(self, index, active_btn):
        """切换右侧内容页面并高亮对应导航按钮。"""
        self.content_area.setCurrentIndex(index)
        for btn in self.nav_buttons:
            if isinstance(btn, QLabel):
                btn.setStyleSheet(self.NAV_LBL_NORMAL)
            else:
                btn.setStyleSheet(self.NAV_STYLE_NORMAL)
        if isinstance(active_btn, QLabel):
            active_btn.setStyleSheet(self.NAV_LBL_ACTIVE)
        else:
            active_btn.setStyleSheet(self.NAV_STYLE_ACTIVE)

    # ---------- 便签墙页面 ----------

    def init_notes_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)

        top_bar = QHBoxLayout()

        # 搜索框
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("输入关键字检索...")
        self.search_input.setFixedWidth(200)

        self.search_input.setStyleSheet("""
            QLineEdit { padding: 6px 10px; border: 1px solid #ccc; border-radius: 6px;
                        background-color: white; font-size: 13px; color: #333; }
            QLineEdit:focus { border: 1px solid #0078D7; background-color: #FCFCFC; }
        """)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        top_bar.addWidget(self.search_input)

        # 清空按钮
        clear_btn = QPushButton(icon("close"))
        clear_btn.setFixedSize(26, 26)
        clear_btn.setToolTip("清空搜索内容")
        set_icon_font(clear_btn, 18)
        clear_btn.setStyleSheet("""
            QPushButton { border: none; background: transparent; color: #999; }
            QPushButton:hover { color: #333; }
        """)
        clear_btn.clicked.connect(self.search_input.clear)
        top_bar.addWidget(clear_btn)

        # 刷新按钮
        refresh_btn = self._icon_label("refresh", "刷新", icon_size=18, text_size=13)
        refresh_btn.setFixedHeight(28)
        refresh_btn.setToolTip("同步最新便签修改")
        refresh_btn.mousePressEvent = lambda e: self.refresh_notes_wall()
        top_bar.addWidget(refresh_btn)

        top_bar.addStretch()

        # 事务追踪按钮
        habit_btn = self._icon_label("playlist_add", "新建事务追踪", icon_size=16, text_size=14,
                                      base_style="QLabel { padding: 8px 15px; color: #555; background-color: #E8E8E8; border-radius: 6px; }",
                                      hover_style="QLabel { padding: 8px 15px; color: #555; background-color: #D0D0D0; border-radius: 6px; }")
        habit_btn.mousePressEvent = lambda e: self.request_new_habit.emit()
        top_bar.addWidget(habit_btn)

        # 新建便签按钮
        new_btn = self._icon_label("note_add", "新建空白便签", icon_size=16, text_size=14,
                                    base_style="QLabel { padding: 8px 15px; color: white; background-color: #0078D7; border-radius: 6px; }",
                                    hover_style="QLabel { padding: 8px 15px; color: white; background-color: #005A9E; border-radius: 6px; }")
        new_btn.mousePressEvent = lambda e: self.request_new_note.emit()
        top_bar.addWidget(new_btn)
        layout.addLayout(top_bar)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_area.setStyleSheet("""
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }
            QScrollBar::handle:vertical { background: #DCDCDC; border-radius: 3px; min-height: 30px; }
            QScrollBar::handle:vertical:hover { background: #A9A9A9; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)

        self.flow_container = FlowWidget()
        self.refresh_notes_wall()
        scroll_area.setWidget(self.flow_container)
        layout.addWidget(scroll_area)
        self.content_area.addWidget(page)

    def _on_search_text_changed(self):
        """搜索框文本变更时启动防抖定时器，避免每次按键都触发完整刷新。"""
        self._search_timer.start()

    def eventFilter(self, obj, event):
        """为 QLabel 模拟按钮悬停效果（QLabel 不支持 :hover 样式）。"""
        from PySide6.QtCore import QEvent
        if hasattr(self, '_hover_labels') and obj in self._hover_labels:
            if event.type() == QEvent.Enter:
                obj.setStyleSheet(obj.property("hover_hover_style"))
            elif event.type() == QEvent.Leave:
                obj.setStyleSheet(obj.property("hover_base_style"))
        return super().eventFilter(obj, event)

    def refresh_notes_wall(self):
        """重新加载磁盘上的便签数据并刷新卡片墙。"""
        self.flow_container.clear_items()
        notes_data_list = self._load_notes_from_disk()
        if not notes_data_list:
            empty_label = QLabel("还没有任何便签，点击右上角新建吧！")
            empty_label.setStyleSheet("color: #999; font-size: 14px;")
            self.flow_container.add_item(empty_label)
            return

        kw = self.search_input.text().strip().lower() if hasattr(self, 'search_input') else ""
        visible_count = 0

        for note_info in notes_data_list:
            if kw and (kw not in note_info["title"].lower() and kw not in note_info["text"].lower()):
                continue

            card = NoteCard(note_info)
            card.clicked.connect(self.request_open_note.emit)
            card.delete_clicked.connect(self._handle_card_delete)
            card.set_top_clicked.connect(self.request_set_top.emit)
            card.export_clicked.connect(self.request_export_note.emit)
            card.set_hotkey_clicked.connect(self.request_set_note_hotkey.emit)
            self.flow_container.add_item(card)
            visible_count += 1

        if visible_count == 0 and kw:
            no_result_lbl = QLabel("未找到匹配的便签")
            no_result_lbl.setStyleSheet("color: #999; font-size: 14px; padding: 20px;")
            self.flow_container.add_item(no_result_lbl)

    def _handle_card_delete(self, note_id):
        self.request_delete_note.emit(note_id)

    @staticmethod
    def _load_notes_from_disk():
        """扫描存储目录，返回便签的摘要信息列表（文件夹模式）。"""
        data_list = []
        if os.path.exists(SAVE_DIR):
            for item in os.listdir(SAVE_DIR):
                item_path = os.path.join(SAVE_DIR, item)
                if not os.path.isdir(item_path):
                    continue
                data_file = os.path.join(item_path, "data.json")
                if not os.path.exists(data_file):
                    continue
                try:
                    with open(data_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    title = data.get("title", "未命名便签")
                    is_top = data.get("is_always_on_top", True)
                    doc = QTextDocument()
                    doc.setHtml(data.get("html_content", ""))
                    raw_text = doc.toPlainText().strip()
                    if not raw_text:
                        raw_text = "[空便签内容]"

                    # 事务追踪便签：html_content 只有占位图片/空勾选，
                    # 真正内容在 habits_data 中，生成统计摘要作为卡片预览。
                    habits_data = data.get("habits_data") or {}
                    habits = habits_data.get("habits") or []
                    if habits:
                        import datetime as _dt
                        today = _dt.date.today().isoformat()
                        # 每日打卡统计仅针对"自由打卡"模式（周期/倒计时不记每日记录）
                        free_habits = [h for h in habits if h.get("mode") == "free"]
                        if free_habits:
                            done = sum(1 for h in free_habits
                                       if (h.get("records") or {}).get(today))
                            line1 = f"{len(habits)} 个事务 · 今日 {done}/{len(free_habits)} 已打卡"
                        else:
                            line1 = f"{len(habits)} 个事务 · 周期/倒计时"
                        names = " · ".join(h.get("name", "?") for h in habits)
                        raw_text = line1 + "\n" + names

                    bg_color = data.get("bg_color", [255, 249, 196, 242])
                    data_list.append({
                        "id": data.get("note_id", ""),
                        "title": title,
                        "text": raw_text,
                        "is_top": is_top,
                        "bg_color": bg_color,
                        "note_hotkey": data.get("note_hotkey", ""),
                    })
                except Exception:
                    pass
        return data_list

    # ---------- 设置页面 ----------

    def init_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("""
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }
            QScrollBar::handle:vertical { background: #DCDCDC; border-radius: 3px; min-height: 30px; }
            QScrollBar::handle:vertical:hover { background: #A9A9A9; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)

        inner_widget = QWidget()
        inner_widget.setStyleSheet("background-color: transparent;")
        inner_layout = QVBoxLayout(inner_widget)
        inner_layout.setContentsMargins(30, 20, 30, 30)

        form_layout = QFormLayout()
        form_layout.setVerticalSpacing(25)
        form_layout.setLabelAlignment(Qt.AlignLeft)

        cfg = load_config()

        # 存储目录
        display_dir = cfg.get("save_dir", "default")
        if display_dir == "default":
            display_dir = os.path.abspath(SAVE_DIR)

        self.path_input = QLineEdit(display_dir)
        self.path_input.setReadOnly(True)
        self.path_input.setStyleSheet(
            "padding: 8px; border: 1px solid #ccc; border-radius: 5px;"
            " background-color: #f9f9f9;"
        )
        browse_btn = QPushButton("更改目录")
        browse_btn.setStyleSheet("padding: 8px 12px; background-color: #eee; border-radius: 5px;")
        browse_btn.clicked.connect(self._browse_directory)

        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(browse_btn)

        # 导出目录
        from main import EXPORT_DIR as _export_dir
        export_display = cfg.get("export_dir", "default")
        if export_display == "default" or not export_display:
            export_display = os.path.abspath(_export_dir)

        self.export_path_input = QLineEdit(export_display)
        self.export_path_input.setReadOnly(True)
        self.export_path_input.setStyleSheet(
            "padding: 8px; border: 1px solid #ccc; border-radius: 5px;"
            " background-color: #f9f9f9;"
        )
        export_browse_btn = QPushButton("更改目录")
        export_browse_btn.setStyleSheet("padding: 8px 12px; background-color: #eee; border-radius: 5px;")
        export_browse_btn.clicked.connect(self._browse_export_directory)

        export_path_layout = QHBoxLayout()
        export_path_layout.addWidget(self.export_path_input)
        export_path_layout.addWidget(export_browse_btn)

        # ComboBox 共享样式：统一简约白底蓝调
        dropdown_style = """
            QComboBox {
                padding: 8px 12px;
                border: 1px solid #D0D0D0;
                border-radius: 6px;
                background-color: #FFFFFF;
                color: #333333;
                font-size: 13px;
            }
            QComboBox:hover {
                border: 1px solid #0078D7;
            }
            QComboBox:focus {
                border: 1px solid #0078D7;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 26px;
                border-left: 1px solid #E8E8E8;
                border-top-right-radius: 6px;
                border-bottom-right-radius: 6px;
            }
            QComboBox::down-arrow {
                width: 0;
                height: 0;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #999999;
                margin-right: 8px;
            }
            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                color: #333333;
                selection-background-color: transparent;
                border: 1px solid #D0D0D0;
                border-radius: 6px;
                padding: 4px;
                outline: none;
            }
            QComboBox QAbstractItemView::item {
                padding: 8px 12px;
                border-radius: 4px;
                min-height: 20px;
            }
            QComboBox QAbstractItemView::item:hover {
                background-color: #F0F4F8;
            }
            QComboBox QAbstractItemView::item:selected {
                background-color: #E8F4FD;
                color: #0078D7;
            }
        """

        # 皮肤选择
        self.skin_combo = QComboBox()
        self.skin_combo.addItems(["极简模式", "AniMode（功能暂时无效）"])
        self.skin_combo.setCurrentText(cfg["skin"])
        self.skin_combo.wheelEvent = lambda event: event.ignore()
        self.skin_combo.setStyleSheet(dropdown_style + "QComboBox { min-width: 250px; }")

        # 字体选择
        self.font_combo = QFontComboBox()
        self.font_combo.setStyleSheet(dropdown_style)
        self.font_combo.setCurrentFont(QFont(cfg["font_family"]))
        self.font_combo.wheelEvent = lambda event: event.ignore()

        # 快捷键输入
        self.hotkey_input = QLineEdit(cfg["toggle_hotkey"])
        self.hotkey_input.setStyleSheet(
            "padding: 8px; border: 1px solid #ccc; border-radius: 5px;"
            " font-weight: bold; color: #0078D7;"
        )
        self.new_hotkey_input = QLineEdit(cfg["new_hotkey"])
        self.new_hotkey_input.setStyleSheet(
            "padding: 8px; border: 1px solid #ccc; border-radius: 5px;"
            " font-weight: bold; color: #28a745;"
        )
        self.show_all_hotkey_input = QLineEdit(cfg.get("show_all_hotkey", "alt+shift+n"))
        self.show_all_hotkey_input.setStyleSheet(
            "padding: 8px; border: 1px solid #ccc; border-radius: 5px;"
            " font-weight: bold; color: #e67e22;"
        )
        self.disable_all_hotkey_input = QLineEdit(cfg.get("disable_all_hotkey", "ctrl+shift+a"))
        self.disable_all_hotkey_input.setStyleSheet(
            "padding: 8px; border: 1px solid #ccc; border-radius: 5px;"
            " font-weight: bold; color: #dc3545;" 
        )
        self.panel_hotkey_input = QLineEdit(cfg.get("panel_hotkey", "alt+c"))
        self.panel_hotkey_input.setStyleSheet(
            "padding: 8px; border: 1px solid #ccc; border-radius: 5px;"
            " font-weight: bold; color: #17a2b8;"
        )

        # ---------- Bangumi 设置 ----------

        # 新番开关
        self.bangumi_checkbox = ToggleSwitch("开启新番信息便签")
        self.bangumi_checkbox.setChecked(cfg.get("enable_bangumi", False))
        cf = self.bangumi_checkbox.font()
        cf.setPixelSize(14)
        self.bangumi_checkbox.setFont(cf)
        bangumi_wrapper = QWidget()
        bangumi_wrap_layout = QVBoxLayout(bangumi_wrapper)
        bangumi_wrap_layout.setContentsMargins(0, 5, 0, 5)
        bangumi_wrap_layout.addWidget(self.bangumi_checkbox)

        # Bangumi 授权（OAuth 一键授权，内置应用凭证；UID 由授权自动获取）
        _oauth = bgm_oauth.load_oauth(cfg)
        bangumi_auth_layout = QVBoxLayout()
        bangumi_auth_layout.setContentsMargins(0, 0, 0, 0)
        bangumi_auth_layout.setSpacing(6)

        oauth_row = QHBoxLayout()
        oauth_row.setSpacing(6)
        self.auth_btn = QPushButton("授权 Bangumi")
        self.auth_btn.setStyleSheet(
            "QPushButton { padding: 7px 14px; border: none; border-radius: 6px;"
            " background: #0078D7; color: white; font-weight: bold; }"
            " QPushButton:hover { background: #005BA1; }"
            " QPushButton:disabled { background: #A0C8E8; }"
        )
        self.auth_btn.clicked.connect(self._start_bangumi_oauth)
        self.cancel_auth_btn = QPushButton("取消授权")
        self.cancel_auth_btn.setStyleSheet(
            "QPushButton { padding: 7px 14px; border: 1px solid #D0D0D0; border-radius: 6px;"
            " background: white; color: #666; }"
            " QPushButton:hover { background: #F5F5F5; }"
            " QPushButton:disabled { color: #BBB; background: #F8F8F8; }"
        )
        self.cancel_auth_btn.clicked.connect(self._cancel_bangumi_oauth)
        self.cancel_auth_btn.setEnabled(False)
        self.disconnect_btn = QPushButton("断开授权")
        self.disconnect_btn.setStyleSheet(
            "QPushButton { padding: 7px 14px; border: 1px solid #D0D0D0; border-radius: 6px;"
            " background: white; color: #666; }"
            " QPushButton:hover { background: #F5F5F5; }"
        )
        self.disconnect_btn.clicked.connect(self._disconnect_bangumi_oauth)
        oauth_row.addWidget(self.auth_btn)
        oauth_row.addWidget(self.cancel_auth_btn)
        oauth_row.addWidget(self.disconnect_btn)
        oauth_row.addStretch()
        bangumi_auth_layout.addLayout(oauth_row)

        self.oauth_status = QLabel()
        self.oauth_status.setWordWrap(True)
        self.oauth_status.setStyleSheet("color: #777; font-size: 12px;")
        self._refresh_oauth_status(_oauth)
        bangumi_auth_layout.addWidget(self.oauth_status)

        # 代理设置
        proxy_layout = QVBoxLayout()
        proxy_layout.setContentsMargins(0, 0, 0, 0)
        proxy_layout.setSpacing(5)

        self.proxy_input = QLineEdit(cfg.get("api_proxy", ""))
        self.proxy_input.setPlaceholderText("例如: 127.0.0.1:7890 (国内网络请留空)")
        self.proxy_input.setStyleSheet("padding: 8px; border: 1px solid #ccc; border-radius: 5px;")

        proxy_hint = QLabel(
            "💡 新番同步小贴士：\n"
            "如果无法获取新番数据，请确保代理软件在后台运行。\n"
            "无需开启系统代理，只需在此填入对应的本地端口即可静默同步。"
        )
        proxy_hint.setStyleSheet("color: #777; font-size: 12px; line-height: 1.3;")
        proxy_hint.setWordWrap(True)

        proxy_layout.addWidget(self.proxy_input)
        proxy_layout.addWidget(proxy_hint)

        # 开机自启
        self.autostart_checkbox = ToggleSwitch("开机时自动在后台静默启动")
        self.autostart_checkbox.setChecked(cfg["autostart"])
        cf2 = self.autostart_checkbox.font()
        cf2.setPixelSize(14)
        self.autostart_checkbox.setFont(cf2)
        autostart_wrapper = QWidget()
        autostart_wrap_layout = QVBoxLayout(autostart_wrapper)
        autostart_wrap_layout.setContentsMargins(0, 5, 0, 5)
        autostart_wrap_layout.addWidget(self.autostart_checkbox)

        # 自动更新
        self.auto_update_checkbox = ToggleSwitch("启动时自动检查更新")
        self.auto_update_checkbox.setChecked(cfg.get("auto_update", True))
        cf3 = self.auto_update_checkbox.font()
        cf3.setPixelSize(14)
        self.auto_update_checkbox.setFont(cf3)
        auto_update_wrapper = QWidget()
        auto_update_wrap_layout = QVBoxLayout(auto_update_wrapper)
        auto_update_wrap_layout.setContentsMargins(0, 5, 0, 5)
        auto_update_wrap_layout.addWidget(self.auto_update_checkbox)

        # 立即检查更新按钮
        update_btn = QPushButton("立即检查更新")
        update_btn.setStyleSheet(
            "QPushButton { padding: 6px 16px; border: 1px solid #D0D0D0;"
            " border-radius: 6px; background: #FFFFFF; font-size: 13px; color: #555; }"
            " QPushButton:hover { background: #F0F0F0; border-color: #B0B0B0; }"
        )
        update_btn.clicked.connect(self.request_check_update.emit)
        update_btn_layout = QHBoxLayout()
        update_btn_layout.addWidget(auto_update_wrapper)
        update_btn_layout.addWidget(update_btn)
        update_btn_layout.addStretch()

        # 装配表单
        def _lbl(text):
            lbl = QLabel(text)
            f = lbl.font()
            f.setPixelSize(14)
            lbl.setFont(f)
            lbl.setStyleSheet("color: #333;")
            return lbl

        form_layout.addRow(_lbl("<b>数据存储目录：</b>"), path_layout)
        form_layout.addRow(_lbl("<b>文档导出目录：</b>"), export_path_layout)
        form_layout.addRow(_lbl("<b>全局便签皮肤：</b>"), self.skin_combo)
        form_layout.addRow(_lbl("<b>便签默认字体：</b>"), self.font_combo)
        form_layout.addRow(_lbl("<b>显示/隐藏全局快捷键：</b>"), self.hotkey_input)
        form_layout.addRow(_lbl("<b>新建便签全局快捷键：</b>"), self.new_hotkey_input)
        form_layout.addRow(_lbl("<b>显示全部便签快捷键：</b>"), self.show_all_hotkey_input)
        form_layout.addRow(_lbl("<b>临时禁用/恢复全部快捷键：</b>"), self.disable_all_hotkey_input)
        form_layout.addRow(_lbl("<b>呼出控制台快捷键：</b>"), self.panel_hotkey_input)
        form_layout.addRow(_lbl("<b>API 代理地址：</b>"), proxy_layout)
        form_layout.addRow(_lbl("<b>新番追踪功能：</b>"), bangumi_wrapper)
        form_layout.addRow(_lbl("<b>Bangumi 授权：</b>"), bangumi_auth_layout)
        form_layout.addRow(_lbl("<b>系统后台行为：</b>"), autostart_wrapper)
        form_layout.addRow(_lbl("<b>自动更新：</b>"), update_btn_layout)

        inner_layout.addLayout(form_layout)
        inner_layout.addStretch()

        save_btn = QPushButton("保存全部设置")
        save_btn.setStyleSheet(
            "QPushButton { background-color: #28a745; color: white; padding: 10px;"
            " border-radius: 8px; font-weight: bold; font-size: 15px; }"
            " QPushButton:hover { background-color: #218838; }"
        )
        save_btn.clicked.connect(self._save_settings)
        inner_layout.addWidget(save_btn, alignment=Qt.AlignRight)

        scroll_area.setWidget(inner_widget)
        layout.addWidget(scroll_area)
        self.content_area.addWidget(page)

        self.oauth_done_signal.connect(self._on_oauth_done)

    # ---------- Bangumi OAuth 授权 ----------

    def _refresh_oauth_status(self, oauth=None):
        """刷新授权状态标签与按钮。"""
        oauth = oauth if oauth is not None else bgm_oauth.load_oauth(load_config())
        if oauth.get("access_token"):
            uid = oauth.get("user_id", "")
            self.oauth_status.setText(f"✅ 已授权" + (f"（用户 ID: {uid}）" if uid else ""))
            self.oauth_status.setStyleSheet("color: #2e7d32; font-size: 12px;")
            self.disconnect_btn.setEnabled(True)
        else:
            self.oauth_status.setText("未授权：点击「授权 Bangumi」一键授权，之后自动同步追番与观看进度")
            self.oauth_status.setStyleSheet("color: #777; font-size: 12px;")
            self.disconnect_btn.setEnabled(False)

    def _start_bangumi_oauth(self):
        if self._oauth_busy:
            return
        # 使用内置应用凭证一键授权（不开放自定义）
        cid, sec = bgm_oauth.DEFAULT_CLIENT_ID, bgm_oauth.DEFAULT_CLIENT_SECRET

        self._oauth_busy = True
        self.auth_btn.setEnabled(False)
        self.cancel_auth_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.oauth_status.setText("⏳ 等待授权中...请在浏览器中完成授权（可点「取消授权」重试）")
        self.oauth_status.setStyleSheet("color: #e67e22; font-size: 12px;")
        self._oauth_cancel_event = threading.Event()
        threading.Thread(
            target=self._oauth_worker, args=(cid, sec, self._oauth_cancel_event), daemon=True
        ).start()
        webbrowser.open(bgm_oauth.build_authorize_url(cid))

    def _cancel_bangumi_oauth(self):
        if self._oauth_cancel_event is not None:
            self._oauth_cancel_event.set()

    def _oauth_worker(self, cid, sec, cancel_event):
        proxy = load_config().get("api_proxy", "")
        code = bgm_oauth.start_callback_server(timeout=60, cancel_event=cancel_event)
        if not code:
            self.oauth_done_signal.emit(False, "授权已取消或超时，请重试")
            return
        try:
            data = bgm_oauth.exchange_code(code, cid, sec, proxy)
            cfg = load_config()
            oauth = bgm_oauth.load_oauth(cfg)
            oauth["access_token"] = data.get("access_token", "")
            oauth["refresh_token"] = data.get("refresh_token", "")
            oauth["expires_at"] = int(time.time()) + int(data.get("expires_in", 604800))
            oauth["user_id"] = str(data.get("user_id", ""))
            cfg["bangumi_oauth"] = oauth
            save_config(cfg)
            self.oauth_done_signal.emit(True, "授权成功，观看进度将自动同步")
        except Exception as e:
            self.oauth_done_signal.emit(False, f"换取令牌失败: {type(e).__name__}: {e}")

    def _on_oauth_done(self, ok, msg):
        self._oauth_busy = False
        self.auth_btn.setEnabled(True)
        self.cancel_auth_btn.setEnabled(False)
        self._refresh_oauth_status()
        if ok:
            # 授权成功：UID 自动写入配置，无需手动填写
            cfg = load_config()
            uid = bgm_oauth.load_oauth(cfg).get("user_id", "")
            if uid:
                cfg["bangumi_uid"] = str(uid)
                save_config(cfg)
        QMessageBox.information(self, "Bangumi 授权", msg)
        # 授权弹窗关闭后再同步，避免新番便签盖住模态提示框
        if ok and cfg.get("enable_bangumi", False):
            try:
                from main import global_signaler
                QTimer.singleShot(800, lambda: global_signaler.force_sync_bangumi_signal.emit())
            except Exception:
                pass

    def _disconnect_bangumi_oauth(self):
        if self._oauth_busy:
            QMessageBox.information(self, "Bangumi 授权", "授权进行中，请先取消")
            return
        cfg = load_config()
        oauth = bgm_oauth.load_oauth(cfg)
        oauth.pop("access_token", None)
        oauth.pop("refresh_token", None)
        oauth.pop("expires_at", None)
        oauth.pop("user_id", None)
        cfg["bangumi_oauth"] = oauth
        save_config(cfg)
        self._refresh_oauth_status()
        QMessageBox.information(self, "Bangumi 授权", "已断开授权")

    def _browse_directory(self):
        """弹出文件夹选择对话框。"""
        new_dir = QFileDialog.getExistingDirectory(
            self, "选择新的数据存储目录", self.path_input.text()
        )
        if new_dir:
            self.path_input.setText(os.path.abspath(new_dir))

    def _browse_export_directory(self):
        """弹出导出目录选择对话框。"""
        new_dir = QFileDialog.getExistingDirectory(
            self, "选择文档导出目录", self.export_path_input.text()
        )
        if new_dir:
            self.export_path_input.setText(os.path.abspath(new_dir))

    def _save_settings(self):
        """收集表单数据，保存配置并发射设置变更信号。"""
        from main import BASE_DIR

        if self.sender():
            self.sender().clearFocus()

        old_cfg = load_config()
        new_dir = os.path.abspath(self.path_input.text())
        default_dir = os.path.abspath(os.path.join(BASE_DIR, "notes_data"))

        # 若用户选择的恰好是程序同级目录，记为 "default" 以保持便携性
        final_save_dir = "default" if new_dir == default_dir else new_dir

        new_export_dir = os.path.abspath(self.export_path_input.text())
        default_export_dir = os.path.abspath(os.path.join(BASE_DIR, "导出的便签文本"))
        final_export_dir = "default" if new_export_dir == default_export_dir else new_export_dir

        cfg = {
            "skin": self.skin_combo.currentText(),
            "font_family": self.font_combo.currentFont().family(),
            "toggle_hotkey": self.hotkey_input.text(),
            "new_hotkey": self.new_hotkey_input.text(),
            "show_all_hotkey": self.show_all_hotkey_input.text(),
            "disable_all_hotkey": self.disable_all_hotkey_input.text().strip(),
            "panel_hotkey": self.panel_hotkey_input.text().strip(),
            "autostart": self.autostart_checkbox.isChecked(),
            "enable_bangumi": self.bangumi_checkbox.isChecked(),
            "api_proxy": self.proxy_input.text().strip(),
            "save_dir": final_save_dir,
            "export_dir": final_export_dir,
            "auto_update": self.auto_update_checkbox.isChecked(),
            "ignored_version": old_cfg.get("ignored_version", ""),
            "is_first_run": old_cfg.get("is_first_run", False),
            "bangumi_uid": old_cfg.get("bangumi_uid", ""),
            "bangumi_oauth": old_cfg.get("bangumi_oauth", {}),
        }
        save_config(cfg)
        self.settings_changed.emit(cfg)

        old_save_dir = old_cfg.get("save_dir", os.path.abspath(SAVE_DIR))
        if new_dir != old_save_dir:
            # 非模态：避免阻塞新番便签同步等下一步交互
            self._settings_toast = QMessageBox(
                QMessageBox.Warning, "需要重启",
                "设置已保存，快捷键已实时生效！\n\n"
                "【注意】\n若你更改了数据存储目录，"
                "请手动将旧文件迁移至新目录，"
                "并重启本程序以使其完全生效。",
                QMessageBox.Ok, self
            )
            self._settings_toast.setWindowModality(Qt.NonModal)
            self._settings_toast.show()
        else:
            # 非模态提示：不阻塞交互（避免同步弹出新番便签时被弹窗卡住）
            self._settings_toast = QMessageBox(
                QMessageBox.Information, "成功",
                "设置已保存，快捷键已实时生效！",
                QMessageBox.Ok, self
            )
            self._settings_toast.setWindowModality(Qt.NonModal)
            self._settings_toast.show()

    # ---------- 关于页面 ----------

    def init_about_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        from main import VERSION

        info = QLabel(
            f"<b>AniNote v{VERSION}</b><br><br>"
            f"本软件基于Creator自身需求制作而成<br>"
            f"作者本人并无编程能力和经历, 因此完全由AI辅助创作, 如有问题还请见谅<br>"
            f"欢迎私信反馈bug<br>"
            f"Made By B站@HunterHasCome"
        )
        info.setAlignment(Qt.AlignCenter)
        info.setStyleSheet("font-size: 16px; color: #555; line-height: 1.5;")
        layout.addWidget(info)
        self.content_area.addWidget(page)
