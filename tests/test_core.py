"""核心链路端到端测试：分析 -> 裁决 -> 字节精确导出 -> 成品复检。"""

from __future__ import annotations

import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from make_samples import build_epub  # noqa: E402

from app import analyze, export  # noqa: E402
from app.errors import StructureErrors, UploadError  # noqa: E402


def must(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok - {msg}")


def main() -> None:
    path = build_epub("/tmp/sample.epub")
    data = open(path, "rb").read()

    print("[1] 正常样例分析")
    r = analyze.analyze_epub(data)
    broken = {(lk["file"], lk["href"]): lk for lk in r["links"]}
    print(f"     broken={r['stats']['broken_links']} "
          f"external={r['stats']['external_links']} "
          f"checked={r['stats']['checked_links']}")
    must(r["stats"]["broken_links"] == 5, "恰好 5 条失效链接")
    must(r["stats"]["external_links"] == 1, "识别 1 条外部链接")
    must(all(lk["locatable"] for lk in r["links"]), "所有失效链接可定位")
    ch1 = "OEBPS/chapter1.xhtml"
    sub = "OEBPS/sub/section.xhtml"
    must((ch1, "#gone1") in broken, "同篇缺 id 检出")
    must((ch1, "chapter2.xhtml#sec9") in broken, "跨篇缺 id 检出")
    must((ch1, "missing.xhtml#x") in broken, "目标缺失检出")
    must((sub, "../chapter2.xhtml#nope") in broken, "上级目录缺 id 检出")
    must((sub, "../../../etc/passwd") in broken, "越界路径检出")
    # 有效链接不应出现
    must((ch1, "#p1") not in broken, "好锚点未误报")
    must((ch1, "sub/section.xhtml#") not in broken, "空片段判为有效")
    must((ch1, "%63hapter2.xhtml#sec2") not in broken, "百分号解码一次后有效")
    must((sub, "../chapter2.xhtml#sec2") not in broken, "上级目录好链接有效")

    print("[2] 导出（为 5 条失效链接选目标）")
    decisions = {}
    targets = {
        "#gone1": ("OEBPS/chapter1.xhtml", "p1"),
        "chapter2.xhtml#sec9": ("OEBPS/chapter2.xhtml", "sec2"),
        "missing.xhtml#x": ("OEBPS/chapter2.xhtml", ""),  # 章节顶部
        "../chapter2.xhtml#nope": ("OEBPS/chapter2.xhtml", "sec3"),
        "../../../etc/passwd": ("OEBPS/chapter1.xhtml", ""),
    }
    for lk in r["links"]:
        if lk["href"] in targets:
            f, i = targets[lk["href"]]
            decisions[str(lk["id"])] = {"file": f, "id": i}
    must(len(decisions) == 5, "5 条裁决齐全")
    fixed = export.export_epub(data, r, decisions)

    print("[3] ZIP 结构与字节保持")
    z1 = zipfile.ZipFile(io.BytesIO(data))
    z2 = zipfile.ZipFile(io.BytesIO(fixed))
    must([i.filename for i in z2.infolist()] == [i.filename for i in z1.infolist()],
         "条目名称/顺序一致")
    m = z2.infolist()[0]
    must(m.filename == "mimetype" and m.compress_type == zipfile.ZIP_STORED,
         "mimetype 首项且不压缩")
    for name in z1.namelist():
        if name in (ch1, sub):
            continue
        must(z1.read(name) == z2.read(name), f"未改条目逐字节一致: {name}")

    print("[4] 修改文件：除属性值外字节相同")
    assert_only_attr_values_changed(
        z1.read(ch1), z2.read(ch1),
        ["#p1", "chapter2.xhtml#sec2", "chapter2.xhtml"],
        "chapter1.xhtml",
    )
    assert_only_attr_values_changed(
        z1.read(sub), z2.read(sub),
        ["../chapter2.xhtml#sec3", "../chapter1.xhtml"],
        "sub/section.xhtml",
    )
    must(b"Chapter&nbsp;1" in z2.read(ch1), "实体保持")
    must(b"<!-- a comment" in z2.read(ch1), "注释保持")
    must(b"\r\n" in z2.read(ch1), "CRLF 空白保持")

    print("[5] 成品重扫无失效")
    rr = analyze.analyze_epub(fixed)
    must(rr["stats"]["broken_links"] == 0, "成品 0 失效链接")
    # 新 href 相对所在文件正确
    text = z2.read(sub).decode()
    must('href="../chapter2.xhtml#sec3"' in text, "子目录相对 href 正确")
    must('href="../chapter1.xhtml"' in text, "越界链接改到顶部 href 正确")

    print("[6] 未处理完全部失效链接 -> 拒绝导出")
    partial = dict(list(decisions.items())[:1])
    try:
        export.export_epub(data, r, partial)
        raise AssertionError("应拒绝导出")
    except export.ExportError as exc:
        must("仍有失效跳转" in str(exc), f"拒绝信息正确: {exc}")

    print("[7] 结构错误定位")
    dup_id = b"""<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">
<body><p id="x">a</p><div id="x">b</div></body></html>"""
    bad = build_epub("/tmp/dupid.epub",
                     {"OEBPS/chapter2.xhtml": dup_id})
    try:
        analyze.analyze_epub(open(bad, "rb").read())
        raise AssertionError("应报重复 id")
    except StructureErrors as exc:
        msgs = [e.to_dict() for e in exc.errors]
        must(any("重复 id" in e["message"] and e["line"] for e in msgs),
             f"重复 id 带行列号: {msgs}")

    missing = build_epub("/tmp/missing.epub", {})
    # 手工构造 manifest 指向缺失文件
    opf = open(path, "rb")  # noqa: SIM115
    bad_opf = zipfile.ZipFile(io.BytesIO(data)).read("OEBPS/content.opf").replace(
        b'<item id="css" href="style.css"',
        b'<item id="css" href="ghost.css"',
    )
    bad2 = build_epub("/tmp/missing-ref.epub",
                      {"OEBPS/content.opf": bad_opf})
    try:
        analyze.analyze_epub(open(bad2, "rb").read())
        raise AssertionError("应报引用缺失")
    except StructureErrors as exc:
        must(any("ghost.css" in e.message for e in exc.errors),
             "manifest 引用缺失定位")

    print("[8] XML 解析失败定位")
    broken_xml = (
        b"<?xml version='1.0' encoding='UTF-8'?>\n"
        b'<html xmlns="http://www.w3.org/1999/xhtml">\n'
        b"<head><title>x</title></head>\n"
        b"<body>\n"
        b"<p>oops\n"
        b"</body>\n"
        b"</html>"
    )
    bad3 = build_epub("/tmp/badxml.epub", {"OEBPS/chapter2.xhtml": broken_xml})
    try:
        analyze.analyze_epub(open(bad3, "rb").read())
        raise AssertionError("应报 XML 解析失败")
    except StructureErrors as exc:
        e = [x for x in exc.errors if "XML 解析失败" in x.message][0]
        must(e.file == "OEBPS/chapter2.xhtml" and e.line,
             f"XML 错误带文件行号: {e}")

    print("[9] 大小限制")
    big = b"x" * (analyze.MAX_UPLOAD + 1)
    try:
        analyze.analyze_epub(big)
        raise AssertionError("应拒绝超大文件")
    except UploadError as exc:
        must("20 MB" in str(exc), "超大 ZIP 报错")

    print("\n全部通过 ✓")


def assert_only_attr_values_changed(
    old: bytes, new: bytes, expected_news: list[str], label: str
) -> None:
    """以 href 值区间为分界，断言区间外字节逐段相同。"""
    from app.xmlscan import document_encoding, scan_anchors, parse_xml

    root, _ = parse_xml(old, label)
    enc = document_encoding(root)
    anchors = sorted(scan_anchors(old, enc), key=lambda a: a.value_start)

    # 结构段 = 属性值区间之外的全部字节；属性值替换只能发生在值区间内，
    # 因此新文件必然是：struct0 + q+新值+q + struct1 + ... 的拼接形式。
    struct_parts: list[bytes] = []
    quotes: list[bytes] = []
    prev = 0
    for a in anchors:
        # 引号紧贴区间两侧
        struct_parts.append(old[prev : a.value_start - 1])  # 到开始引号之前
        quotes.append(old[a.value_start - 1 : a.value_start])
        prev = a.value_end + 1  # 越过结束引号
    tail = old[prev:]

    pos = 0
    changed = 0
    for idx, a in enumerate(anchors):
        must(new[pos : pos + len(struct_parts[idx])] == struct_parts[idx],
             f"{label}: 第 {a.line} 行之前的结构字节保持一致")
        pos += len(struct_parts[idx])
        must(new[pos : pos + 1] == quotes[idx],
             f"{label}: 第 {a.line} 行属性引号保持")
        pos += 1
        endq = new.find(quotes[idx], pos)
        must(endq != -1, f"{label}: 第 {a.line} 行属性值结束引号存在")
        if new[pos:endq] != old[a.value_start : a.value_end]:
            changed += 1
        pos = endq + 1
    must(new[pos:] == tail, f"{label}: 文件末尾结构字节保持一致")
    must(changed > 0, f"{label}: 至少一个属性值被修改")

    for token in expected_news:
        must(token.encode() in new, f"{label}: 新内容包含 {token!r}")


if __name__ == "__main__":
    main()
