"""EPUB 结构解析与失效链接扫描。

流程：ZIP 校验 -> container.xml -> OPF -> manifest/spine/nav ->
逐篇 XHTML 扫描 ``a[href]``，按"相对所在文件解析、路径与片段各解码一次、
大小写敏感、空片段指向章节顶部"判定失效。
"""

from __future__ import annotations

import io
import posixpath
import unicodedata
import zipfile
from dataclasses import dataclass, field

from lxml import etree

from .errors import LocatedError, StructureErrors, UploadError
from .urltools import (
    is_external,
    percent_decode_once,
    resolve_relative,
    same_document,
    split_href,
)
from .xmlscan import (
    RawAnchor,
    document_encoding,
    parse_xml,
    scan_anchors,
)

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
XHTML_NS = "http://www.w3.org/1999/xhtml"
XML_NS = "http://www.w3.org/XML/1998/namespace"
ENC_NS = "http://www.w3.org/2001/04/xmlenc#"

MAX_UPLOAD = 20 * 1024 * 1024
MAX_UNCOMPRESSED = 50 * 1024 * 1024
MAX_XHTML = 30
XHTML_EXTS = (".xhtml", ".xht")
XHTML_MEDIA = "application/xhtml+xml"


def _localname(el: etree._Element) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag[tag.rfind("}") + 1 :]


def _namespace(el: etree._Element) -> str:
    tag = el.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1 : tag.find("}")]
    return ""


@dataclass
class ManifestItem:
    item_id: str
    href: str           # 归一化 ZIP 路径
    raw_href: str       # OPF 中原始 href
    media_type: str
    properties: set[str]


@dataclass
class Chapter:
    path: str
    title: str
    in_spine: bool
    ids: list[dict] = field(default_factory=list)


@dataclass
class LinkRecord:
    uid: int
    file: str
    line: int
    text: str
    raw_href: str
    external: bool
    broken: bool
    reason: str | None
    target_file: str | None
    fragment: str | None       # None=无片段；""=空片段（顶部）
    locatable: bool
    value_start: int
    value_end: int
    quote: str

    def to_dict(self) -> dict:
        return {
            "id": self.uid,
            "file": self.file,
            "line": self.line,
            "text": self.text,
            "href": self.raw_href,
            "broken": self.broken,
            "reason": self.reason,
            "target_file": self.target_file,
            "fragment": self.fragment,
            "locatable": self.locatable,
            "external": self.external,
            "value_start": self.value_start if self.locatable else None,
            "value_end": self.value_end if self.locatable else None,
            "quote": self.quote if self.locatable else None,
        }


@dataclass
class _DocBundle:
    path: str
    raw: bytes
    root: etree._Element
    anchors: list[RawAnchor]
    ids: list[dict]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def analyze_epub(data: bytes) -> dict:
    """完整分析一本 EPUB，返回供前端使用的 JSON 结构。

    致命结构错误以 StructureErrors 一次性抛出（可含多条）。
    """
    if len(data) > MAX_UPLOAD:
        raise UploadError(
            f"EPUB 超过 20 MB 上限（实际 {len(data) / 1024 / 1024:.1f} MB）"
        )

    errors: list[LocatedError] = []
    zf = _open_zip(data, errors)

    try:
        entries = _check_entries(zf, errors)
        if errors:
            raise StructureErrors(errors)

        opf_path = _read_container(zf, errors)
        opf_raw = zf.read(opf_path)
        opf_root, _ = parse_xml(opf_raw, opf_path)
        items, id_to_item, spine_paths = _read_opf(
            zf, opf_root, opf_path, entries, errors
        )
        if errors:
            raise StructureErrors(errors)

        xhtml_items = [it for it in items if _is_xhtml_item(it)]
        if len(xhtml_items) > MAX_XHTML:
            raise UploadError(
                f"XHTML 篇数超过 30 上限（manifest 内 {len(xhtml_items)} 篇）"
            )

        nav_path = _nav_path(id_to_item, errors)
        if errors:
            raise StructureErrors(errors)

        nav_labels = _read_nav_labels(zf, nav_path)

        docs = _parse_documents(zf, xhtml_items, errors)
        if errors:
            raise StructureErrors(errors)

        chapters = _build_chapters(docs, spine_paths, nav_labels)
        links, stats = _scan_links(docs, id_to_item, set(entries))

        result = {
            "chapters": [
                {
                    "path": c.path,
                    "title": c.title,
                    "in_spine": c.in_spine,
                    "ids": c.ids,
                }
                for c in chapters
            ],
            "links": [lk.to_dict() for lk in links],
            "stats": stats,
            "nav_path": nav_path,
            "opf_path": opf_path,
            "encodings": {p: document_encoding(d.root) for p, d in docs.items()},
        }
        return result
    finally:
        zf.close()


