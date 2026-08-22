"""Markdown ⇄ Qt 富文本 双向转换器（AniNote 便签 Markdown 模式）。

md_to_html:     Markdown 源码 → HTML 字符串（mistune 完整 GFM 渲染，中性配色）
doc_to_markdown: QTextDocument → Markdown 源码（从 block/charFormat 结构化遍历，高保真）

设计约束：
- 渲染用 mistune（完整 GFM：嵌套列表/缩进代码块/转义/脚注/表格等），零手写解析器
- 图片用相对路径（与便签 data.json 的 html_content 约定一致）
- 任务列表 - [ ] / - [x] 与富文本 ☐/☑ 双向映射
- 配色中性：正文黑、标题深灰黑、引用灰底、代码灰底、链接蓝（不跟随便签蓝色主题）
"""

import os
import re
import urllib.parse

import mistune
from mistune import HTMLRenderer

# ---------- Markdown → HTML（mistune 渲染器） ----------


class _NoteRenderer(HTMLRenderer):
    """AniNote 便签渲染器：内联样式 + 中性配色 + 相对路径图片解析。

    base_dir 为便签目录（用于把 Markdown 图片相对路径解析为 file:// 绝对 URL）。
    """

    base_dir = ""

    # ---- 块级 ----

    def heading(self, text, level, **attrs):
        sizes = {1: 18, 2: 16, 3: 14, 4: 13, 5: 13, 6: 13}
        return (
            f"<h{level} style='font-size:{sizes.get(level, 13)}px; color:#0C447C;"
            f" margin:8px 0 3px; font-weight:bold;'>{text}</h{level}>"
        )

    def paragraph(self, text):
        return f"<p style='margin:3px 0; color:#333333;'>{text}</p>"

    def block_quote(self, text):
        return (
            "<blockquote style='border-left:3px solid #0078D7; background:#E6F1FB;"
            " border-radius:0 6px 6px 0; padding:4px 10px; margin:6px 0; color:#185FA5;'>"
            + text + "</blockquote>"
        )

    def list(self, text, ordered, **attrs):
        tag = "ol" if ordered else "ul"
        return f"<{tag} style='margin:4px 0; padding-left:22px;'>{text}</{tag}>"

    def list_item(self, text):
        # 任务列表：mistune task_lists 插件输出
        # <input class="task-list-item-checkbox" type="checkbox" disabled[ checked]/>
        # → 替换为便签风格的 ☐/☑ 字符
        m = re.match(
            r'^<input class="task-list-item-checkbox" type="checkbox"( disabled)?( checked)?/>?(.*)$',
            text, re.S,
        )
        if m:
            mark = "☑" if m.group(2) else "☐"
            return f"<li style='color:#333333;'>{mark} {m.group(3)}</li>"
        return f"<li style='color:#333333;'>{text}</li>"

    def block_code(self, code, info=None):
        return (
            "<pre style='background:#F4F5F6; border-radius:6px; padding:6px 10px;"
            " font-family:Consolas,monospace; font-size:12px; margin:6px 0;"
            " color:#333333; white-space:pre-wrap;'><code>" + code + "</code></pre>"
        )

    def thematic_break(self):
        return "<hr style='border:none; border-top:1px solid #DDDDDD; margin:8px 0;'>"

    def table(self, text):
        return (
            "<table style='border-collapse:collapse; margin:4px 0;'>"
            + text + "</table>"
        )

    def table_row(self, text):
        return "<tr>" + text + "</tr>"

    def table_cell(self, text, align=None, head=False):
        tag = "th" if head else "td"
        style = "border:1px solid #DDDDDD; padding:2px 8px; font-size:12px;"
        if align:
            style += f" text-align:{align};"
        if head:
            style += " font-weight:bold; background:#E8F4FD;"
        return f"<{tag} style='{style}'>{text}</{tag}>"

    # ---- 行内 ----

    def codespan(self, text):
        return (
            "<code style='background:#F1F3F4; border-radius:3px; padding:0 4px;"
            " font-family:Consolas,monospace; font-size:12px; color:#B4005A;'>"
            + text + "</code>"
        )

    def link(self, text, url, title=None):
        title_attr = f' title="{title}"' if title else ""
        return (
            f'<a href="{url}" style="color:#0078D7; text-decoration:none;"'
            f'{title_attr}>{text}</a>'
        )

    def image(self, alt, url, title=None):
        src = _resolve_image_src(url, self.base_dir)
        title_attr = f' title="{title}"' if title else ' title="双击查看原图"'
        return (
            f'<img src="{src}" style="max-width:100%; max-height:400px;"{title_attr}>'
        )


_engine = mistune.create_markdown(
    renderer=_NoteRenderer(),
    plugins=["strikethrough", "table", "url", "task_lists", "footnotes"],
)


