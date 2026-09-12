"""便签集（便签夹）：多个折叠便签吸附成一条，带可拖动的夹子头部。

设计要点：
- **虚拟分组**：不新增便签数据，只在成员各自的 data.json 里记 `stack_id`（归属）
  与 `stack_pos`（顺序，0 为最上面那条）。成员仍是各自独立的窗口，宽度 / 底色 /
  透明度都保持自己的。
- **便签夹头部**（`StackHeader`）：独立的小窗口，宽度与颜色取整叠最上面那条
  （离它最近），可拖动 = 移动整叠。
- **排布规则**（`layout`）：以"刚变化的那条"为锚点（锚点原位不动），其余成员
  按顺序向上 / 向下紧贴排布 —— 中间那条展开时，下方成员下移、上方不动。
- **吸附**：折叠条拖动松手时判定，以目标（未被拖动的那个）为基准对齐。
- **悬停抽出**：折叠条 hover 时临时增高一行，显示缩略预览（`AniNoteWindow.set_peek`）。

本模块刻意不 import main（避免循环依赖），对便签对象全部走鸭子类型；
所有对外方法都包了 try/except —— 便签集出问题只影响自身，绝不能拖累普通便签。
"""
import sys
import uuid

from PySide6.QtCore import Qt, QObject, QTimer, QPoint, QEvent
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QLineEdit, QHBoxLayout,
                               QVBoxLayout, QMenu, QGraphicsDropShadowEffect)

from icons import icon, set_icon_font

SNAP_GAP = 26.0          # 吸附判定：纵向边缘间距 ≤ 该值即吸附
SNAP_H_OVERLAP = 0.30    # 吸附判定：横向重叠至少占较窄者的比例
DETACH_SLACK = 46        # 拖离整叠超过该距离 → 拆出
HEADER_H = 30            # 便签夹头部高度
HEADER_GAP = 2           # 头部与第一条之间的缝隙
PEEK_H = 18              # 悬停抽出时多露出的高度（含一行预览文字）
STACK_GAP = 0            # 成员之间的纵向缝隙
MIN_MEMBERS = 2          # 少于该数量自动解散
REBUILD_DELAY = 260      # 批量加载后的重建延迟（ms）
DEFAULT_STACK_NAME = "便签夹"
DRAG_THRESHOLD = 4       # 拖动阈值（px）：小于它只当点击，避免双击重命名时误拖整叠

# 夹子头部样式。便签底色只留一点影子做淡彩，主体仍是浅色卡片，避免"一整条色块"的土气感
HEADER_NAME_QSS = (
    "QLineEdit { border: none; background: transparent; color: #3C4043;"
    " font-family: 'Microsoft YaHei'; font-size: 12px; font-weight: 600;"
    " padding: 0 2px; }"
)
HEADER_NAME_EDIT_QSS = (
    "QLineEdit { border: none; border-bottom: 1px solid #1A73E8;"
    " background: rgba(255, 255, 255, 0.75); color: #202124;"
    " font-family: 'Microsoft YaHei'; font-size: 12px; font-weight: 600;"
    " padding: 0 2px; }"
)
HEADER_COUNT_QSS = (
    "background: transparent; color: #9AA0A6; font-size: 11px;"
    " font-family: 'Microsoft YaHei';"
)
MENU_QSS = (
    "QMenu { background-color: #FAFAFA; border: 1px solid #E0E0E0;"
    " border-radius: 10px; padding: 6px; }"
    " QMenu::item { padding: 7px 24px; border-radius: 6px; margin: 1px 3px;"
    " color: #333333; font-size: 13px; }"
    " QMenu::item:selected { background-color: #E8F0FE; color: #1A73E8; }"
    " QMenu::separator { height: 1px; background: #E8E8E8; margin: 4px 12px; }"
)


def _log(msg):
    try:
        print(f"[AniNote][便签集] {msg}", file=sys.stderr)
    except Exception:
        pass