# ---------------------------------------------------------------------------
# ZIP / OCF
# ---------------------------------------------------------------------------

def _open_zip(data: bytes, errors: list[LocatedError]) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadError(f"不是合法的 ZIP/EPUB 文件: {exc}") from exc
    bad = zf.testzip()
    if bad is not None:
        errors.append(LocatedError(f"ZIP 条目 CRC 校验失败: {bad}", bad))
    return zf


def _check_entries(
    zf: zipfile.ZipFile, errors: list[LocatedError]
) -> list[str]:
    infos = zf.infolist()
    if not infos:
        errors.append(LocatedError("ZIP 为空"))
        return []

    # mimetype 必须是首项且不压缩
    first = infos[0]
    if first.filename != "mimetype":
        errors.append(
            LocatedError("mimetype 必须是 ZIP 的第一个条目", first.filename)
        )
    if first.compress_type != zipfile.ZIP_STORED:
        errors.append(LocatedError("mimetype 必须以不压缩方式存储", "mimetype"))
    try:
        mt = zf.read("mimetype")
        if mt != b"application/epub+zip":
            errors.append(
                LocatedError(
                    f"mimetype 内容应为 application/epub+zip，实为 {mt[:40]!r}",
                    "mimetype",
                )
            )
    except KeyError:
        errors.append(LocatedError("缺少 mimetype 条目", "mimetype"))

    # 加密检测
    if "META-INF/encryption.xml" in zf.namelist():
        try:
            er, _ = parse_xml(zf.read("META-INF/encryption.xml"),
                              "META-INF/encryption.xml")
            if er.findall(f".//{{{ENC_NS}}}EncryptedData"):
                raise UploadError("检测到 META-INF/encryption.xml，加密 EPUB 不受支持")
        except LocatedError:
            errors.append(
                LocatedError("encryption.xml 无法解析",
                             "META-INF/encryption.xml")
            )

    total = 0
    names: list[str] = []
    seen_norm: dict[str, str] = {}
    for info in infos:
        name = info.filename
        if name.endswith("/"):
            continue  # 目录条目
        total += info.file_size
        if total > MAX_UNCOMPRESSED:
            raise UploadError("解压后总大小超过 50 MB 上限")
        _validate_entry_name(name, errors)
        names.append(name)
        norm = unicodedata.normalize("NFC", name)
        if norm in seen_norm:
            if seen_norm[norm] != name:
                errors.append(
                    LocatedError(
                        f"归一化后路径重名: {name!r} 与 {seen_norm[norm]!r}", name
                    )
                )
        else:
            seen_norm[norm] = name
    return names


def _validate_entry_name(name: str, errors: list[LocatedError]) -> None:
    if name.startswith("/"):
        errors.append(LocatedError("ZIP 条目名不能以 / 开头", name))
    if "\\" in name:
        errors.append(LocatedError("ZIP 条目名不能包含反斜杠", name))
    if posixpath.splitdrive(name)[0]:
        errors.append(LocatedError("ZIP 条目名不能含盘符", name))
    parts = name.split("/")
    if any(p == ".." for p in parts):
        errors.append(LocatedError("ZIP 条目名不能包含 .. 段", name))


