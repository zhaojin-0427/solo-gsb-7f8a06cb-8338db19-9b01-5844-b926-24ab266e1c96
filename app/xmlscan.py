"""XHTML 字节级扫描。

lxml 负责权威的 XML 解析与语义树；本模块在 lxml 解析成功的前提下，对
原文做一次确定性的词法扫描（在解码后的 str 上进行），给出每个
``<a href>`` 属性值在原始字节中的精确区间，使导出时能做到"除属性值
字节外其余字节完全相同"。

扫描器严格按 XML 1.0 词法跳过注释、CDATA、PI、DOCTYPE、文本，自行维护
命名空间栈；调用方会用 lxml 树按文档顺序得到的锚点列表与扫描结果逐条
核对（命名空间、行号、属性值归一化结果），只要有一条对不上，该文件的
链接全部判为"无法唯一对应原文属性位置"，禁止导出。

字符偏移到字节偏移的映射通过"前缀增量编码"建立，因此对 GBK 等多字节、
尾字节可能为 0x22/0x3C 的编码同样安全。
"""

from __future__ import annotations

import codecs
import html.entities
import re
from dataclasses import dataclass

from lxml import etree

from .errors import LocatedError

XML_NS = "http://www.w3.org/XML/1998/namespace"
XHTML_NS = "http://www.w3.org/1999/xhtml"

_NAMED_ENTITIES = dict(html.entities.name2codepoint)
_NAMED_ENTITIES["apos"] = 0x27

_ENTITY_RE = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);|&([A-Za-z][A-Za-z0-9_.-]*);")


def unescape_xml_text(text: str) -> str:
    """与 XML 解析器一致地展开实体（含数字字符引用与 XHTML 命名字体）。"""

    def repl(m: re.Match) -> str:
        num = m.group(1)
        if num is not None:
            code = int(num[1:], 16) if num[0] in "xX" else int(num)
            return chr(code)
        name = m.group(2)
        if name in _NAMED_ENTITIES:
            return chr(_NAMED_ENTITIES[name])
        return m.group(0)

    return _ENTITY_RE.sub(repl, text)


def normalize_attribute_value(value: str) -> str:
    """复刻 XML 规范对属性值的处理：换行归一化 + 实体展开。"""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return unescape_xml_text(value)


@dataclass
class RawAnchor:
    """一个 <a href> 在原文字节中的定位信息。"""

    line: int                       # '<' 所在行（1 起）
    ns_uri: str                     # 元素命名空间（无命名空间为 ""）
    qname: str                      # 原始限定名（如 a / html:a）
    value_start: int                # href 属性值（不含引号）起点（字节）
    value_end: int                  # 属性值终点（不含引号，字节）
    quote: str                      # 引号字符 ' 或 "
    raw_value: str                  # 解码、归一化后的属性值


class _Scope:
    __slots__ = ("decls", "parent")

    def __init__(self, parent: "_Scope | None", decls: dict[str, str]):
        self.decls = decls
        self.parent = parent

    def lookup_prefix(self, prefix: str) -> str | None:
        scope: "_Scope | None" = self
        while scope is not None:
            if prefix in scope.decls:
                return scope.decls[prefix]
            scope = scope.parent
        return None

    def lookup_default(self) -> str | None:
        scope: "_Scope | None" = self
        while scope is not None:
            if "" in scope.decls:
                return scope.decls[""]
            scope = scope.parent
        return None


def _resolve_qname(qname: str, scope: _Scope) -> tuple[str, str]:
    if ":" in qname:
        prefix, local = qname.split(":", 1)
        if prefix == "xml":
            return XML_NS, local
        uri = scope.lookup_prefix(prefix)
        if uri is None:
            uri = ""
        return uri, local
    # 注意 lookup_default() 返回 None（未声明）与 ""（xmlns="" 取消）有别
    default = scope.lookup_default()
    return (default if default is not None else ""), qname


@dataclass
class _Attr:
    name: str
    value_start: int   # 字符偏移
    value_end: int
    quote: str


def _parse_start_tag(s: str, i: int) -> tuple[str, list[_Attr], bool, int]:
    """解析开始标签内容，i 指向 '<' 之后首字符。"""
    n = len(s)
    start = i
    while i < n and s[i] not in " \t\r\n/>":
        i += 1
    qname = s[start:i]
    attrs: list[_Attr] = []
    self_closing = False
    while True:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n:
            raise ValueError("开始标签未闭合")
        c = s[i]
        if c == ">":
            i += 1
            break
        if c == "/":
            self_closing = True
            i += 1
            continue
        name_start = i
        while i < n and s[i] not in " \t\r\n/>=":
            i += 1
        attr_name = s[name_start:i]
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n or s[i] != "=":
            raise ValueError("属性缺少 =")
        i += 1
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n or s[i] not in ('"', "'"):
            raise ValueError("属性值缺少引号")
        quote = s[i]
        i += 1
        value_start = i
        while i < n and s[i] != quote:
            i += 1
        if i >= n:
            raise ValueError("属性值未闭合")
        value_end = i
        i += 1
        attrs.append(_Attr(attr_name, value_start, value_end, quote))
    return qname, attrs, self_closing, i