def _screen_available(x, y, w, h):
    """取便签夹所在那块屏幕的可用区域（拿不到就返回 None）。"""
    try:
        scr = QGuiApplication.screenAt(QPoint(int(x + w // 2), int(y + h // 2)))
        if scr is None:
            scr = QGuiApplication.primaryScreen()
        return scr.availableGeometry() if scr is not None else None
    except Exception:
        return None


def _clamp_to_screen(x, y, w, h):
    """把矩形拉回屏幕可用区内 —— 保证便签夹永远留在屏幕上、始终能被拖回来。"""
    g = _screen_available(x, y, w, h)
    if g is None:
        return int(x), int(y)
    try:
        lo_x, hi_x = g.left(), g.right() - int(w) + 1
        lo_y, hi_y = g.top(), g.bottom() - int(h) + 1
        if hi_x < lo_x:
            hi_x = lo_x
        if hi_y < lo_y:
            hi_y = lo_y
        return max(lo_x, min(int(x), hi_x)), max(lo_y, min(int(y), hi_y))
    except Exception:
        return int(x), int(y)


def _visible(note):
    """该成员当前是否参与排布（隐藏的便签不占位置）。"""
    try:
        return bool(note.isVisible()) and not getattr(note, 'is_hidden', False)
    except RuntimeError:
        return False


def _alive(note):
    """成员窗口是否还活着（窗口已销毁的成员直接剔除）。"""
    try:
        note.width()
        return True
    except RuntimeError:
        return False


class StackHeader(QWidget):
    """便签夹头部：显示名称与条数，拖动它移动整叠，双击可重命名。

    样式走 AniNote 的浅色卡片风：便签底色只按 18% 掺进浅灰底做淡彩，
    另加一条细边框和加深后的图标点缀 —— 不再是一整条纯色块。
    数量挪到右侧、用灰色小字，视觉重心留给名称。
    """

    def __init__(self, manager, stack):
        super().__init__(None)
        self.manager = manager
        self.stack = stack
        self._drag_pos = None        # 按下时"鼠标 - 窗口左上角"的偏移
        self._press_global = None    # 按下时的全局坐标（判拖动阈值用）
        self._dragged = False
        self._renaming = False
        self._hovered = False

        # 置顶跟随便签：不能硬编码 WindowStaysOnTopHint，否则便签取消置顶后
        # 整叠沉下去了、只剩夹子还浮在所有窗口最上面
        self._always_on_top = self._stack_on_top()
        self.setWindowFlags(self._flags_for(self._always_on_top))
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_QuitOnClose, False)   # 夹子窗口不参与"关窗即退出"
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("拖动可移动整叠便签 · 双击标题重命名 · 右键更多")
        self.setFixedHeight(HEADER_H)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.bg = QFrame(self)
        self.bg.setObjectName("stack_header_bg")
        outer.addWidget(self.bg)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(10)
        shadow.setColor(QColor(0, 0, 0, 34))
        shadow.setOffset(0, 2)
        self.bg.setGraphicsEffect(shadow)

        row = QHBoxLayout(self.bg)
        row.setContentsMargins(9, 0, 9, 0)
        row.setSpacing(6)

        self.icon_lbl = QLabel(icon("folder"))
        set_icon_font(self.icon_lbl, 13)
        row.addWidget(self.icon_lbl)

        self.name_edit = QLineEdit(self.bg)
        self.name_edit.setText(stack.name)
        self.name_edit.setReadOnly(True)
        self.name_edit.setFrame(False)
        self.name_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.name_edit.setStyleSheet(HEADER_NAME_QSS)
        self.name_edit.installEventFilter(self)
        self.name_edit.returnPressed.connect(self._commit_rename)
        self.name_edit.editingFinished.connect(self._commit_rename)
        row.addWidget(self.name_edit, 1)

        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet(HEADER_COUNT_QSS)
        row.addWidget(self.count_lbl)
        self._apply_bg()

    # ---------- 置顶（跟随便签）----------

    @staticmethod
    def _flags_for(on_top):
        """窗口标志：Qt.Tool 隐藏任务栏图标；置顶与否与便签保持一致。"""
        flags = Qt.Tool | Qt.FramelessWindowHint
        if on_top:
            flags |= Qt.WindowStaysOnTopHint
        return flags

    def _stack_on_top(self):
        """夹子该不该置顶：取整叠顺序 0（最上面那条）便签的设置 —— 颜色 / 宽度也是取它。

        用 alive_members 而非 visible_members：切换置顶时便签会 hide → 改 flag → show，
        若按"当前可见成员"判断，会在这中间读到隔壁那条的值、来回翻标志。
        """
        try:
            members = self.stack.alive_members()
            if members:
                return bool(getattr(members[0], 'is_always_on_top', True))
        except Exception:
            pass
        return True

    def set_always_on_top(self, on):
        """改窗口标志。Qt 规定 setWindowFlags 会让窗口隐藏，必须自己再 show 回来。"""
        on = bool(on)
        if self._always_on_top == on:
            return
        self._always_on_top = on
        try:
            was_visible = self.isVisible()
            self.setWindowFlags(self._flags_for(on))
            if was_visible:
                self.show()
        except RuntimeError:
            pass

    def sync_top(self):
        """按整叠第一条便签的置顶设置刷新自己。"""
        self.set_always_on_top(self._stack_on_top())

    def always_on_top(self):
        """当前是否置顶（排布时决定要不要 raise）。"""
        return bool(self._always_on_top)

    # ---------- 外观 ----------

    @staticmethod
    def _mix(r, g, b, keep=0.18):
        """便签底色按 keep 比例掺进浅灰底 → 克制的淡彩。"""
        base = (246, 247, 249)
        return [max(0, min(255, int(base[i] * (1 - keep) + (r, g, b)[i] * keep)))
                for i in range(3)]

    @staticmethod
    def _accent(r, g, b):
        """图标点缀色：底色压暗，保证在浅底上看得清。"""
        return "#%02X%02X%02X" % tuple(
            max(72, min(150, int(v * 0.62))) for v in (r, g, b))

    def _apply_bg(self):
        r, g, b, a = 236, 238, 242, 240
        color = getattr(self.stack, "header_color", None)
        if isinstance(color, (list, tuple)) and len(color) == 4:
            r, g, b, a = color
        try:
            alpha = float(a) / 255.0 if float(a) > 1 else float(a)
        except (TypeError, ValueError):
            alpha = 0.94
        mr, mg, mb = self._mix(r, g, b)
        border = "#D6DAE0" if self._hovered else "#E4E7EC"
        self.bg.setStyleSheet(
            "QFrame#stack_header_bg {"
            f" background-color: rgba({mr}, {mg}, {mb}, {alpha:.2f});"
            f" border: 1px solid {border}; border-radius: 10px; }}"
        )
        self.icon_lbl.setStyleSheet(
            "background: transparent; border: none;"
            f" color: {self._accent(r, g, b)};"
        )

    def refresh(self, color=None, count=None):
        """同步夹子的颜色、条数与置顶状态（颜色取整叠最上面那条便签）。

        颜色 / 条数 / 名称没变就不重设 —— 折叠展开动画期间这里会被高频调用。
        """
        self.sync_top()          # 便签改了置顶，夹子要跟着变
        if isinstance(color, (list, tuple)) and len(color) == 4:
            new_color = [int(v) for v in color]
            if new_color != self.stack.header_color:
                self.stack.header_color = new_color
                self._apply_bg()
        elif self.stack.header_color is None:
            self._apply_bg()
        if count is not None:
            text = f"{int(count)} 条"
            if self.count_lbl.text() != text:
                self.count_lbl.setText(text)
        if not self._renaming and self.name_edit.text() != self.stack.name:
            self.name_edit.setText(self.stack.name)

    def enterEvent(self, event):
        super().enterEvent(event)
        if not self._hovered:
            self._hovered = True
            self._apply_bg()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if self._hovered and not self._renaming:
            self._hovered = False
            self._apply_bg()

    # ---------- 定位（永远留在屏幕内）----------

    def place(self, x, y, width):
        """定位 + 定宽（宽度依据最近的成员），并把夹子夹在屏幕可用区内。"""
        self.setFixedWidth(max(int(width), 120))
        x, y = _clamp_to_screen(int(x), int(y), self.width(), self.height())
        if (self.x(), self.y()) != (x, y):
            self.move(x, y)

    def clamp_into_screen(self):
        """把夹子拉回屏幕内（成员不动）—— 保证它永远可见、可拖动。"""
        x, y = _clamp_to_screen(self.x(), self.y(), self.width(), self.height())
        if (self.x(), self.y()) != (x, y):
            self.move(x, y)

    # ---------- 重命名 ----------

    def begin_rename(self):
        """进入重命名态（双击标题或右键菜单进入）。"""
        if self._renaming:
            return
        self._renaming = True
        self.name_edit.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.name_edit.setReadOnly(False)
        self.name_edit.setStyleSheet(HEADER_NAME_EDIT_QSS)
        self.name_edit.setCursor(Qt.IBeamCursor)
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _cancel_rename(self):
        """Esc：回填原名再走提交流程（名字没变 → 不会落盘）。"""
        self.name_edit.setText(self.stack.name)
        self._commit_rename()

    def _commit_rename(self):
        """提交重命名（回车 / 失焦触发）。"""
        if not self._renaming:
            return
        self._renaming = False
        name = self.name_edit.text().strip() or DEFAULT_STACK_NAME
        self.name_edit.setReadOnly(True)
        self.name_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.name_edit.setStyleSheet(HEADER_NAME_QSS)
        self.name_edit.setCursor(Qt.ArrowCursor)
        self.name_edit.deselect()
        self.name_edit.setText(name)
        if name != self.stack.name:
            self.manager.set_name(self.stack, name)

    def eventFilter(self, obj, event):
        if obj is self.name_edit and self._renaming:
            if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self._cancel_rename()
                return True
            if event.type() == QEvent.FocusOut:
                self._commit_rename()
                return False
        return super().eventFilter(obj, event)

    # ---------- 拖动整叠 ----------

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self._renaming:
            return
        self._press_global = event.globalPosition().toPoint()
        self._drag_pos = self._press_global - self.pos()
        self._dragged = False
        self.setCursor(Qt.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is None or self._renaming:
            return
        gpos = event.globalPosition().toPoint()
        if not self._dragged:
            # 没过阈值就按"点击"处理 —— 否则双击重命名的第一下会顺手把整叠拖走
            if (gpos - self._press_global).manhattanLength() < DRAG_THRESHOLD:
                return
            self._dragged = True
        delta = (gpos - self._drag_pos) - self.pos()
        if delta.x() or delta.y():
            self.manager.move_stack(self.stack, delta.x(), delta.y())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_pos is None:
            return
        dragged = self._dragged
        self._drag_pos = None
        self._press_global = None
        self._dragged = False
        self.setCursor(Qt.OpenHandCursor)
        if dragged:
            self.manager.save_positions(self.stack)
            self.manager.clamp_header(self.stack)   # 被拖出屏幕也能自己回来
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = None
            self._press_global = None
            self._dragged = False
            self.begin_rename()
            event.accept()

    def contextMenuEvent(self, event):
        if self._renaming:
            return
        menu = QMenu(self)
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint
                            | Qt.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WA_TranslucentBackground)
        menu.setStyleSheet(MENU_QSS)
        act_rename = menu.addAction("重命名便签夹")
        act_collapse = menu.addAction("全部折叠")
        menu.addSeparator()
        act_dissolve = menu.addAction("解散便签集")
        act_rename.triggered.connect(
            lambda checked=False: QTimer.singleShot(0, self.begin_rename))
        act_collapse.triggered.connect(
            lambda checked=False, s=self.stack: self.manager.collapse_all(s))
        act_dissolve.triggered.connect(
            lambda checked=False, s=self.stack: self.manager.dissolve(s))
        menu.exec(event.globalPos())


class NoteStack:
    """一叠便签（纯数据 + 成员窗口引用）。"""

    def __init__(self, stack_id):
        self.id = stack_id
        self.members = []          # 按显示顺序（stack_pos 递增）
        self.header = None         # StackHeader 或 None
        self.header_color = None   # 夹子底色（取最上面那条便签）
        self.name = DEFAULT_STACK_NAME   # 用户可改的名字（落盘在 config 的 stack_names）

    def alive_members(self):
        self.members = [m for m in self.members if _alive(m)]
        return self.members

    def visible_members(self):
        return [m for m in self.alive_members() if _visible(m)]


class NoteStackManager(QObject):
    """便签集的全局协调器：吸附判定、排布、排序、拆出、解散。"""

    def __init__(self):
        super().__init__()
        self._stacks = {}          # stack_id -> NoteStack
        self._rebuild_timer = None
        self._dragging = None      # 正在被拖动的便签（拖动期间不参与排布定位）
        self._name_cache = None    # stack_id -> 自定义名称（惰性从 config 读入）

    # ---------- 基础设施 ----------

    def _notes(self):
        try:
            import main as note_app
            return [n for n in list(note_app.ACTIVE_NOTES)]
        except Exception:
            return []

    def _later(self, delay, func):
        """延迟执行（惰性建 QTimer：本模块可能在 QApplication 之前被 import）。"""
        if self._rebuild_timer is None:
            self._rebuild_timer = QTimer(self)
            self._rebuild_timer.setSingleShot(True)
            self._rebuild_timer.timeout.connect(self._on_later)
        self._pending_func = func
        self._rebuild_timer.start(delay)

    def _on_later(self):
        func = getattr(self, '_pending_func', None)
        self._pending_func = None
        if func is not None:
            try:
                func()
            except Exception as e:
                _log(f"延迟任务失败: {e}")

    def _new_id(self):
        return "stk" + uuid.uuid4().hex[:8]

    def stack_of(self, note):
        sid = getattr(note, 'stack_id', '') or ''
        return self._stacks.get(sid)

    # ---------- 便签夹命名（落盘在 config 的 stack_names）----------

    def _names(self):
        """惰性读入自定义名称表（note_stacks 不 import main，这里才需要）。"""
        if self._name_cache is None:
            self._name_cache = {}
            try:
                import main as note_app
                data = note_app.load_config().get("stack_names") or {}
                if isinstance(data, dict):
                    self._name_cache = {str(k): str(v) for k, v in data.items()}
            except Exception as e:
                _log(f"读取便签夹名称失败: {e}")
        return self._name_cache

    def name_of(self, stack):
        return self._names().get(stack.id) or DEFAULT_STACK_NAME

    def set_name(self, stack, name):
        """改名并落盘。便签夹是虚拟分组，名字只能挂在 config 里。"""
        try:
            name = (name or "").strip() or DEFAULT_STACK_NAME
            self._names()[stack.id] = name
            stack.name = name
            if stack.header is not None and _alive_header(stack.header):
                stack.header.refresh()
            import main as note_app
            cfg = dict(note_app.load_config())   # 复制一份：文件不存在时 load_config 会返回默认字典本体
            cfg["stack_names"] = dict(self._names())
            note_app.save_config(cfg)
        except Exception as e:
            _log(f"保存便签夹名称失败: {e}")

    # ---------- 归组 / 重建 ----------

    def on_note_shown(self, note):
        """便签显示 / 新建 / 加载完成 → 稍后统一重建（批量加载只跑一次）。"""
        try:
            self._later(REBUILD_DELAY, self.rebuild)
        except Exception as e:
            _log(f"on_note_shown 失败: {e}")

    def rebuild(self):
        """按 stack_id 把内存中的便签归组，重建缺失的叠并排布。"""
        try:
            groups = {}
            for n in self._notes():
                sid = getattr(n, 'stack_id', '') or ''
                if sid and _visible(n):
                    groups.setdefault(sid, []).append(n)
            # 清掉已经不成立的叠（成员被删 / 隐藏 / 不足两条）
            for sid in list(self._stacks.keys()):
                members = [m for m in groups.get(sid, []) if _alive(m)]
                if len(members) < MIN_MEMBERS:
                    self._destroy(self._stacks[sid], unbind=False)
            # 重建 / 更新
            for sid, members in groups.items():
                if len(members) < MIN_MEMBERS:
                    continue
                members.sort(key=lambda n: int(getattr(n, 'stack_pos', 0) or 0))
                st = self._stacks.get(sid)
                if st is None:
                    st = NoteStack(sid)
                    st.name = self.name_of(st)      # 夹子建出来之前先把名字挂上
                    self._stacks[sid] = st
                st.members = members
                for i, m in enumerate(members):
                    m.stack_pos = i
                self.layout(st)
        except Exception as e:
            _log(f"rebuild 失败: {e}")

    # ---------- 排布 ----------

    def layout(self, stack, keep_note=None, free_note=None):
        """重排整叠：keep_note 原位不动，其余成员按顺序紧贴排布。"""
        try:
            members = stack.visible_members()
            if not members:
                self._hide_header(stack)
                return
            if keep_note in members:
                i = members.index(keep_note)
                top = keep_note.y()
                for m in members[:i]:
                    top -= (m.height() + STACK_GAP)
                x = keep_note.x()
            else:
                top = min(m.y() for m in members)
                x = members[0].x()
            y = top
            for m in members:
                if m is free_note:
                    y += m.height() + STACK_GAP   # 占位但不移动（拖动中的那条跟鼠标走）
                    continue
                if (m.x(), m.y()) != (x, y):
                    m.move(x, y)
                y += m.height() + STACK_GAP
            self._place_header(stack, x, top, members[0])
        except Exception as e:
            _log(f"layout 失败: {e}")

    def _place_header(self, stack, x, top, first):
        try:
            hdr = stack.header
            if hdr is None or not _alive_header(hdr):
                hdr = StackHeader(self, stack)
                stack.header = hdr
            hdr.refresh(color=getattr(first, 'bg_color', None),
                        count=len(stack.visible_members()))
            hdr.place(x, top - HEADER_H - HEADER_GAP, first.width())
            if not hdr.isVisible():
                hdr.show()
            # 只在夹子本身置顶时才 raise：非置顶窗口 raise_ 会把它顶到别的应用
            # 窗口前面（SetWindowPos HWND_TOP），排布又很频繁，看起来就像"夹子乱置顶"
            if hdr.always_on_top():
                hdr.raise_()
        except Exception as e:
            _log(f"夹子头部定位失败: {e}")

    def _hide_header(self, stack):
        try:
            if stack.header is not None and _alive_header(stack.header):
                stack.header.hide()
        except Exception:
            pass

    # ---------- 整叠移动 ----------

    def move_stack(self, stack, dx, dy):
        try:
            for m in stack.visible_members():
                m.move(m.x() + int(dx), m.y() + int(dy))
            hdr = stack.header
            if hdr is not None and _alive_header(hdr) and hdr.isVisible():
                hdr.move(hdr.x() + int(dx), hdr.y() + int(dy))
        except Exception as e:
            _log(f"move_stack 失败: {e}")

    def save_positions(self, stack):
        try:
            for m in stack.alive_members():
                if hasattr(m, 'save_data'):
                    m.save_data()
        except Exception as e:
            _log(f"保存整叠位置失败: {e}")

    def clamp_header(self, stack):
        """把夹子拉回屏幕内（成员不动）。

        整叠被拖到屏幕外（或成员展开后把夹子顶出去）时，夹子仍留在屏幕上，
        用户才有"把手"把它拖回来 —— 否则整叠会彻底失联。
        """
        try:
            hdr = stack.header
            if hdr is None or not _alive_header(hdr) or not hdr.isVisible():
                return
            hdr.clamp_into_screen()
        except Exception as e:
            _log(f"夹子归位失败: {e}")

    # ---------- 吸附 / 排序 / 拆出 ----------

    def on_drag_moved(self, note):
        """拖动过程中：叠内实时让位（其余成员按预测顺序排布）。"""
        try:
            st = self.stack_of(note)
            if st is None or len(st.visible_members()) < MIN_MEMBERS:
                return
            order = self._order_by_position(st, note)
            if order != st.members:
                st.members = order
                for i, m in enumerate(order):
                    m.stack_pos = i
            keep = next((m for m in st.members if m is not note), None)
            self.layout(st, keep_note=keep, free_note=note)
        except Exception as e:
            _log(f"拖动中排布失败: {e}")

    def on_drag_released(self, note):
        """拖动松手：叠内 → 排序或拆出；孤立 → 尝试吸附。"""
        try:
            self._dragging = None
            st = self.stack_of(note)
            if st is not None and len(st.visible_members()) >= MIN_MEMBERS:
                if self._should_detach(st, note):
                    self.detach(note)
                else:
                    keep = next((m for m in st.members if m is not note), None)
                    self.layout(st, keep_note=keep)
                    self.save_positions(st)
                return
            target = self._find_snap_target(note)
            if target is not None:
                self.snap_into(note, target)
        except Exception as e:
            _log(f"拖动结束处理失败: {e}")

    def _order_by_position(self, stack, note):
        """按当前纵坐标把 note 插到合适位置（越过邻条中线即换序）。"""
        others = [m for m in stack.members if m is not note]
        cy = note.y() + note.height() // 2
        idx = 0
        for m in others:
            if cy > m.y() + m.height() // 2:
                idx += 1
        return others[:idx] + [note] + others[idx:]

    def _should_detach(self, stack, note):
        """拖离整叠足够远 → 拆出。"""
        others = [m for m in stack.visible_members() if m is not note]
        if not others:
            return False
        top = min(m.y() for m in others)
        bottom = max(m.y() + m.height() for m in others)
        cy = note.y() + note.height() // 2
        return cy < top - DETACH_SLACK or cy > bottom + DETACH_SLACK

    def _find_snap_target(self, note):
        """在其它折叠便签里找可吸附的那条（最贴近的）。"""
        if not getattr(note, 'is_collapsed', False):
            return None
        best, best_score = None, None
        my_sid = getattr(note, 'stack_id', '') or ''
        for other in self._notes():
            if other is note or not _visible(other):
                continue
            if not getattr(other, 'is_collapsed', False):
                continue
            osid = getattr(other, 'stack_id', '') or ''
            if osid and osid == my_sid:
                continue                      # 同叠内部拖动交给排序逻辑
            if self._h_overlap(note, other) < SNAP_H_OVERLAP:
                continue
            gap = self._v_gap(note, other)
            if gap > SNAP_GAP:
                continue
            score = abs(gap) + abs(note.x() - other.x()) * 0.2
            if best_score is None or score < best_score:
                best, best_score = other, score
        return best

    @staticmethod
    def _h_overlap(a, b):
        left = max(a.x(), b.x())
        right = min(a.x() + a.width(), b.x() + b.width())
        if right <= left:
            return 0.0
        narrower = max(min(a.width(), b.width()), 1)
        return (right - left) / float(narrower)

    @staticmethod
    def _v_gap(a, b):
        """纵向边缘间距：重叠时为负值，分离时为正值。"""
        a_top, a_bottom = a.y(), a.y() + a.height()
        b_top, b_bottom = b.y(), b.y() + b.height()
        if a_bottom < b_top:
            return float(b_top - a_bottom)
        if b_bottom < a_top:
            return float(a_top - b_bottom)
        return -float(min(a_bottom, b_bottom) - max(a_top, b_top))

    def snap_into(self, note, target):
        """把 note 吸附到 target 所在的叠（以 target 为基准对齐，target 不动）。"""
        try:
            st = self.stack_of(target)
            if st is None:
                sid = self._new_id()
                st = NoteStack(sid)
                st.name = self.name_of(st)
                self._stacks[sid] = st
                st.members = [target]
                target.stack_id = sid
                target.stack_pos = 0
            old = self.stack_of(note)
            if old is not None and old is not st:
                if note in old.members:
                    old.members.remove(note)
                if len(old.members) < MIN_MEMBERS:
                    # 原叠只剩一条 → 解散并把剩下那条解绑（它已不属于任何集）
                    self._destroy(old, unbind=True)
            cy_note = note.y() + note.height() // 2
            cy_tgt = target.y() + target.height() // 2
            try:
                idx = st.members.index(target)
            except ValueError:
                st.members.append(target)
                idx = len(st.members) - 1
            idx += 0 if cy_note < cy_tgt else 1
            if note in st.members:
                st.members.remove(note)
            st.members.insert(idx, note)
            note.stack_id = st.id
            for i, m in enumerate(st.members):
                m.stack_pos = i
            target.save_data()
            note.save_data()
            self.layout(st, keep_note=target)   # 未被拖动的那条为锚
            self.save_positions(st)
        except Exception as e:
            _log(f"吸附失败: {e}")

    def detach(self, note):
        """把便签从整叠里拆出（剩余不足两条则解散）。"""
        try:
            st = self.stack_of(note)
            note.stack_id = ""
            note.stack_pos = 0
            note.save_data()
            if st is None:
                return
            if note in st.members:
                st.members.remove(note)
            if len(st.alive_members()) < MIN_MEMBERS:
                self._destroy(st)
            else:
                self.layout(st)
                self.save_positions(st)
        except Exception as e:
            _log(f"拆出失败: {e}")

    # ---------- 解散 / 展开折叠 ----------

    def dissolve(self, stack):
        try:
            self._destroy(stack)
        except Exception as e:
            _log(f"解散失败: {e}")

    def _destroy(self, stack, unbind=True):
        """销毁一叠：unbind=True 时连成员归属一起清掉（用户主动解散）。"""
        try:
            if stack is None:
                return
            if unbind:
                for m in list(stack.members):
                    if _alive(m):
                        m.stack_id = ""
                        m.stack_pos = 0
                        if hasattr(m, 'save_data'):
                            m.save_data()
            hdr = stack.header
            stack.header = None
            if hdr is not None and _alive_header(hdr):
                hdr.hide()
                hdr.deleteLater()
            self._stacks.pop(stack.id, None)
        except Exception as e:
            _log(f"销毁整叠失败: {e}")

    def collapse_all(self, stack):
        try:
            for m in list(stack.members):
                if _alive(m) and hasattr(m, 'set_collapsed'):
                    m.set_collapsed(True)
        except Exception as e:
            _log(f"全部折叠失败: {e}")

    # ---------- 事件钩子 ----------

    def on_geometry_changed(self, note):
        """成员折叠 / 展开 / 悬停抽出后 → 让整叠跟着让位。"""
        try:
            st = self.stack_of(note)
            if st is None:
                return
            self.layout(st, keep_note=note)
        except Exception as e:
            _log(f"几何变化后排布失败: {e}")

    def on_window_state_changed(self, note):
        """成员改了置顶 / 窗口标志后 → 同步夹子（夹子自己不参与动画，只改 flag）。"""
        try:
            st = self.stack_of(note)
            if st is None:
                return
            hdr = st.header
            if hdr is not None and _alive_header(hdr):
                hdr.sync_top()
        except Exception as e:
            _log(f"同步夹子置顶失败: {e}")

    def peek(self, note, on):
        """便签集成员的悬停"抽出"效果。"""
        try:
            st = self.stack_of(note)
            if st is None or not getattr(note, 'is_collapsed', False):
                return
            if not hasattr(note, 'set_peek'):
                return
            note.set_peek(on)
            self.layout(st, keep_note=note)
        except Exception as e:
            _log(f"悬停抽出失败: {e}")

    def unregister(self, note):
        """便签被删除 / 关闭时，从整叠里摘掉。"""
        try:
            st = self.stack_of(note)
            if st is None:
                return
            if note in st.members:
                st.members.remove(note)
            if len(st.alive_members()) < MIN_MEMBERS:
                self._destroy(st)
            else:
                self.layout(st)
        except Exception as e:
            _log(f"注销成员失败: {e}")


def _alive_header(hdr):
    try:
        hdr.width()
        return True
    except RuntimeError:
        return False


STACKS = NoteStackManager()