def _read_container(zf: zipfile.ZipFile, errors: list[LocatedError]) -> str:
    path = "META-INF/container.xml"
    if path not in zf.namelist():
        errors.append(LocatedError("缺少 META-INF/container.xml", path))
        raise StructureErrors(errors)
    root, _ = parse_xml(zf.read(path), path)
    rootfiles = root.findall(f".//{{{CONTAINER_NS}}}rootfile")
    if not rootfiles:
        errors.append(LocatedError("container.xml 中没有 rootfile", path))
        raise StructureErrors(errors)
    chosen = None
    for rf in rootfiles:
        mt = (rf.get("media-type") or "").lower()
        if mt in ("application/oebps-package+xml", ""):
            chosen = rf.get("full-path")
            break
    if not chosen:
        errors.append(
            LocatedError("container.xml 中找不到 OPF 类型的 rootfile", path)
        )
        raise StructureErrors(errors)
    opf = unicodedata.normalize("NFC", chosen.lstrip("/"))
    if opf not in zf.namelist():
        errors.append(
            LocatedError(f"container.xml 指向的 OPF 不存在: {chosen}", path)
        )
        raise StructureErrors(errors)
    return opf


# ---------------------------------------------------------------------------
# OPF / manifest / spine / nav
# ---------------------------------------------------------------------------

def _read_opf(
    zf: zipfile.ZipFile,
    root: etree._Element,
    opf_path: str,
    entries: list[str],
    errors: list[LocatedError],
) -> tuple[list[ManifestItem], dict[str, ManifestItem], list[str]]:
    base_dir = posixpath.dirname(opf_path)
    manifest_el = root.find(f"{{{OPF_NS}}}manifest")
    spine_el = root.find(f"{{{OPF_NS}}}spine")
    if manifest_el is None:
        errors.append(LocatedError("OPF 缺少 manifest", opf_path))
    if spine_el is None:
        errors.append(LocatedError("OPF 缺少 spine", opf_path))
    if errors:
        return [], {}, []

    items: list[ManifestItem] = []
    id_to_item: dict[str, ManifestItem] = {}
    paths_seen: dict[str, str] = {}
    entry_set = set(entries)

    for el in manifest_el.findall(f"{{{OPF_NS}}}item"):
        item_id = el.get("id")
        raw_href = el.get("href")
        media_type = (el.get("media-type") or "").strip().lower()
        props = set((el.get("properties") or "").split())
        if not item_id or not raw_href:
            errors.append(
                LocatedError(
                    f"manifest item 缺少 id 或 href (id={item_id!r}, "
                    f"href={raw_href!r})",
                    opf_path,
                    el.sourceline,
                )
            )
            continue
        if item_id in id_to_item:
            errors.append(
                LocatedError(f"manifest id 重复: {item_id}", opf_path,
                             el.sourceline)
            )
            continue
        path_part, _frag = split_href(raw_href)
        try:
            norm = resolve_relative(base_dir, path_part)
        except ValueError:
            errors.append(
                LocatedError(
                    f"manifest href 相对路径越界: {raw_href}",
                    opf_path, el.sourceline,
                )
            )
            continue
        if norm in paths_seen:
            errors.append(
                LocatedError(
                    f"manifest 条目归一化路径重名: {raw_href} 与 "
                    f"{paths_seen[norm]} 均指向 {norm}",
                    opf_path, el.sourceline,
                )
            )
            continue
        if norm not in entry_set:
            errors.append(
                LocatedError(
                    f"manifest 引用了包内不存在的文件: {raw_href}",
                    opf_path, el.sourceline,
                )
            )
            continue
        paths_seen[norm] = raw_href
        item = ManifestItem(item_id, norm, raw_href, media_type, props)
        items.append(item)
        id_to_item[item_id] = item

    spine_paths: list[str] = []
    for ir in spine_el.findall(f"{{{OPF_NS}}}itemref"):
        ref = ir.get("idref")
        if ref not in id_to_item:
            errors.append(
                LocatedError(
                    f"spine itemref 引用了不存在的 manifest id: {ref}",
                    opf_path, ir.sourceline,
                )
            )
            continue
        item = id_to_item[ref]
        if not _is_xhtml_item(item):
            errors.append(
                LocatedError(
                    f"spine 条目 {ref} 媒体类型不是 XHTML: {item.media_type}",
                    opf_path, ir.sourceline,
                )
            )
            continue
        if item.href not in spine_paths:
            spine_paths.append(item.href)

    return items, id_to_item, spine_paths


