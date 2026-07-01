"""
AniNote 统一设计系统 — 颜色 / 间距 / 字号 / 圆角 / 阴影。

"""

# ============================================================
#  色彩
# ============================================================

class Colors:
    # 主色
    primary = "#0078D7"
    primary_hover = "#005A9E"
    primary_light = "#E8F4FD"        # 选中 / 高亮背景

    # 语义色
    danger = "#E81123"
    success = "#107C10"
    warning = "#E67E22"

    # 文字
    text_primary = "#333333"
    text_secondary = "#666666"
    text_muted = "#999999"
    text_white = "#FFFFFF"
    text_inverse = "#FFFFFF"

    # 表面 / 背景
    surface = "#FFFFFF"               # 白底卡片
    surface_hover = "#F5F5F5"         # 悬停浅灰
    surface_light = "#FAFAFA"         # 极浅灰底
    bg_default = "#FFF9C4"            # 默认便签底色（暖黄）

    # 边框
    border = "#DDDDDD"
    border_light = "#EEEEEE"
    border_focus = "#0078D7"

    # 阴影
    shadow = "rgba(0, 0, 0, 0.04)"   # 统一阴影色

    # 事务追踪器预设颜色
    habit_presets = ["#E81123", "#FF8C00", "#107C10", "#0078D7", "#881798", "#333333"]


# ============================================================
#  间距
# ============================================================

class Spacing:
    xs = 4
    sm = 8
    md = 12
    lg = 16
    xl = 24


# ============================================================
#  圆角
# ============================================================

class Radius:
    sm = 4
    md = 8
    lg = 12
    xl = 16
    full = 999                     # 胶囊 / 圆形


# ============================================================
#  字号
# ============================================================

class FontSize:
    caption = 11
    small = 13
    body = 14
    medium = 15
    large = 18
    heading = 20


# ============================================================
#  字体
# ============================================================

class Font:
    family = "Microsoft YaHei"
    bold = "600"
    normal = "400"


# ============================================================
#  阴影效果（QGraphicsDropShadowEffect 参数）
# ============================================================

class Shadow:
    light = {"blur": 12, "offset": (0, 2), "color": (0, 0, 0, 30)}   # 便签卡片
    medium = {"blur": 20, "offset": (0, 6), "color": (0, 0, 0, 40)}   # 控制面板


# ============================================================
#  常用 QSS 片段
# ============================================================

class QSS:
    """可复用的样式片段，通过 f-string 拼接使用。"""

    # 基础按钮（无色底板）
    btn_bare = (
        "QPushButton {{ border: none; background: transparent;"
        " font-size: {font_size}px; color: {color}; padding: {pad_v}px {pad_h}px; }}"
        "QPushButton:hover {{ background: rgba(0,0,0,0.05); border-radius: {radius}px; }}"
    )

    # 主色按钮
    btn_primary = (
        "QPushButton {{ background-color: {primary}; color: white; padding: {pad_v}px {pad_h}px;"
        " border-radius: {radius}px; font-weight: bold; font-size: {font_size}px; }}"
        "QPushButton:hover {{ background-color: {primary_hover}; }}"
    )

    # 次色按钮（浅灰）
    btn_secondary = (
        "QPushButton {{ background-color: {bg}; color: {color}; padding: {pad_v}px {pad_h}px;"
        " border-radius: {radius}px; font-weight: bold; font-size: {font_size}px;"
        " border: 1px solid {border}; }}"
        "QPushButton:hover {{ background-color: {hover_bg}; }}"
    )

    # 输入框
    input = (
        "padding: {pad_v}px {pad_h}px; border: 1px solid {border};"
        " border-radius: {radius}px; background-color: {bg}; font-size: {font_size}px; color: {color};"
    )
    input_focus = "border: 1px solid {primary}; background-color: #FCFCFC;"

    # 卡片
    card = (
        "background-color: {bg}; border-radius: {radius}px;"
        " border: 1px solid {border};"
    )

    # 滚动条
    scrollbar = (
        "QScrollBar:vertical {{ background: transparent; width: 5px; margin: 0; }}"
        "QScrollBar::handle:vertical {{ background: #D0D0D0; border-radius: 2px; min-height: 20px; }}"
        "QScrollBar::handle:vertical:hover {{ background: #A0A0A0; }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}"
    )