def _doctype_end(s: str, start: int) -> int:
    j = start
    n = len(s)
    bracket = 0
    quote = ""
    while j < n:
        ch = s[j]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket -= 1
        elif ch == ">" and bracket == 0:
            return j + 1
        j += 1
    raise ValueError("DOCTYPE 未闭合")


class _ByteMap:
    """字符偏移 -> 字节偏移（对给定编码做前缀增量编码）。"""

    def __init__(self, text: str, encoding: str):
        self.encoding = encoding
        self._text = text
        self._cache_char = 0
        self._cache_byte = 0
        self._encoder = codecs.getincrementalencoder(encoding)()

    def byte_offset(self, char_pos: int) -> int:
        # 简单的单调缓存即可：调用方按文档顺序访问
        if char_pos < self._cache_char:
            # 从头重算（极少发生）
            self._cache_char = 0
            self._cache_byte = 0
            self._encoder = codecs.getincrementalencoder(self.encoding)()
        chunk = self._text[self._cache_char:char_pos]
        encoded = self._encoder.encode(chunk, final=False)
        self._cache_byte += len(encoded)
        self._cache_char = char_pos
        return self._cache_byte


def scan_anchors(raw: bytes, encoding: str) -> list[RawAnchor]:
    """扫描 XHTML，按文档顺序返回所有 XHTML 命名空间 ``a[href]`` 的定位。"""
    used_raw = raw
    if used_raw.startswith(codecs.BOM_UTF8):
        used_raw = used_raw[3:]
    try:
        text = used_raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = used_raw.decode(encoding, errors="replace")

    bmap = _ByteMap(text, encoding)
    n = len(text)
    i = 0
    line = 1
    scope_stack: list[_Scope] = []
    anchors: list[RawAnchor] = []

    while i < n:
        if text[i] == "<":
            if i + 1 >= n:
                break
            nxt = text[i + 1]
            if nxt == "?":  # PI / XML 声明
                end = text.find("?>", i + 2)
                if end < 0:
                    raise ValueError("处理指令未闭合")
                line += text.count("\n", i, end + 2)
                i = end + 2
                continue
            if nxt == "!":
                if text[i + 2 : i + 4] == "--":  # 注释
                    end = text.find("-->", i + 4)
                    if end < 0:
                        raise ValueError("注释未闭合")
                    line += text.count("\n", i, end + 3)
                    i = end + 3
                    continue
                if text[i + 2 : i + 9] == "[CDATA[":  # CDATA
                    end = text.find("]]>", i + 9)
                    if end < 0:
                        raise ValueError("CDATA 未闭合")
                    line += text.count("\n", i, end + 3)
                    i = end + 3
                    continue
                # DOCTYPE
                try:
                    end = _doctype_end(text, i + 2)
                except ValueError as exc:
                    raise LocatedError(str(exc), line=line) from exc
                line += text.count("\n", i, end)
                i = end
                continue
            if nxt == "/":  # 结束标签
                end = text.find(">", i + 2)
                if end < 0:
                    raise ValueError("结束标签未闭合")
                line += text.count("\n", i, end + 1)
                i = end + 1
                if scope_stack:
                    scope_stack.pop()
                continue
            # 开始标签
            tag_line = line
            tag_start = i
            try:
                qname, attrs, self_closing, new_i = _parse_start_tag(text, i + 1)
            except ValueError as exc:
                raise LocatedError(
                    f"开始标签词法扫描失败: {exc}", line=tag_line
                ) from exc
            line += text.count("\n", tag_start, new_i)

            decls: dict[str, str] = {}
            for attr in attrs:
                if attr.name == "xmlns":
                    decls[""] = normalize_attribute_value(
                        text[attr.value_start : attr.value_end]
                    )
                elif attr.name.startswith("xmlns:"):
                    decls[attr.name.split(":", 1)[1]] = normalize_attribute_value(
                        text[attr.value_start : attr.value_end]
                    )
            scope = _Scope(scope_stack[-1] if scope_stack else None, decls)
            ns_uri, local = _resolve_qname(qname, scope)
            if local == "a" and ns_uri == XHTML_NS:
                for attr in attrs:
                    if ":" not in attr.name and attr.name == "href":
                        value = text[attr.value_start : attr.value_end]
                        anchors.append(
                            RawAnchor(
                                line=tag_line,
                                ns_uri=ns_uri,
                                qname=qname,
                                value_start=bmap.byte_offset(attr.value_start),
                                value_end=bmap.byte_offset(attr.value_end),
                                quote=attr.quote,
                                raw_value=normalize_attribute_value(value),
                            )
                        )
                        break
            if not self_closing:
                scope_stack.append(scope)
            i = new_i
            continue
        if text[i] == "\n":
            line += 1
        i += 1
    return anchors


