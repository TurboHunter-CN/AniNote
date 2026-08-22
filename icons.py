"""
AniNote 图标系统 — 使用 Google Material Symbols 字体。

TTF 可由 WOFF2 转换得到（运行 _convert_font.py），或手动下载放到项目根目录。
"""

from PySide6.QtGui import QFontDatabase, QFont
import os

_FONT_LOADED = False
_FONT_FAMILY = "Material Symbols Outlined"
_FONT_AVAILABLE = False  # True when font file is found and loaded
_FAMILIES = []           # Populated by _init_font()


def _init_font():
    """加载 Material Symbols 字体（仅执行一次）。
    优先加载静态实例化版本，避免变量字体在 Qt 中渲染异常。
    """
    global _FONT_LOADED, _FONT_AVAILABLE, _FAMILIES
    if _FONT_LOADED:
        return
    _FONT_LOADED = True
    font_paths = [
        os.path.join(os.path.dirname(__file__), "MaterialSymbolsOutlined_Static.ttf"),
        os.path.join(os.path.dirname(__file__), "MaterialSymbolsOutlined.ttf"),
        os.path.join(os.path.dirname(__file__), "MaterialSymbolsOutlined.woff2"),
    ]
    for fp in font_paths:
        if os.path.exists(fp) and os.path.getsize(fp) > 50000:
            fid = QFontDatabase.addApplicationFont(fp)
            if fid >= 0:
                _FONT_AVAILABLE = True
                _FAMILIES = QFontDatabase.applicationFontFamilies(fid)
                return

    # 字体未安装 → 使用 Unicode 后备方案
    _ICONS.clear()
    _ICONS.update(_FALLBACK_ICONS)


def icon(name, size=18):
    """返回图标字符。Material Symbols 字体可用时优先使用 Google 图标，
    否则退回到 Unicode 符号。

    Args:
        name: 图标名（如 'add', 'delete', 'search'）。
        size: 字号（仅 Material 模式使用）。
    Returns:
        str: 图标字符。
    """
    _init_font()
    return _ICONS.get(name, "")


def fallback_icon(name):
    """始终返回 Unicode 后备字符，不依赖 Material Symbols 字体。
    用于 QMenu/QAction 等不能使用 PUA 码点的场景。
    """
    return _FALLBACK_ICONS.get(name, "")


def icon_text(name, label="", size=16):
    """图标 + 文字，适合 QPushButton / QAction 的 text 属性。

    Args:
        name: 图标名。
        label: 文字标签。
        size: 字号。
    Returns:
        str: "图标  文字" 格式字符串。
    """
    ic = icon(name, size)
    return f"{ic}  {label}" if label else ic


def set_icon_font(widget, size=18):
    """将 widget 的字体设为 Material Icons 优先 + 系统字体兜底。

    调用后 widget 中的图标 PUA 码位由 Material Icons 渲染，
    中文等字符由系统默认字体渲染。
    """
    _init_font()
    if not _FONT_AVAILABLE:
        return
    font = widget.font()
    families = [_FAMILIES[0]] + font.families()
    font.setFamilies(families)
    font.setPixelSize(size)
    widget.setFont(font)


