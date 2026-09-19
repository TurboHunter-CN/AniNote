"""
AniNote 字体工具 — 统一字体族取值与真实字面（字重）解析。

设计要点
--------
Qt 在 Windows 上对「粗体」的处理有两种：
1. **真实字面**：字体文件里本身就设计好的 Bold / Medium / Light（如思源黑体有 7 档）。
   直接用 `setStyleName("Bold")` 命中，笔画是设计出来的，边缘干净。
2. **合成粗体**：字体族没有该字重时，Qt 用数学方式把笔画整体撑粗。
   实测微软雅黑在 Qt 中只暴露 Regular / Light / Bold 三个 styleName，
   但 `setStyleName("Bold")` 不生效、字重恒为 400，代码里的 `setBold(True)`
   全部走了数学撑粗 —— 笔画相互粘连，这就是「发虚」的根源。

因此这里统一提供 `make_font()`：优先按 styleName 找真实字面，
找不到才退回 `setWeight()`。这样换任何字体都能拿到最好的那一档。
"""

import os

from PySide6.QtGui import QFontDatabase, QFont

# 默认字体族（Noto Sans SC 为思源黑体同源，自带 7 档真实字重）
DEFAULT_FONT_FAMILY = "Noto Sans SC"

# 常见字重 → 候选 styleName（按优先级从高到低试探）
_WEIGHT_STYLE_CANDIDATES = {
    "black": ("Black", "Heavy"),
    "bold": ("Bold", "SemiBold", "Medium", "DemiBold"),
    "semibold": ("SemiBold", "Medium", "DemiBold", "Bold"),
    "medium": ("Medium", "DemiBold", "SemiBold", "Regular"),
    "regular": ("Regular", "Book", "Normal"),
    "light": ("Light", "DemiLight", "Thin"),
    "thin": ("Thin", "ExtraLight", "Light"),
}

# 字重关键字 → Qt QFont.Weight（找不到真实字面时的回退）
_WEIGHT_FALLBACK = {
    "black": QFont.Weight.Black,
    "bold": QFont.Weight.Bold,
    "semibold": QFont.Weight.DemiBold,
    "medium": QFont.Weight.Medium,
    "regular": QFont.Weight.Normal,
    "light": QFont.Weight.Light,
    "thin": QFont.Weight.Thin,
}

# 系统未安装默认字体时的退路（按顺序试探）。
# 这里列出旧字体名是有意为之 —— 它们只作为「可用性判断的候选」，
# 不参与任何硬编码渲染，用户换字体时会被配置值覆盖。
_FALLBACK_FAMILIES = (
    DEFAULT_FONT_FAMILY,
    "Source Han Sans SC",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "SimHei",
)

_style_cache = {}      # family -> set(styleName)，避免重复查询
_resolved_family = None
_families_cache = None  # 系统字体族集合（进程内不变，缓存一次）


def _styles_of(family):
    """取某字体族支持的 styleName 集合（带缓存）。"""
    if family not in _style_cache:
        try:
            _style_cache[family] = set(QFontDatabase.styles(family))
        except Exception:
            _style_cache[family] = set()
    return _style_cache[family]


def available_families():
    """当前系统已安装的字体族集合（带缓存）。

    字体族列表在进程运行期不会变化，而 `QFontDatabase.families()` 每次都要
    枚举几百个字体。它会被 `make_font()` 间接调用 —— 而刷新便签标题字号时
    可能一帧内调用多次，缓存后这部分开销降到接近零。
    """
    global _families_cache
    if _families_cache is None:
        try:
            _families_cache = set(QFontDatabase.families())
        except Exception:
            _families_cache = set()
    return _families_cache


def resolve_family(family=None):
    """解析出实际可用的字体族名。

    传入的字体族若未安装（例如用户配置里存的是别的机器上的字体），
    按 _FALLBACK_FAMILIES 顺序回退，保证不会退化成 Qt 默认的无衬线体。
    """
    global _resolved_family

    installed = available_families()
    if family and family in installed:
        return family

    if _resolved_family and _resolved_family in installed:
        return _resolved_family

    for cand in _FALLBACK_FAMILIES:
        if cand in installed:
            _resolved_family = cand
            return cand
    return family or DEFAULT_FONT_FAMILY


def make_font(family=None, px=None, weight="regular", bold=False, italic=False):
    """构造字体对象：优先命中真实字面，否则回退合成字重。

    Args:
        family: 字体族；None 时使用 resolve_family() 的结果。
        px: 像素字号；None 表示不设置（沿用父级）。
        weight: 'thin' / 'light' / 'regular' / 'medium' / 'semibold' / 'bold' / 'black'。
        bold: 兼容旧调用——True 等价于 weight='bold'。
        italic: 是否斜体。
    Returns:
        QFont
    """
    fam = resolve_family(family)
    f = QFont()
    # 必须显式给出备用字体，但**只挂一个**。
    # 只设单个字体族时，Qt 量文本宽度/求字形要拿整个系统字体库去找后备字形；
    # 而回退链越长越慢（Qt 沿链逐个试）：实测同一段 4000 字文本，
    # 幼圆单独用 208 ms、挂 6 个回退 138 ms、只挂 1 个 2.1 ms。
    # 这就是"内容多的便签展开要等好几秒、内容一条条冒出来"的真正根源 ——
    # 与重绘时机无关，是字体查找把布局拖垮了。
    fb = best_fallback(fam)
    f.setFamilies([fam, fb] if fb else [fam])

    if bold:
        weight = "bold"
    key = str(weight).lower()
    styles = _styles_of(fam)

    hit = None
    for cand in _WEIGHT_STYLE_CANDIDATES.get(key, ()):
        if cand in styles:
            hit = cand
            break

    if hit:
        # 命中真实字面 —— 这是笔画干净的关键
        f.setStyleName(hit)
    else:
        # 没有该档位：退回 Qt 合成
        f.setWeight(_WEIGHT_FALLBACK.get(key, QFont.Weight.Normal))

    # 字号必须为正：调用方（如日程块按可用高度自适应字号）在极端尺寸下
    # 可能算出 0 或负数，直接交给 Qt 会刷 "Pixel size <= 0" 警告并让字体失效。
    try:
        if px and int(px) > 0:
            f.setPixelSize(int(px))
    except (TypeError, ValueError):
        pass
    if italic:
        f.setItalic(True)
    return f




def best_fallback(family=None):
    """挑一个确定可用、字形完整的中文备用字体。

    回退链**只挂一个**：链越长越慢 —— Qt 会沿着链逐个试。
    实测同一段 4000 字文本，挂 6 个回退要 138 ms，只挂 1 个只要 2.1 ms。
    """
    fam = resolve_family(family)
    for cand in _FALLBACK_FAMILIES:
        if cand != fam and cand in available_families():
            return cand
    return None


def font_family_css(family=None):
    """供 QSS 内联使用的 font-family 片段（**必须带备用字体**）。

    只写单个字体族时，Qt 量文本宽度/求字形要拿整个系统字体库去找后备字形：
    实测同一段 4000 字文本，幼圆单独用要 208 ms，补上一个备用字体只要 2.1 ms。
    内容多的便签展开要等好几秒、内容一条条冒出来，根因就在这里。
    """
    fam = resolve_family(family)
    fb = best_fallback(fam)
    return f"font-family: '{fam}', '{fb}';" if fb else f"font-family: '{fam}';"