def _nav_path(
    id_to_item: dict[str, ManifestItem], errors: list[LocatedError]
) -> str:
    navs = [it for it in id_to_item.values() if "nav" in it.properties]
    if not navs:
        errors.append(LocatedError("EPUB3 必须有一个 properties=\"nav\" 的目录文档"))
        return ""
    if not _is_xhtml_item(navs[0]):
        errors.append(
            LocatedError("nav 文档媒体类型不是 XHTML", navs[0].href)
        )
    return navs[0].href


def _is_xhtml_item(item: ManifestItem) -> bool:
    return item.media_type == XHTML_MEDIA


def _looks_like_xhtml_path(path: str) -> bool:
    return path.lower().endswith(XHTML_EXTS)


def _read_nav_labels(zf: zipfile.ZipFile, nav_path: str) -> dict[str, str]:
    """从 nav.xhtml 提取 章节路径 -> 目录文字 的映射（取第一个匹配）。"""
    labels: dict[str, str] = {}
    if not nav_path:
        return labels
    try:
        root, _ = parse_xml(zf.read(nav_path), nav_path)
    except LocatedError:
        return labels
    base_dir = posixpath.dirname(nav_path)
    for a in root.iter(f"{{{XHTML_NS}}}a"):
        href = a.get("href")
        if not href:
            continue
        if is_external(href):
            continue
        path_part, _frag = split_href(href)
        if path_part.strip() == "":
            continue
        try:
            norm = resolve_relative(base_dir, path_part)
        except ValueError:
            continue
        text = _element_text(a)
        if text and norm not in labels:
            labels[norm] = text
    return labels


# ---------------------------------------------------------------------------
# XHTML 文档
# ---------------------------------------------------------------------------

def _parse_documents(
    zf: zipfile.ZipFile,
    xhtml_items: list[ManifestItem],
    errors: list[LocatedError],
) -> dict[str, _DocBundle]:
    docs: dict[str, _DocBundle] = {}
    for item in xhtml_items:
        path = item.href
        raw = zf.read(path)
        try:
            root, _parser = parse_xml(raw, path)
        except LocatedError as exc:
            errors.append(exc)
            continue
        encoding = document_encoding(root)
        try:
            anchors = scan_anchors(raw, encoding)
        except LocatedError as exc:
            exc.file = path
            errors.append(exc)
            anchors = []
        ids = _collect_ids(root, path, errors)
        docs[path] = _DocBundle(path, raw, root, anchors, ids)
    return docs


def _collect_ids(
    root: etree._Element, path: str, errors: list[LocatedError]
) -> list[dict]:
    found: dict[str, int] = {}
    out: list[dict] = []
    for el in root.iter():
        val = el.get("id")
        if val is None:
            val = el.get(f"{{{XML_NS}}}id")
        if val is None or val == "":
            continue
        line = el.sourceline
        if val in found:
            errors.append(
                LocatedError(
                    f'同篇重复 id: {val!r}（首次出现于第 {found[val]} 行）',
                    path, line,
                )
            )
            continue
        found[val] = line
        local = _localname(el)
        out.append(
            {
                "id": val,
                "line": line,
                "tag": local,
                "snippet": _id_snippet(el),
            }
        )
    return out