def _resolve_image_src(src, base_dir):
    """把 Markdown 图片相对路径解析为 QTextBrowser 可用的 file:// 绝对 URL。"""
    src = src.strip()
    if not src:
        return ""
    if src.startswith(("http://", "https://", "data:")):
        return src
    if src.startswith("file://"):
        return src
    if base_dir:
        p = os.path.join(base_dir, src.replace("/", os.sep))
        return "file:///" + p.replace(os.sep, "/")
    return src


def md_to_html(text, base_dir=""):
    """Markdown 源码 → HTML。base_dir 为便签目录（用于解析相对路径图片）。"""
    _NoteRenderer.base_dir = base_dir
    html = _engine(text or "")
    # task_lists 插件绕过 list_item 直接输出
    # <li class="task-list-item"><input class="task-list-item-checkbox" .../>
    # → 替换为便签风格的 ☐/☑ 文本（QTextBrowser 不渲染 disabled checkbox）
    html = re.sub(
        r'<li class="task-list-item"><input class="task-list-item-checkbox"'
        r' type="checkbox"( disabled)?( checked)?/?>(.*?)</li>',
        lambda m: f'<li style="color:#333333;">{"☑" if m.group(2) else "☐"} {m.group(3)}</li>',
        html,
        flags=re.S,
    )
    return html


# ---------- QTextDocument → Markdown ----------

QFont_BOLD_THRESHOLD = 700


def _fragment_to_md(fragment, base_dir, skip_bold=False):
    """单个文本片段 → Markdown 片段（处理粗体/斜体/删除线/等宽/图片）。

    skip_bold: 标题块内 Qt 默认粗体，跳过 ** 包裹（# 前缀已表达层级）。
    """
    fmt = fragment.charFormat()
    if fmt.isImageFormat():
        img = fmt.toImageFormat()
        name = img.name() or ""
        # 解析相对路径（file:/// 前缀按最长优先剥除）
        if name.startswith("file:///"):
            decoded = urllib.parse.unquote(name[len("file:///"):])
        elif name.startswith("file://"):
            decoded = urllib.parse.unquote(name[len("file://"):])
        else:
            decoded = name
        if decoded.startswith(("http://", "https://", "data:")):
            rel = decoded
        elif name.startswith(("http://", "https://", "data:")):
            rel = name
        else:
            try:
                rel = os.path.relpath(decoded, base_dir).replace(os.sep, "/")
            except ValueError:
                rel = decoded
        alt = os.path.basename(rel) if rel else "图片"
        return f"![{alt}]({rel})"

    text = fragment.text()
    if not text:
        return ""
    parts = []
    if not skip_bold and fmt.fontWeight() >= QFont_BOLD_THRESHOLD:
        parts.append("**")
    if fmt.fontItalic():
        parts.append("*")
    if fmt.fontStrikeOut():
        parts.append("~~")
    if fmt.fontFixedPitch():
        parts.append("`")
    parts.append(text)
    if fmt.fontFixedPitch():
        parts.append("`")
    if not skip_bold and fmt.fontStrikeOut():
        parts.append("~~")
    if not skip_bold and fmt.fontItalic():
        parts.append("*")
    if not skip_bold and fmt.fontWeight() >= QFont_BOLD_THRESHOLD:
        parts.append("**")
    return "".join(parts)


def doc_to_markdown(document, base_dir=""):
    """QTextDocument → Markdown 源码（高保真：标题/列表/任务/图片/行内样式）。"""
    lines = []
    block = document.begin()
    while block.isValid():
        bf = block.blockFormat()
        level = bf.headingLevel()
        text = block.text()

        prefix = ""
        task_prefix = False
        # 任务列表（行首 ☐/☑）
        stripped = text.lstrip()
        if stripped.startswith(("☐", "☑")):
            checked = "x" if stripped[0] == "☑" else " "
            indent = " " * (len(text) - len(stripped))
            prefix = f"{indent}- [{checked}] "
            task_prefix = True
        elif level:
            prefix = "#" * level + " "
        else:
            from PySide6.QtGui import QTextListFormat
            text_list = block.textList()
            if text_list is not None:
                style = text_list.format().style()
                if style == QTextListFormat.ListDecimal:
                    prefix = "1. "
                else:  # ListDisc / ListCircle / ListSquare
                    prefix = "- "

        frag_parts = []
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                t = _fragment_to_md(frag, base_dir, skip_bold=bool(level))
                # 任务行：剥掉行首 ☐/☑（prefix 已含 - [ ] 标记）
                if task_prefix and t.startswith(("☐", "☑")):
                    t = t[1:].lstrip()
                frag_parts.append(t)
            it += 1

        body = "".join(frag_parts).strip()
        if prefix or body:
            lines.append(prefix + body)
        else:
            lines.append("")
        block = block.next()

    # 压缩连续空行（最多 1 个），并去掉开头/结尾空行
    result = []
    prev_blank = False
    for ln in lines:
        if not ln.strip():
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        result.append(ln)
    while result and not result[0].strip():
        result.pop(0)
    while result and not result[-1].strip():
        result.pop()
    return "\n".join(result)
