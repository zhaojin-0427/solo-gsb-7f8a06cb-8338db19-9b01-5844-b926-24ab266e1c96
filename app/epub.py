# -*- coding: utf-8 -*-
"""EPUB3 章节跳转修复核心逻辑。

- lxml 做语义解析（container/OPF/nav/XHTML 锚点与 id）。
- 自写 XML 字节扫描器，为每个 ``a[href]`` 属性值定位原文字节区间；
  导出时只替换该区间，其余字节（未选链接、正文、实体、注释、空白）
  逐字节保持不变。
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from itertools import zip_longest
from urllib.parse import quote, unquote, urlsplit

from lxml import etree

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_TOTAL_INFLATED = 50 * 1024 * 1024
MAX_XHTML_FILES = 30

OPF_NS = "http://www.idpf.org/2007/opf"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
XML_NS = "http://www.w3.org/XML/1998/namespace"

XHTML_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}

REASON_TEXT = {
    "external": "外部链接（不检查、不修改）",
    "ok": "跳转有效",
    "malformed": "href 含非法百分号序列",
    "empty_href_top": "空 href 等价指向本篇顶部（有效）",
    "out_of_root": "相对路径解析后越出包根目录",
    "target_not_in_manifest": "目标不是 manifest 中声明的 XHTML",
    "target_missing_file": "目标文件在包内不存在",
    "missing_id": "目标文件缺少所指片段 id",
    "empty_fragment_top": "空片段指向章节顶部（有效）",
}


class EpubError(Exception):
    """结构性致命错误。"""

    def __init__(self, code: str, message: str, file: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.file = file


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class Item:
    item_id: str
    name: str
    media_type: str
    properties: str = ""


@dataclass
class Span:
    start: int
    end: int  # 半开区间（不含引号）


@dataclass
class Link:
    uid: str
    file_name: str
    text: str
    href: str               # lxml 解码后的属性值
    span: Span | None       # 原文字节区间；None = 无法唯一定位
    byte_verified: bool
    source: str             # spine / nav / manifest
    external: bool
    status: str             # ok / broken
    reason: str
    target_file: str | None = None
    fragment: str | None = None

    def to_payload(self) -> dict:
        return {
            "id": self.uid,
            "file": self.file_name,
            "text": self.text,
            "href": self.href,
            "source": self.source,
            "external": self.external,
            "status": self.status,
            "reason": self.reason,
            "reasonText": REASON_TEXT.get(self.reason, self.reason),
            "targetFile": self.target_file,
            "fragment": self.fragment,
            "mappable": self.span is not None,
        }


@dataclass
class FileModel:
    name: str
    media_type: str
    in_spine: bool = False
    is_nav: bool = False
    title: str | None = None
    nav_label: str | None = None
    ids: list[str] = field(default_factory=list)
    ids_set: set[str] = field(default_factory=set)
    links: list[Link] = field(default_factory=list)


@dataclass
class EntryData:
    info: zipfile.ZipInfo
    data: bytes


@dataclass
class Book:
    entries: list[EntryData]
    entries_by_name: dict[str, EntryData]
    opf_name: str
    items: dict[str, Item]
    files: dict[str, FileModel]
    spine_names: list[str]
    nav_names: list[str]
    links: list[Link]
    faults: list[dict]


# ---------------------------------------------------------------------------
# XML 字节级词法扫描
# ---------------------------------------------------------------------------

_NAME_BREAK = frozenset(b" \t\r\n\f=/>\"'")
_WS = frozenset(b" \t\r\n\f")


@dataclass
class _Tag:
    name: bytes
    # [(属性名, (值起点, 值终点半开)), ...]，已剔除 xmlns 声明
    attrs: list


class _XmlLexError(Exception):
    pass


def _find_gt(data: bytes, pos: int) -> int:
    quote = -1
    while pos < len(data):
        c = data[pos]
        if quote >= 0:
            if c == quote:
                quote = -1
        elif c in (ord('"'), ord("'")):
            quote = c
        elif c == ord(">"):
            return pos
        pos += 1
    raise _XmlLexError("标记未闭合")


def _skip_decl(data: bytes, pos: int) -> int:
    if data.startswith(b"<!--", pos):
        end = data.find(b"-->", pos + 4)
        if end < 0:
            raise _XmlLexError("注释未闭合")
        return end + 3
    if data.startswith(b"<![CDATA[", pos):
        end = data.find(b"]]>", pos + 9)
        if end < 0:
            raise _XmlLexError("CDATA 未闭合")
        return end + 3
    if data[pos + 1:pos + 2] == b"?":
        end = data.find(b"?>", pos + 2)
        if end < 0:
            raise _XmlLexError("处理指令未闭合")
        return end + 2
    return _find_gt(data, pos + 2) + 1  # <!DOCTYPE ...>


def _parse_start_tag(data: bytes, start: int) -> tuple[_Tag, int]:
    n = len(data)
    pos = start + 1
    begin = pos
    while pos < n and data[pos] not in _NAME_BREAK and data[pos] != ord("<"):
        pos += 1
    if pos == begin:
        raise _XmlLexError("非法标签起始")
    name = data[begin:pos]

    attrs: list = []
    while True:
        while pos < n and data[pos] in _WS:
            pos += 1
        if pos >= n:
            raise _XmlLexError("开始标签未闭合")
        c = data[pos]
        if c == ord(">"):
            return _Tag(name, attrs), pos + 1
        if c == ord("/"):
            if pos + 1 >= n or data[pos + 1] != ord(">"):
                raise _XmlLexError("空元素标签缺少 >")
            return _Tag(name, attrs), pos + 2
        abegin = pos
        while pos < n and data[pos] not in _NAME_BREAK and data[pos] != ord("<"):
            pos += 1
        aname = data[abegin:pos]
        if not aname:
            raise _XmlLexError("属性名缺失")
        while pos < n and data[pos] in _WS:
            pos += 1
        if pos >= n or data[pos] != ord("="):
            raise _XmlLexError("属性缺少 =")
        pos += 1
        while pos < n and data[pos] in _WS:
            pos += 1
        if pos >= n or (data[pos] != ord('"') and data[pos] != ord("'")):
            raise _XmlLexError("属性值必须使用引号")
        q = data[pos]
        vstart = pos + 1
        vend = data.find(bytes([q]), vstart)
        if vend < 0:
            raise _XmlLexError("属性值引号未闭合")
        if aname != b"xmlns" and not aname.startswith(b"xmlns:"):
            attrs.append((aname, (vstart, vend)))
        pos = vend + 1


def scan_start_tags(data: bytes) -> list[_Tag]:
    """按文档顺序返回所有开始标签（含空元素标签），跳过注释/CDATA/PI/声明。"""
    tags: list[_Tag] = []
    n = len(data)
    pos = 0
    while True:
        lt = data.find(b"<", pos)
        if lt < 0:
            break
        c2 = data[lt + 1:lt + 2]
        if c2 in (b"!", b"?"):
            pos = _skip_decl(data, lt)
            continue
        if c2 == b"/":
            gt = data.find(b">", lt + 2)
            if gt < 0:
                raise _XmlLexError("结束标签未闭合")
            pos = gt + 1
            continue
        tag, pos = _parse_start_tag(data, lt)
        tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# DOCTYPE 内部实体 & 属性值解转义（仅用于字节定位校验）
# ---------------------------------------------------------------------------

_ENTITY_RE = re.compile(
    rb"""<!ENTITY\s+([A-Za-z_:][\w.\-:]*)\s+(['"])(.*?)\2\s*>""",
    re.DOTALL,
)
_PREDEFINED = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
_REF_RE = re.compile(r"&(#x?[0-9a-fA-F]+|[A-Za-z_:][\w.:-]*);")


def extract_internal_entities(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ENTITY_RE.finditer(data):
        name = m.group(1).decode("ascii")
        try:
            out[name] = m.group(3).decode("utf-8")
        except UnicodeDecodeError:
            out[name] = m.group(3).decode("utf-8", "replace")
    return out


def unescape_attr(raw: str, entities: dict[str, str]) -> tuple[str, bool]:
    ok = True

    def repl(m: re.Match) -> str:
        nonlocal ok
        ent = m.group(1)
        if ent.startswith("#"):
            try:
                if ent[1:2] in ("x", "X"):
                    return chr(int(ent[2:], 16))
                return chr(int(ent[1:]))
            except ValueError:
                ok = False
                return m.group(0)
        if ent in _PREDEFINED:
            return _PREDEFINED[ent]
        if ent in entities:
            return entities[ent]
        ok = False
        return m.group(0)

    return _REF_RE.sub(repl, raw), ok


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------


def localname(el) -> str:
    return etree.QName(el).localname


def make_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
        collect_ids=False,
    )


def parse_xml(data: bytes, file_name: str) -> etree._Element:
    try:
        root = etree.fromstring(data, parser=make_parser())
    except etree.XMLSyntaxError as exc:
        raise EpubError("xml_parse", f"XML 解析失败：{exc.msg}", file_name) from exc
    if root is None:
        raise EpubError("xml_parse", "XML 根元素为空", file_name)
    return root


def normalize_name(name: str) -> str:
    if "\\" in name:
        raise EpubError("invalid_path", f"路径含反斜杠：{name}", name)
    if name.startswith("/"):
        raise EpubError("invalid_path", f"路径不可以 / 开头：{name}", name)
    norm = posixpath.normpath(name)
    if norm in (".", "..") or norm.startswith("../"):
        raise EpubError("out_of_root", f"路径越出包根：{name}", name)
    return norm


def resolve_rel(base_name: str, rel_decoded: str) -> str | None:
    """相对 base 所在目录解析（rel 已解码一次）。越界 -> None。"""
    if rel_decoded.startswith("/"):
        return None
    base_dir = posixpath.dirname(base_name)
    joined = posixpath.join(base_dir, rel_decoded) if base_dir else rel_decoded
    norm = posixpath.normpath(joined)
    if norm in (".", "..") or norm.startswith("../") or norm.startswith("/"):
        return None
    return norm


_BAD_PCT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def strict_unquote(s: str) -> str:
    """百分号解码一次；非法 % 序列抛 ValueError。"""
    if _BAD_PCT_RE.search(s):
        raise ValueError("非法百分号编码")
    return unquote(s, errors="strict")


def split_href(href: str):
    """-> (path_decoded, fragment_decoded|None, has_fragment, external)。"""
    parts = urlsplit(href)
    if parts.scheme:
        return href, (parts.fragment if href.find("#") >= 0 else None), True, True
    path = parts.path
    if parts.query:
        path += "?" + parts.query
    has_frag = href.find("#") >= 0
    path_dec = strict_unquote(path)
    frag_dec = strict_unquote(parts.fragment) if has_frag else None
    return path_dec, frag_dec, has_frag, False


def text_of_anchor(el) -> str:
    text = " ".join(" ".join(el.itertext()).split())
    return text[:120]


# ---------------------------------------------------------------------------
# ZIP 读取与结构检查
# ---------------------------------------------------------------------------


def load_zip(data: bytes) -> list[EntryData]:
    if len(data) > MAX_UPLOAD_BYTES:
        raise EpubError(
            "too_large",
            f"ZIP 文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB 上限",
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise EpubError("bad_zip", "不是合法的 ZIP/EPUB 文件") from exc

    bad = zf.testzip()
    if bad is not None:
        raise EpubError("bad_zip", f"ZIP 条目损坏（CRC 失败）：{bad}")

    entries: list[EntryData] = []
    total = 0
    seen: set[str] = set()
    for info in zf.infolist():
        if info.flag_bits & 0x1:
            raise EpubError("encrypted", f"条目被加密：{info.filename}")
        raw = info.filename
        if raw.startswith("__MACOSX/"):
            continue
        name = normalize_name(raw)
        if name in seen:
            raise EpubError("duplicate_name", f"归一化后路径重名：{name}", name)
        seen.add(name)
        blob = zf.read(info)
        total += len(blob)
        if total > MAX_TOTAL_INFLATED:
            raise EpubError(
                "too_large",
                f"解压总大小超过 {MAX_TOTAL_INFLATED // 1024 // 1024} MB 上限",
            )
        entries.append(EntryData(info=info, data=blob))

    if not entries:
        raise EpubError("bad_zip", "ZIP 不包含任何条目")
    return entries


def check_mimetype_and_encryption(entries: list[EntryData]) -> list[dict]:
    """结构问题以 warning 形式报告（导出时 mimetype 会被纠正为首项 STORED）；
    加密问题无法安全处理，以 error 形式阻断。"""
    faults: list[dict] = []
    mt = next((e for e in entries if e.info.filename == "mimetype"), None)
    if mt is None:
        faults.append({"code": "mimetype_missing", "file": "mimetype",
                       "severity": "error",
                       "message": "缺少 mimetype 条目"})
    else:
        if entries[0].info.filename != "mimetype":
            faults.append({"code": "mimetype_not_first", "file": "mimetype",
                           "severity": "warning",
                           "message": "mimetype 不是 ZIP 首项（导出时将自动纠正为首项）"})
        if mt.info.compress_type != zipfile.ZIP_STORED:
            faults.append({"code": "mimetype_compressed", "file": "mimetype",
                           "severity": "warning",
                           "message": "mimetype 被压缩（导出时将自动改为不压缩）"})
        if mt.data != b"application/epub+zip":
            faults.append({"code": "mimetype_wrong", "file": "mimetype",
                           "severity": "error",
                           "message": "mimetype 内容必须是 application/epub+zip"})
    if any(e.info.filename == "META-INF/encryption.xml" for e in entries):
        faults.append({
            "code": "encrypted",
            "file": "META-INF/encryption.xml",
            "severity": "error",
            "message": "检测到 encryption.xml，EPUB 含加密内容，本工具不支持",
        })
    return faults


def find_rootfile(by_name: dict[str, EntryData]) -> str:
    container = by_name.get("META-INF/container.xml")
    if container is None:
        raise EpubError(
            "container_missing",
            "缺少 META-INF/container.xml，无法定位 OPF",
            "META-INF/container.xml",
        )
    root = parse_xml(container.data, "META-INF/container.xml")
    candidates = []
    for rf in root.iter():
        if not isinstance(rf.tag, str) or localname(rf) != "rootfile":
            continue
        full = rf.get("full-path")
        if not full:
            continue
        mt = rf.get("media-type")
        if mt and mt != "application/oebps-package+xml":
            continue
        candidates.append(full)
    if not candidates:
        raise EpubError(
            "container_missing",
            "container.xml 中没有指向 OPF 的 rootfile",
            "META-INF/container.xml",
        )
    return normalize_name(candidates[0])


# ---------------------------------------------------------------------------
# OPF
# ---------------------------------------------------------------------------


def parse_opf(by_name: dict[str, EntryData]):
    opf_name = find_rootfile(by_name)
    opf_entry = by_name.get(opf_name)
    if opf_entry is None:
        raise EpubError("missing_file", f"container 指向的 OPF 不存在：{opf_name}", opf_name)
    root = parse_xml(opf_entry.data, opf_name)

    items: dict[str, Item] = {}
    name_to_id: dict[str, str] = {}
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if etree.QName(el).namespace != OPF_NS or localname(el) != "item":
            continue
        item_id = el.get("id")
        href = el.get("href")
        media = el.get("media-type")
        if not item_id:
            raise EpubError("invalid_opf", "manifest item 缺少 id", opf_name)
        if not href:
            raise EpubError("invalid_opf", f"item '{item_id}' 缺少 href", opf_name)
        if not media:
            raise EpubError("invalid_opf", f"item '{item_id}' 缺少 media-type", opf_name)
        if item_id in items:
            raise EpubError("invalid_opf", f"manifest item id 重复：{item_id}", opf_name)
        try:
            href_dec = strict_unquote(href)
        except ValueError:
            raise EpubError("malformed_href",
                            f"item '{item_id}' href 含非法百分号序列", opf_name)
        name = resolve_rel(opf_name, href_dec)
        if name is None:
            raise EpubError("out_of_root",
                            f"item '{item_id}' href 越出包根：{href}", opf_name)
        if name in name_to_id:
            raise EpubError("duplicate_name",
                            f"manifest 归一化路径重名：{name}", name)
        if name not in by_name:
            raise EpubError("missing_file",
                            f"manifest 引用的文件不存在：{name}", name)
        name_to_id[name] = item_id
        items[item_id] = Item(item_id, name, media, el.get("properties", "") or "")

    spine_names: list[str] = []
    saw_spine = False
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if etree.QName(el).namespace != OPF_NS or localname(el) != "itemref":
            continue
        saw_spine = True
        ref = el.get("idref")
        if not ref:
            raise EpubError("invalid_opf", "spine itemref 缺少 idref", opf_name)
        if ref not in items:
            raise EpubError("spine_broken",
                            f"spine 引用了不存在的 manifest id：{ref}", opf_name)
        spine_names.append(items[ref].name)
    if not saw_spine:
        raise EpubError("invalid_opf", "OPF 缺少 spine", opf_name)

    for name in spine_names:
        iid = name_to_id[name]
        if items[iid].media_type not in XHTML_MEDIA_TYPES:
            raise EpubError("invalid_spine",
                            f"spine 项不是 XHTML：{name}（{items[iid].media_type}）",
                            name)

    nav_items = [it for it in items.values() if "nav" in it.properties.split()]
    if not nav_items:
        raise EpubError("nav_missing",
                        "manifest 未声明 properties=\"nav\" 的 EPUB3 导航文档",
                        opf_name)
    nav_names: list[str] = []
    for it in nav_items:
        if it.media_type not in XHTML_MEDIA_TYPES:
            raise EpubError("invalid_nav",
                            f"nav 不是 XHTML：{it.name}（{it.media_type}）", it.name)
        nav_names.append(it.name)

    xhtml_count = sum(1 for it in items.values() if it.media_type in XHTML_MEDIA_TYPES)
    if xhtml_count > MAX_XHTML_FILES:
        raise EpubError(
            "too_many_xhtml",
            f"manifest 中 XHTML 共 {xhtml_count} 篇，超过 {MAX_XHTML_FILES} 篇上限",
        )

    return opf_name, items, spine_names, nav_names


# ---------------------------------------------------------------------------
# nav
# ---------------------------------------------------------------------------


def extract_nav(by_name: dict[str, EntryData], nav_name: str):
    """返回 (目标路径 -> 链接文字)。"""
    root = parse_xml(by_name[nav_name].data, nav_name)
    navs = [
        el for el in root.iter()
        if isinstance(el.tag, str)
        and etree.QName(el).namespace == XHTML_NS
        and localname(el) == "nav"
    ]
    toc = next(
        (n for n in navs if "toc" in (n.get("{%s}type" % EPUB_NS) or "").split()),
        navs[0] if navs else None,
    )
    labels: dict[str, str] = {}
    if toc is None:
        return labels
    for a in toc.iter("{%s}a" % XHTML_NS):
        href = a.get("href")
        if not href:
            continue
        try:
            path_dec, _, _, external = split_href(href)
        except ValueError:
            continue
        if external:
            continue
        target = nav_name if path_dec == "" else resolve_rel(nav_name, path_dec)
        if target is None or target in labels:
            continue
        label = text_of_anchor(a)
        if label:
            labels[target] = label
    return labels


def extract_title(root) -> str | None:
    for el in root.iter("{%s}title" % XHTML_NS):
        t = " ".join(" ".join(el.itertext()).split())
        if t:
            return t[:120]
    return None


# ---------------------------------------------------------------------------
# 锚点字节定位
# ---------------------------------------------------------------------------


def _attr_matches(clark_key: str, aname: bytes, encoding: str) -> bool:
    if clark_key.startswith("{%s}" % XML_NS):
        return aname.decode(encoding, "replace") == "xml:" + clark_key.rsplit("}", 1)[1]
    if clark_key.startswith("{"):
        local = clark_key.rsplit("}", 1)[1]
        text = aname.decode(encoding, "replace")
        return text == local or text.split(":", 1)[-1] == local
    return aname.decode(encoding, "replace") == clark_key


def href_spans_for_file(data: bytes, root, encoding: str, file_name: str
                        ) -> tuple[list[tuple[etree._Element, Span, bool]], bool]:
    """对齐 lxml 元素与字节标签。

    返回 ([(a 元素, Span, 字节校验通过), ...], 是否整体对齐成功)。
    列表按文档顺序，仅包含 XHTML 命名空间下带 href 的 a 元素。
    注意：lxml 的元素代理是短生命周期对象，不能用 id() 跨调用做键。
    """
    try:
        tags = scan_start_tags(data)
    except _XmlLexError:
        # lxml 能解析而扫描器失败，说明扫描器无法唯一对应属性
        return [], False

    elems = [el for el in root.iter() if isinstance(el.tag, str)]
    if len(tags) != len(elems):
        return [], False

    ents = extract_internal_entities(data)
    anchors: list[tuple[etree._Element, Span, bool]] = []
    for idx, (el, tag) in enumerate(zip(elems, tags)):
        q = etree.QName(el)
        if tag.name.split(b":")[-1].decode(encoding, "replace") != q.localname:
            return [], False
        attrs = list(el.attrib.items())
        if len(attrs) != len(tag.attrs):
            return [], False
        for (key, val), (aname, (vs, ve)) in zip(attrs, tag.attrs):
            if not _attr_matches(key, aname, encoding):
                return [], False
            # 注意：无前缀属性不受默认命名空间影响，lxml 中通常为裸 "href"
            is_href = key == "href" or key == "{%s}href" % XHTML_NS
            if not is_href:
                continue
            if q.namespace != XHTML_NS or q.localname != "a":
                continue
            raw = data[vs:ve].decode(encoding, "replace")
            decoded, refs_ok = unescape_attr(raw, ents)
            anchors.append((el, Span(vs, ve), decoded == val and refs_ok))
    return anchors, True


# ---------------------------------------------------------------------------
# 链接分类
# ---------------------------------------------------------------------------


def classify(href: str, file_name: str, files: dict[str, FileModel],
             by_name: dict[str, EntryData]) -> dict:
    if href == "":
        # 浏览器语义：href="" 以当前文档 URL 为基准，等价指向本篇顶部
        return dict(external=False, status="ok", reason="empty_href_top",
                    target_file=file_name, fragment="")
    try:
        path_dec, frag_dec, has_frag, external = split_href(href)
    except ValueError:
        return dict(external=False, status="broken", reason="malformed",
                    target_file=None, fragment=None)
    if external:
        return dict(external=True, status="ok", reason="external",
                    target_file=None, fragment=frag_dec)

    if path_dec.startswith("/"):
        return dict(external=False, status="broken", reason="out_of_root",
                    target_file=None, fragment=frag_dec)

    if path_dec == "":
        target = file_name
    else:
        target = resolve_rel(file_name, path_dec)
        if target is None:
            return dict(external=False, status="broken", reason="out_of_root",
                        target_file=None, fragment=frag_dec)

    if target not in by_name:
        return dict(external=False, status="broken", reason="target_missing_file",
                    target_file=target, fragment=frag_dec)

    if target not in files:
        return dict(external=False, status="broken",
                    reason="target_not_in_manifest",
                    target_file=target, fragment=frag_dec)

    if not has_frag:
        return dict(external=False, status="ok", reason="ok",
                    target_file=target, fragment=None)
    if frag_dec == "":
        return dict(external=False, status="ok", reason="empty_fragment_top",
                    target_file=target, fragment="")
    if frag_dec in files[target].ids_set:
        return dict(external=False, status="ok", reason="ok",
                    target_file=target, fragment=frag_dec)
    return dict(external=False, status="broken", reason="missing_id",
                target_file=target, fragment=frag_dec)


# ---------------------------------------------------------------------------
# 整体解析
# ---------------------------------------------------------------------------


def parse_book(data: bytes) -> Book:
    entries = load_zip(data)
    by_name = {e.info.filename: e for e in entries}
    faults = check_mimetype_and_encryption(entries)

    opf_name, items, spine_names, nav_names = parse_opf(by_name)

    xhtml_items = sorted(
        [it for it in items.values() if it.media_type in XHTML_MEDIA_TYPES],
        key=lambda it: it.name,
    )

    nav_labels: dict[str, str] = {}
    for name in nav_names:
        for k, v in extract_nav(by_name, name).items():
            nav_labels.setdefault(k, v)

    # 第一遍：解析、收集 id（含重复检测）
    files: dict[str, FileModel] = {}
    roots: dict[str, etree._Element] = {}
    encodings: dict[str, str] = {}
    for it in xhtml_items:
        root = parse_xml(by_name[it.name].data, it.name)
        roots[it.name] = root
        encodings[it.name] = root.getroottree().docinfo.encoding or "utf-8"

        seen: dict[str, int] = {}
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            vals = [el.get("id"), el.get("{%s}id" % XML_NS)]
            line = getattr(el, "sourceline", 0) or 0
            for val in vals:
                if val is None:
                    continue
                if val in seen:
                    faults.append({
                        "code": "duplicate_id",
                        "file": it.name,
                        "severity": "warning",
                        "message": (
                            f"同篇重复 id：'{val}'（第 {seen[val]} 行与第 {line} 行）"
                        ),
                    })
                else:
                    seen[val] = line
        fm = FileModel(
            name=it.name,
            media_type=it.media_type,
            in_spine=it.name in spine_names,
            is_nav=it.name in nav_names,
            title=extract_title(root),
            nav_label=nav_labels.get(it.name),
            ids=list(seen.keys()),
            ids_set=set(seen.keys()),
        )
        files[it.name] = fm

    # 第二遍：扫描链接
    all_links: list[Link] = []
    counter = 0
    for it in xhtml_items:
        data = by_name[it.name].data
        root = roots[it.name]
        encoding = encodings[it.name]
        fm = files[it.name]

        span_map, aligned = href_spans_for_file(data, root, encoding, it.name)
        if not aligned:
            faults.append({
                "code": "unmappable",
                "file": it.name,
                "severity": "error",
                "message": "字节扫描无法与解析结果一一对应，本文件所有链接均无法唯一定位",
            })

        source = "nav" if fm.is_nav else ("spine" if fm.in_spine else "manifest")
        used: set[tuple[int, int]] = set()

        anchors_iter = list(root.iter("{%s}a" % XHTML_NS))
        anchors_iter = [el for el in anchors_iter if el.get("href") is not None]

        if aligned and len(anchors_iter) != len(span_map):
            aligned = False
            span_map = []
            faults.append({
                "code": "unmappable",
                "file": it.name,
                "severity": "error",
                "message": "a[href] 元素数量与字节扫描结果不一致，无法唯一定位",
            })

        for el, span_info in zip_longest(anchors_iter, span_map):
            href = el.get("href")
            counter += 1
            uid = f"L{counter}"

            span: Span | None = None
            byte_ok = False
            if aligned and span_info is not None:
                _, cand, byte_ok = span_info
                key = (cand.start, cand.end)
                if key not in used:
                    used.add(key)
                    span = cand

            cls = classify(href, it.name, files, by_name)
            link = Link(
                uid=uid,
                file_name=it.name,
                text=text_of_anchor(el),
                href=href,
                span=span,
                byte_verified=byte_ok,
                source=source,
                external=cls["external"],
                status=cls["status"],
                reason=cls["reason"],
                target_file=cls["target_file"],
                fragment=cls["fragment"],
            )

            # 失效链接若无法唯一定位，逐条指出并禁止导出
            if not link.external and link.status == "broken":
                if not aligned:
                    faults.append({
                        "code": "unmappable",
                        "file": it.name,
                        "severity": "error",
                        "linkId": uid,
                        "message": (
                            f"失效链接无法对应原文属性位置：文字「{link.text}」"
                            f"href=\"{href}\""
                        ),
                    })
                elif span is None:
                    faults.append({
                        "code": "unmappable",
                        "file": it.name,
                        "severity": "error",
                        "linkId": uid,
                        "message": (
                            f"失效链接属性位置不唯一：文字「{link.text}」"
                            f"href=\"{href}\""
                        ),
                    })
                elif not byte_ok:
                    faults.append({
                        "code": "unmappable",
                        "file": it.name,
                        "severity": "error",
                        "linkId": uid,
                        "message": (
                            f"失效链接属性值含无法核对的原始字节（可能使用未声明实体）："
                            f"href=\"{href}\""
                        ),
                    })
                    link.span = None

            fm.links.append(link)
            all_links.append(link)

    return Book(
        entries=entries,
        entries_by_name=by_name,
        opf_name=opf_name,
        items=items,
        files=files,
        spine_names=spine_names,
        nav_names=nav_names,
        links=all_links,
        faults=faults,
    )


# ---------------------------------------------------------------------------
# 新 href 编码与导出
# ---------------------------------------------------------------------------


def encode_relpath(rel: str) -> str:
    return "/".join(quote(seg, safe="!$&'()*+,;=:@") for seg in rel.split("/"))


def encode_fragment(frag: str) -> str:
    # 片段内不允许裸 #；其余 RFC3986 sub-delims / ":" / "@" / "/" / "?" 保留
    return quote(frag, safe="-._~!$&'()*+,;=:@/?")


def build_href(source_file: str, target_file: str, fragment: str | None) -> str:
    if source_file == target_file:
        path_part = ""
    else:
        base_dir = posixpath.dirname(source_file)
        rel = posixpath.relpath(target_file, base_dir or ".")
        path_part = encode_relpath(rel)
    if fragment is None:
        return path_part
    return f"{path_part}#{encode_fragment(fragment)}"


def validate_decisions(book: Book, decisions: dict[str, dict]) -> None:
    link_ids = {l.uid for l in book.links}
    for uid, dec in decisions.items():
        if uid not in link_ids:
            raise EpubError("invalid_decision", f"裁决引用了不存在的链接 id：{uid}")
        target = dec.get("targetFile")
        fragment = dec.get("fragment", "__missing__")
        if target not in book.files:
            raise EpubError("invalid_decision",
                            f"裁决目标不是 manifest 中的 XHTML：{target}", target)
        if fragment == "__missing__":
            raise EpubError("invalid_decision", f"裁决缺少 fragment：{uid}", target)
        if fragment not in ("",) and fragment not in book.files[target].ids_set:
            # 空串 = 章节顶部
            raise EpubError("invalid_decision",
                            f"裁决目标 id 不存在：{target}#{fragment}", target)


def apply_decisions(book: Book, decisions: dict[str, dict]) -> dict[str, bytes]:
    """生成 修改文件名 -> 新字节（只替换所选 href 属性值区间）。"""
    edits_by_file: dict[str, list] = {}
    for link in book.links:
        dec = decisions.get(link.uid)
        if not dec:
            continue
        if link.span is None or not link.byte_verified:
            raise EpubError(
                "unmappable",
                f"该链接无法唯一定位原文字节，禁止导出：{link.file_name} "
                f"href=\"{link.href}\"",
                link.file_name,
            )
        new_href = build_href(link.file_name, dec["targetFile"], dec["fragment"])
        edits_by_file.setdefault(link.file_name, []).append((link, new_href))

    changed: dict[str, bytes] = {}
    for file_name, edits in edits_by_file.items():
        data = book.entries_by_name[file_name].data
        spans = sorted((l.span for l, _ in edits), key=lambda s: s.start)
        # 区间重叠检查
        for a, b in zip(spans, spans[1:]):
            if a.end > b.start:
                raise EpubError("unmappable",
                                f"链接属性区间重叠，无法安全替换：{file_name}", file_name)
        out = bytearray(data)
        for link, new_href in sorted(edits, key=lambda x: x[0].span.start, reverse=True):
            s = link.span
            out[s.start:s.end] = new_href.encode("utf-8")
        changed[file_name] = bytes(out)
    return changed


def repackage(book: Book, changed: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        # mimetype 必须首项且 STORED：若原包顺序不对，这里自动纠正（其余条目
        # 的相对顺序、压缩方式与内容保持不变）。
        ordered = list(book.entries)
        mt_idx = next(
            (i for i, e in enumerate(ordered) if e.info.filename == "mimetype"), None
        )
        if mt_idx is not None and mt_idx != 0:
            mt_entry = ordered.pop(mt_idx)
            ordered.insert(0, mt_entry)

        for idx, entry in enumerate(ordered):
            name = entry.info.filename
            old = entry.info
            data = changed.get(name, entry.data)
            zi = zipfile.ZipInfo(filename=name, date_time=old.date_time)
            zi.external_attr = old.external_attr
            zi.create_system = old.create_system
            zi.comment = old.comment
            zi.extra = old.extra
            if name == "mimetype":
                zi.compress_type = zipfile.ZIP_STORED
                zf.writestr(zi, data, compress_type=zipfile.ZIP_STORED)
            else:
                zi.compress_type = old.compress_type
                zf.writestr(zi, data, compress_type=old.compress_type)
    return out.getvalue()


def rescan(epub_bytes: bytes):
    """重扫成品：返回 (error_faults, broken_links, all_faults)。

    下载闸口只看 error_faults 与 broken_links；warning（如重打包已纠正
    的 mimetype 项、原书即存在且未被本次修复触及的重复 id 提示）不阻断。
    """
    book = parse_book(epub_bytes)
    broken = [
        l.to_payload() for l in book.links
        if not l.external and l.status == "broken"
    ]
    errors = [f for f in book.faults if f.get("severity", "error") == "error"]
    return errors, broken, book.faults