def _id_snippet(el: etree._Element) -> str:
    text = _element_text(el)
    return text[:120]


def _build_chapters(
    docs: dict[str, _DocBundle],
    spine_paths: list[str],
    nav_labels: dict[str, str],
) -> list[Chapter]:
    chapters: list[Chapter] = []
    ordered = [p for p in spine_paths if p in docs]
    ordered += [p for p in docs if p not in set(ordered)]
    for path in ordered:
        doc = docs[path]
        title = nav_labels.get(path) or _doc_title(doc.root) or posixpath.basename(path)
        chapters.append(
            Chapter(
                path=path,
                title=title,
                in_spine=path in set(spine_paths),
                ids=doc.ids,
            )
        )
    return chapters


def _doc_title(root: etree._Element) -> str:
    el = root.find(f".//{{{XHTML_NS}}}title")
    if el is not None:
        text = _element_text(el)
        if text:
            return text
    return ""


# ---------------------------------------------------------------------------
# 链接扫描
# ---------------------------------------------------------------------------

def _scan_links(
    docs: dict[str, _DocBundle],
    id_to_item: dict[str, ManifestItem],
    entry_set: set[str],
) -> tuple[list[LinkRecord], dict]:
    by_path = {it.href: it for it in id_to_item.values()}
    records: list[LinkRecord] = []
    uid = 0
    n_external = 0
    n_checked = 0
    n_broken = 0
    n_unlocatable = 0

    for path in sorted(docs):
        doc = docs[path]
        tree_anchors = _tree_anchor_elements(doc)
        mapping = _pair_anchors(tree_anchors, doc.anchors)
        base_dir = posixpath.dirname(path)
        id_index = {d["id"]: d for d in doc.ids}

        for idx, (el, line, href_value) in enumerate(tree_anchors):
            raw_anchor = mapping.get(idx)
            locatable = raw_anchor is not None
            uid += 1
            text = _anchor_text(el)

            if is_external(href_value):
                n_external += 1
                if raw_anchor is None:
                    # 外部链接不会被修改，但定位失败说明扫描器对该文件
                    # 可能整体不可信，仍要让用户知情（不阻断，因不导出）。
                    n_unlocatable += 1
                continue

            n_checked += 1
            broken = False
            reason = None
            target_file: str | None = None
            fragment: str | None = None

            path_part, fragment = split_href(href_value)
            if same_document(path_part):
                target_file = path
                ok, reason = _check_fragment(fragment, id_index)
                broken = not ok
            else:
                decoded_path: str | None = None
                decoded_frag: str | None = fragment
                try:
                    decoded_path = percent_decode_once(path_part)
                    target_file = resolve_relative(base_dir, decoded_path)
                except ValueError as exc:
                    broken = True
                    reason = f"path-escape:{exc}"
                    target_file = None
                if decoded_frag is not None and not broken:
                    try:
                        decoded_frag = percent_decode_once(fragment)
                    except ValueError as exc:
                        broken = True
                        reason = f"bad-fragment-encoding:{exc}"

                if not broken and target_file is not None:
                    item = by_path.get(target_file)
                    target_doc = docs.get(target_file)
                    decision = _classify_target(
                        target_file, item, target_doc, entry_set, decoded_frag
                    )
                    broken = decision[0]
                    reason = decision[1]
                    if decision[2] == "drop":
                        # 非 XHTML 资源：不在本工具校验范围
                        n_checked -= 1
                        continue

            if not locatable:
                n_unlocatable += 1
            if broken:
                n_broken += 1
                records.append(
                    LinkRecord(
                        uid, path, line, text, href_value, False, True, reason,
                        target_file, fragment, locatable,
                        raw_anchor.value_start if raw_anchor else -1,
                        raw_anchor.value_end if raw_anchor else -1,
                        raw_anchor.quote if raw_anchor else '"',
                    )
                )

    stats = {
        "documents": len(docs),
        "external_links": n_external,
        "checked_links": n_checked,
        "broken_links": n_broken,
        "unlocatable_links": n_unlocatable,
    }
    return records, stats