# Material Symbols Outlined Unicode 码点映射
# 来源: https://marella.github.io/material-symbols/demo/
_ICONS = {
    # 导航
    "arrow_back":    "\ue5c4",   # ←
    "arrow_forward": "\ue5c8",   # →
    "chevron_left":  "\ue5cb",   # ◀
    "chevron_right": "\ue5cc",   # ▶
    "expand_more":   "\ue5cf",   # ▼
    "expand_less":   "\ue5ce",   # ▲

    # 操作
    "add":           "\ue145",   # +
    "add_circle":    "\ue147",   # ⊕
    "arrow_upward":  "\ue5d8",   # ↑
    "arrow_downward":"\ue5db",   # ↓
    "crop_square":   "\ue3c1",   # □
    "fullscreen":    "\ue5d0",   # 全屏
    "close":         "\ue5cd",   # ×
    "delete":        "\ue872",   # 垃圾桶
    "edit":          "\ue3c9",   # 笔
    "refresh":       "\ue5d5",   # 刷新
    "search":        "\ue8b6",   # 搜索
    "settings":      "\ue8b8",   # 齿轮
    "visibility":    "\ue8f4",   # 眼睛（显示）
    "visibility_off":"\ue8f5",   # 眼睛关闭（隐藏）
    "download":      "\ue2c0",   # 下载/导出
    "upload":        "\ue2c6",   # 上传

    # 状态
    "lock":          "\ue897",   # 锁
    "lock_open":     "\ue898",   # 开锁
    "push_pin":      "\uf10d",   # 图钉（置顶）

    # 内容
    "check_box":     "\ue834",   # ☑
    "check_box_outline": "\ue835",  # ☐
    "check_circle":  "\ue86c",   # 圆圈勾
    "circle":        "\uef4a",   # 空心圆 ○
    "task_alt":      "\ue2e6",   # ✓
    "content_copy":  "\ue14d",   # 复制
    "description":   "\ue873",   # 文档

    # 文件
    "dashboard":     "\ue871",   # 仪表盘
    "note_add":      "\ue89c",   # 新建便签
    "playlist_add":  "\ue03b",   # 列表添加

    # 警告
    "error":         "\ue000",   # 错误
    "warning":       "\ue002",   # 警告
    "info":          "\ue88e",   # 信息

    # 杂项
    "calendar_today":"\ue935",   # 日历
    "schedule":      "\ue8b5",   # 时钟
    "timer":         "\ue425",   # 计时器
    "more_horiz":    "\ue5d3",   # 更多水平
    "more_vert":     "\ue5d4",   # 更多垂直
    "menu":          "\ue5d2",   # 菜单
    "home":          "\ue88a",   # 主页
    "palette":       "\ue40a",   # 调色板
    "photo_camera":  "\ue412",   # 相机/截图
    "insert_photo":  "\ue3f4",   # 图片/插入图片
    "content_cut":   "\ue14e",   # 剪刀
    "format_bold":   "\ue238",
    "format_italic": "\ue23f",
    "format_underlined": "\ue249",
    "format_color_text": "\ue23c",
}

# 后备 Unicode 图标（当 Material Symbols 字体未安装时使用）
_FALLBACK_ICONS = {
    "arrow_back":    "\u25c0",   # ◀
    "arrow_forward": "\u25b6",   # ▶
    "chevron_left":  "\u2039",   # ‹
    "chevron_right": "\u203a",   # ›
    "expand_more":   "\u25bc",   # ▼
    "expand_less":   "\u25b2",   # ▲
    "add":           "\u229e",   # ⊞
    "close":         "\u2715",   # ✕
    "delete":        "\u2715",   # ✕
    "refresh":       "\u21bb",   # ↻
    "search":        "\u2315",   # ⌕
    "settings":      "\u2699",   # ⚙
    "lock":          "\u26bf",   # ⚿
    "push_pin":      "\u25b2",   # ▲
    "check_box":     "\u2611",   # ☑
    "check_box_outline": "\u2610",  # ☐
    "task_alt":      "\u2713",   # ✓
    "circle":        "\u25cb",   # ○
    "note_add":      "\u229e",   # ⊞
    "calendar_today":"\u25a0",   # ■
    "schedule":      "\u25d8",   # ◘
    "home":          "\u2302",   # ⌂
    "palette":       "\u25d0",   # ◐
    "dashboard":     "\u2630",   # ☰
    "visibility":    "\u25c9",   # ◉
    "download":      "\u2193",   # ↓
    "upload":        "\u2191",   # ↑
    "arrow_upward":  "\u2191",   # ↑
    "arrow_downward":"\u2193",   # ↓
    "crop_square":   "\u25a1",   # □
    "fullscreen":    "\u29c9",   # ⧉
    "playlist_add":  "\u271a",   # ✚
}
