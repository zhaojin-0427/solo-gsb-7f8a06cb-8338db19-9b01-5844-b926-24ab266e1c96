"""URI 解析与编码工具。

规则（见需求）：
* href 相对所在文件解析；
* 路径与片段各只百分号解码一次；
* 文件名与 id 区分大小写（不做大小写折叠）；
* 空片段 / 无片段指向章节顶部；
* 外部链接（scheme、协议相对）不处理；
* 输出为正确百分号编码的相对 href。
"""

from __future__ import annotations

import posixpath
import re
import unicodedata
import urllib.parse

# RFC 3986 scheme = ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ) ":"
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# query 前的 '#' 才是片段分隔符
_HASH_RE = re.compile(r"#(?=[^?#]*$)")


def is_external(href: str) -> bool:
    """判断 href 是否为外部引用。

    带显式 scheme（http:、mailto:、data: …）或以 ``//`` 开头的协议相对
    引用都算外部，本工具不校验、不改动。
    """
    if href.startswith("//"):
        return True
    return bool(_SCHEME_RE.match(href.lstrip()))


def split_href(href: str) -> tuple[str, str | None]:
    """把 href 拆成 (路径部分, 片段)。

    片段为 None 表示没有 ``#``；为 "" 表示空片段（指向章节顶部）。
    query（``?`` 之后）不属于路径也不属于片段，直接丢弃，不参与解析。
    """
    m = _HASH_RE.search(href)
    if not m:
        path_part = href
        fragment = None
    else:
        path_part = href[: m.start()]
        fragment = href[m.start() + 1 :]
    if "?" in path_part:
        path_part = path_part.split("?", 1)[0]
    return path_part, fragment


def percent_decode_once(component: str) -> str:
    """百分号解码恰好一次；非法转义抛出 ValueError。"""
    # unquote 本身只做一次解码（不会对解码结果再解码）。
    try:
        return urllib.parse.unquote(component, errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"百分号编码不是合法 UTF-8: {component!r}") from exc


def resolve_relative(base_dir: str, ref_path: str) -> str:
    """把相对引用解析成 ZIP 内归一化路径。

    ``base_dir`` 为引用所在文件所在目录（POSIX，可能为 ""）。
    以 ``/`` 开头的路径相对包根解析。
    归一化后若越出包根（".." 逃逸），抛出 ValueError。
    结果使用 NFC 归一化，便于与 ZIP 条目按文件系统常见形态比对；
    大小写不做折叠。
    """
    if ref_path.startswith("/"):
        joined = ref_path[1:]
    else:
        joined = posixpath.join(base_dir, ref_path) if base_dir else ref_path
    stack: list[str] = []
    for seg in joined.split("/"):
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            if not stack:
                raise ValueError(f"相对路径越界: {ref_path!r}")
            stack.pop()
        else:
            stack.append(seg)
    return unicodedata.normalize("NFC", "/".join(stack))


def quote_path_segment(segment: str) -> str:
    """单个路径段的百分号编码（保留 segment 内允许的字符）。"""
    return urllib.parse.quote(segment, safe="!$&'()*+,:;=@[]~")


def quote_fragment(frag: str) -> str:
    """片段百分号编码。'#'、空格等必须编码。"""
    return urllib.parse.quote(frag, safe="!$&'()*+,:;=?@[]~")


def build_href(source_path: str, target_path: str, fragment: str | None) -> str:
    """生成从 source 文件指向 target 文件（同包内 XHTML）的相对 href。

    同篇引用输出纯片段（``#id``）；fragment 为 None/"" 表示章节顶部，
    同篇顶部没有实际意义，调用方不会产生该组合；跨篇顶部输出纯相对
    路径（不带 ``#``）。
    """
    if target_path == source_path:
        if fragment:
            return f"#{quote_fragment(fragment)}"
        # 同篇指顶部：空片段
        return ""
    rel = posixpath.relpath(target_path, posixpath.dirname(source_path) or ".")
    rel = "/".join(quote_path_segment(seg) for seg in rel.split("/"))
    if fragment:
        return f"{rel}#{quote_fragment(fragment)}"
    return rel


def same_document(path_part: str) -> bool:
    """路径部分为空（或仅空白字符）表示篇内引用。"""
    return path_part.strip() == ""
