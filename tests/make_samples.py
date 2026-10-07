"""生成覆盖各种情形的样例 EPUB，供 test_core.py 使用。"""

from __future__ import annotations

import zipfile

CONTAINER = b"""<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = b"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bid">urn:uuid:test</dc:identifier>
    <dc:title>Test Book</dc:title>
    <dc:language>zh</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
    <item id="sub" href="sub/section.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="img" href="img/a.png" media-type="image/png"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="sub"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

NAV = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Nav</title></head>
<body>
<nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops">
  <ol>
    <li><a href="chapter1.xhtml">第一章</a></li>
    <li><a href="sub/section.xhtml">第二节</a></li>
    <li><a href="chapter2.xhtml#sec2">第二章 sec2</a></li>
  </ol>
</nav>
</body>
</html>
""".encode("utf-8")

# 故意保留多种写法：CRLF、注释、实体、未选链接、空白
CH1 = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n'
    '<html xmlns="http://www.w3.org/1999/xhtml">\r\n'
    '<head><title>Chapter 1</title></head>\r\n'
    '<body>\r\n'
    '<!-- a comment with <a href="x">fake</a> inside -->\r\n'
    '<h1 id="top">Chapter&nbsp;1</h1>\r\n'
    '<p><a id="p1"></a>Para one &#65; &amp; B</p>\r\n'
    '<ul>\r\n'
    '  <li><a href="#gone1">坏锚点：同篇缺 id</a></li>\r\n'
    '  <li><a href="chapter2.xhtml#sec9">坏锚点：跨篇缺 id</a></li>\r\n'
    '  <li><a href="missing.xhtml#x">坏锚点：目标文件缺失</a></li>\r\n'
    '  <li><a href="sub/section.xhtml#">空片段（应有效，指顶部）</a></li>\r\n'
    '  <li><a href="#p1">好锚点</a></li>\r\n'
    '  <li><a href="https://example.com/a?x=1#f">外部链接</a></li>\r\n'
    '  <li><a href="style.css#x">非 XHTML 资源（跳过）</a></li>\r\n'
    '  <li><a href="%63hapter2.xhtml#sec2">百分号路径（应解析成功）</a></li>\r\n'
    '</ul>\r\n'
    '</body></html>\r\n'
).encode("utf-8")

SUB = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Section</title></head>
<body>
<h1 id="s1">S1</h1>
<p><a href="../chapter2.xhtml#sec2">好链接：上级目录</a></p>
<p><a href="../chapter2.xhtml#nope">坏链接：上级目录缺 id</a></p>
<p><a href="../nope/../chapter2.xhtml#sec2">归一化后有效的 . 路径</a></p>
<p><a href="../../../etc/passwd">越界路径</a></p>
</body></html>
""".encode("utf-8")

CH2 = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter 2</title></head>
<body>
<h1 id="sec2">Section 2 &#x2603;</h1>
<p id="sec3">Section three</p>
</body></html>
""".encode("utf-8")


def build_epub(path: str, override: dict[str, bytes] | None = None) -> str:
    files = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": OPF,
        "OEBPS/nav.xhtml": NAV,
        "OEBPS/chapter1.xhtml": CH1,
        "OEBPS/chapter2.xhtml": CH2,
        "OEBPS/sub/section.xhtml": SUB,
        "OEBPS/style.css": b"body{}",
        "OEBPS/img/a.png": b"\x89PNG\r\n\x1a\n",
    }
    if override:
        files.update(override)
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in files.items():
            if name == "mimetype":
                zf.writestr(name, payload, compress_type=zipfile.ZIP_STORED)
            else:
                zf.writestr(name, payload, compress_type=zipfile.ZIP_DEFLATED)
    return path


if __name__ == "__main__":
    print(build_epub("/tmp/sample.epub"))