def _tree_anchor_elements(
    doc: _DocBundle,
) -> list[tuple[etree._Element, int, str]]:
    out = []
    for el in doc.root.iter():
        if _localname(el) != "a":
            continue
        # 只校验 XHTML 命名空间下的 a[href]；文档内嵌的其它词汇表
        # （SVG/MathML/无命名空间元素）不在范围内。
        if _namespace(el) != XHTML_NS:
            continue
        href = el.get("href")
        if href is None:
            continue
        out.append((el, el.sourceline, href))
    return out


def _pair_anchors(
    tree_anchors: list[tuple[etree._Element, int, str]],
    raw_anchors: list[RawAnchor],
) -> dict[int, RawAnchor]:
    """将 lxml 树中的锚点与字节扫描锚点按文档顺序一一对应。

    两侧都按文档顺序产出锚点：第 N 个树锚点对应第 N 个字节锚点。
    若数量不同，或某一对的 (行号, 命名空间, 归一化属性值) 不一致，
    说明字节扫描与 XML 树存在分歧，整份文件放弃定位，保守禁止导出。
    （即使一行中有两个逐字相同的 href，顺序本身也确定了唯一对应。）
    """
    if len(tree_anchors) != len(raw_anchors):
        return {}
    mapping: dict[int, RawAnchor] = {}
    for t, ((el, line, val), ra) in enumerate(zip(tree_anchors, raw_anchors)):
        ns_uri = _namespace(el)
        if ra.line != line or ra.ns_uri != ns_uri or ra.raw_value != val:
            return {}
        mapping[t] = ra
    return mapping


def _check_fragment(
    fragment: str | None, id_index: dict[str, dict]
) -> tuple[bool, str | None]:
    # 无片段或空片段均指向章节顶部，恒有效
    if fragment is None or fragment == "":
        return True, None
    try:
        decoded = percent_decode_once(fragment)
    except ValueError as exc:
        return False, f"bad-fragment-encoding:{exc}"
    if decoded == "":
        return True, None
    if decoded in id_index:
        return True, None
    return False, f"id-missing:{decoded}"


def _classify_target(
    target_file: str,
    item: ManifestItem | None,
    target_doc: "_DocBundle | None",
    entry_set: set[str],
    fragment: str | None,
) -> tuple[bool, str | None, str]:
    """返回 (是否失效, 原因, 动作)；动作 drop 表示非 XHTML，不校验。"""
    if item is not None:
        if not _is_xhtml_media(item.media_type):
            return False, None, "drop"
        if target_doc is None:
            return True, "target-missing", "keep"
        if fragment is None or fragment == "":
            return False, None, "keep"
        try:
            decoded = percent_decode_once(fragment)
        except ValueError as exc:
            return True, f"bad-fragment-encoding:{exc}", "keep"
        if decoded == "" or decoded in {d["id"] for d in target_doc.ids}:
            return False, None, "keep"
        return True, f"id-missing:{decoded}", "keep"

    # 不在 manifest 中
    if target_file in entry_set:
        if _looks_like_xhtml_path(target_file):
            return True, "not-in-manifest", "keep"
        return False, None, "drop"
    if _looks_like_xhtml_path(target_file):
        return True, "target-missing", "keep"
    return False, None, "drop"


def _is_xhtml_media(media_type: str) -> bool:
    return media_type == XHTML_MEDIA


def _element_text(el: etree._Element) -> str:
    parts = []
    for t in el.itertext():
        parts.append(t)
    text = " ".join("".join(parts).split())
    return text.strip()


def _anchor_text(el: etree._Element) -> str:
    text = _element_text(el)
    if text:
        return text[:100]
    img = el.find(f".//{{{XHTML_NS}}}img")
    if img is not None and img.get("alt"):
        return f'[图片] {img.get("alt")}'.strip()[:100]
    return ""