# ---------------------------------------------------------------------------
# lxml 解析
# ---------------------------------------------------------------------------

_XHTML_ENTITIES = " ".join(
    f'<!ENTITY {name} "&#{cp};">'
    for name, cp in html.entities.name2codepoint.items()
)


class _EntityDTDResolver(etree.Resolver):
    """为显式引用外部 XHTML DTD 的文档提供合成 DTD（不访问网络）。"""

    def resolve(self, system_url, public_id, context):  # type: ignore[override]
        return self.resolve_string(_XHTML_ENTITIES, context)


def _doctype_end_bytes(data: bytes, start: int) -> int:
    """字节版：返回 <!DOCTYPE 声明结束 '>' 之后的位置。"""
    j = start
    n = len(data)
    bracket = 0
    quote = b""
    while j < n:
        ch = data[j : j + 1]
        if quote:
            if ch == quote:
                quote = b""
        elif ch in (b"'", b'"'):
            quote = ch
        elif ch == b"[":
            bracket += 1
        elif ch == b"]":
            bracket -= 1
        elif ch == b">" and bracket == 0:
            return j + 1
        j += 1
    raise ValueError("DOCTYPE 未闭合")


def inject_entities_for_parse(raw: bytes) -> tuple[bytes, int, int]:
    """把 XHTML 命名实体声明注入解析副本（不增加任何换行）。

    返回 (新字节, 注入点, 注入新行数=0)。原文字节永不修改；注入只让 lxml
    能展开 &nbsp; 等命名实体，后续元素行号与原文一致。
    """
    subset = ("[" + _XHTML_ENTITIES + "]").encode("ascii")
    m = re.search(rb"<!DOCTYPE\b", raw, re.IGNORECASE)
    if m:
        gt = _doctype_end_bytes(raw, m.start()) - 1
        open_bracket = raw.find(b"[", m.end(), gt)
        close_bracket = raw.rfind(b"]", m.end(), gt)
        if open_bracket != -1 and close_bracket != -1:
            inner = _XHTML_ENTITIES.encode("ascii") + b" "
            return (
                raw[: open_bracket + 1] + inner + raw[open_bracket + 1 :],
                open_bracket + 1,
                0,
            )
        return raw[:gt] + b" " + subset + b" " + raw[gt:], gt, 0

    pos = 0
    if raw.startswith(codecs.BOM_UTF8):
        pos = 3
    if raw[pos:pos + 5] == b"<?xml":
        end = raw.find(b"?>", pos + 5)
        if end != -1:
            pos = end + 2
    decl = b"<!DOCTYPE html " + subset + b">"
    return raw[:pos] + decl + raw[pos:], pos, 0


def make_parser() -> etree.XMLParser:
    parser = etree.XMLParser(
        attribute_defaults=False,
        dtd_validation=False,
        load_dtd=True,
        no_network=True,
        resolve_entities=True,
        huge_tree=False,
    )
    parser.resolvers.add(_EntityDTDResolver())
    return parser


def parse_xml(raw: bytes, file_label: str, parser: etree.XMLParser | None = None):
    """解析 XML，失败时抛出带行列号的 LocatedError。返回 (root, parser)。

    两遍策略：先按原文解析；仅当失败原因是"命名实体未定义"时注入实体
    子集重试。每次解析使用全新 parser，避免失败状态在文件间污染。
    """

    def _raise(exc: etree.XMLSyntaxError) -> None:
        ln, col = getattr(exc, "position", (None, None))
        msg = exc.msg or "XML 解析失败"
        raise LocatedError(
            f"XML 解析失败: {msg}", file=file_label, line=ln, column=col
        ) from exc

    first_parser = make_parser()
    try:
        root = etree.fromstring(raw, parser=first_parser)
    except etree.XMLSyntaxError as exc:
        msg = exc.msg or ""
        if "Entity" in msg and "not defined" in msg:
            parse_source, _at, _extra_lines = inject_entities_for_parse(raw)
            retry_parser = make_parser()
            try:
                root = etree.fromstring(parse_source, parser=retry_parser)
            except etree.XMLSyntaxError as exc2:
                _raise(exc2)
        else:
            _raise(exc)
    if root is None:
        raise LocatedError("XML 文档为空", file=file_label)
    return root, first_parser


def document_encoding(root) -> str:
    """取 lxml 探测到的声明编码，缺省按 UTF-8。"""
    enc = root.getroottree().docinfo.encoding
    if not enc:
        return "utf-8"
    try:
        codecs.lookup(enc)
    except LookupError:
        return "utf-8"
    return enc
