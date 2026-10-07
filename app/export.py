"""按用户裁决修复链接并重打包 EPUB。

硬性保证：
* 被修改的 XHTML，除所选 href 属性值对应的原文字节外，其余字节逐字节相同；
* 未改 ZIP 条目的解压内容保持一致；
* mimetype 仍是首项且不压缩；
* 重扫成品，仍有失效跳转则禁止下载。
"""

from __future__ import annotations

import io
import zipfile

from . import analyze
from .errors import StructureErrors, UploadError
from .urltools import build_href


class ExportError(Exception):
    """导出阶段错误（裁决无效 / 仍有失效链接 / 无法定位）。"""


def apply_edits(raw: bytes, edits: list[dict], encoding: str = "utf-8") -> bytes:
    """对单个 XHTML 的原文字节应用 (start,end,new_value) 编辑。

    从后往前替换；属性值只用 URI 合法 ASCII 字符（非 ASCII 一律百分号
    编码），因此新字节为纯 ASCII，任何声明编码下都与原文字节布局兼容。
    """
    edits_sorted = sorted(edits, key=lambda e: e["start"], reverse=True)
    out = raw
    for ed in edits_sorted:
        s, e = ed["start"], ed["end"]
        if not (0 <= s <= e <= len(out)):
            raise ExportError(f"属性区间越界 ({s},{e})")
        new_bytes = ed["value"].encode("ascii")
        try:
            out[s:e].decode(encoding)
        except UnicodeDecodeError as exc:
            raise ExportError(
                f"原属性值不是声明编码 {encoding} 下的合法文本，拒绝修改"
            ) from exc
        out = out[:s] + new_bytes + out[e:]
    return out


def export_epub(original: bytes, analysis: dict, decisions: dict) -> bytes:
    """导出修复后的 EPUB。

    decisions: {link_id(字符串): {"file": 目标 XHTML 路径, "id": 目标 id 或 ""}}
    """
    # 1) 重新分析原始文件（不相信上传后被篡改的 analysis）
    fresh = analyze.analyze_epub(original)
    _ensure_consistent(analysis, fresh)

    links = {lk["id"]: lk for lk in fresh["links"]}
    chapters = {ch["path"]: ch for ch in fresh["chapters"]}

    # 2) 校验裁决并构造每文件编辑
    edited_files: dict[str, list[dict]] = {}
    decided_ids: set[int] = set()
    for key, target in decisions.items():
        try:
            link_id = int(key)
        except (TypeError, ValueError):
            raise ExportError(f"无效的链接标识: {key!r}")
        if link_id not in links:
            raise ExportError(f"链接 {link_id} 不在失效列表中（可能已过期）")
        link = links[link_id]
        if not link["locatable"]:
            raise ExportError(
                f"链接无法唯一对应到原文属性位置，禁止导出: "
                f"{link['file']} 第 {link['line']} 行 href={link['href']!r}"
            )
        target_file = (target or {}).get("file", "")
        target_id = (target or {}).get("id", "") or ""
        if target_file not in chapters:
            raise ExportError(
                f"链接 {link_id} 的目标章节不在 manifest 内: {target_file!r}"
            )
        if target_id:
            valid_ids = {d["id"] for d in chapters[target_file]["ids"]}
            if target_id not in valid_ids:
                raise ExportError(
                    f"链接 {link_id} 的目标 id 不存在: "
                    f"{target_file}#{target_id}"
                )
        new_href = build_href(link["file"], target_file, target_id or None)
        edited_files.setdefault(link["file"], []).append(
            {
                "start": _offset(link, "start"),
                "end": _offset(link, "end"),
                "value": new_href,
                "link_id": link_id,
            }
        )
        decided_ids.add(link_id)

    # 3) 未裁决的失效链接（含不可定位的）一律禁止导出
    unresolved = [lk for lk in fresh["links"] if lk["id"] not in decided_ids]
    if unresolved:
        sample = "; ".join(
            f"{lk['file']}:{lk['line']} {lk['href']!r}"
            + ("（无法定位原文属性位置）" if not lk["locatable"] else "")
            for lk in unresolved[:5]
        )
        more = "" if len(unresolved) <= 5 else f" 等 {len(unresolved)} 条"
        raise ExportError(f"仍有失效跳转未处理，禁止导出: {sample}{more}")

    # 4) 重打包
    src = zipfile.ZipFile(io.BytesIO(original))
    dst_buf = io.BytesIO()
    dst = zipfile.ZipFile(dst_buf, "w")
    infos = src.infolist()

    # mimetype 首项、不压缩（重写以保证顺序与压缩方式确定）
    dst.writestr(
        "mimetype",
        b"application/epub+zip",
        compress_type=zipfile.ZIP_STORED,
    )
    for info in infos:
        if info.filename == "mimetype":
            continue
        payload = src.read(info.filename)
        if info.filename in edited_files:
            encoding = fresh.get("encodings", {}).get(info.filename, "utf-8")
            payload = apply_edits(payload, edited_files[info.filename], encoding)
        # 保留原压缩方式、日期时间与 ZIP 属性
        new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
        new_info.compress_type = info.compress_type
        new_info.external_attr = info.external_attr
        new_info.internal_attr = info.internal_attr
        new_info.create_system = info.create_system
        dst.writestr(new_info, payload, compress_type=info.compress_type)
    dst.close()
    src.close()
    result = dst_buf.getvalue()

    # 5) 成品重扫：仍有失效跳转则禁止下载
    try:
        rescan = analyze.analyze_epub(result)
    except (StructureErrors, LocatedError, UploadError) as exc:
        raise ExportError(f"成品复检失败: {exc}") from exc
    if rescan["stats"]["broken_links"]:
        lines = [
            f"{lk['file']}:{lk['line']} {lk['href']!r}"
            for lk in rescan["links"]
            if lk["broken"]
        ]
        raise ExportError(
            "成品中仍有失效跳转，禁止下载: " + "; ".join(lines[:5])
        )

    # 6) 断言未改条目解压内容一致
    _assert_unchanged_entries(original, result, set(edited_files))
    return result


def _offset(link: dict, which: str) -> int:
    key = "value_start" if which == "start" else "value_end"
    val = link.get(key, -1)
    if val is None or val < 0:
        raise ExportError(
            f"链接无法唯一对应原文属性位置: {link['file']} 第 {link['line']} 行"
        )
    return val


def _ensure_consistent(snapshot: dict, fresh: dict) -> None:
    """简单一致性检查：换书/陈旧会话直接拒绝。"""
    if snapshot.get("opf_path") != fresh["opf_path"]:
        raise ExportError("裁决属于另一本书（OPF 不一致），请重新上传")
    old = sorted((lk["file"], lk["line"], lk["href"]) for lk in snapshot["links"])
    new = sorted((lk["file"], lk["line"], lk["href"]) for lk in fresh["links"])
    if old != new:
        raise ExportError("失效链接列表已变化，请重新扫描后再导出")


def _assert_unchanged_entries(original: bytes, result: bytes, changed: set[str]) -> None:
    z1 = zipfile.ZipFile(io.BytesIO(original))
    z2 = zipfile.ZipFile(io.BytesIO(result))
    names1 = [i.filename for i in z1.infolist()]
    names2 = [i.filename for i in z2.infolist()]
    if names1 != names2:
        raise ExportError("成品 ZIP 条目顺序/名称发生变化")
    for name in names1:
        if name in changed:
            continue
        if z1.read(name) != z2.read(name):
            raise ExportError(f"未修改条目解压内容被改变: {name}")
    z1.close()
    z2.close()
