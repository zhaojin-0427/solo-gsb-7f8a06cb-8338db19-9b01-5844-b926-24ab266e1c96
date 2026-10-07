# EPUB3 章节跳转修复（本地网页工具）

本地运行的 EPUB3 失效链接修复器：上传未加密 EPUB，自动定位 **篇内锚点** 与
**指向 XHTML 的相对链接** 中失效的跳转，在网页上为每条失效链接选择新目标
（manifest 内 XHTML 的 id 或章节顶部）并预览，导出字节级精确修复的 EPUB。

- 后端：Python 3.11 + FastAPI + lxml
- 前端：原生 HTML / CSS / JavaScript（无构建步骤）
- 所有处理均在本机完成，文件不外传

## 一条命令启动（Docker Compose）

```bash
docker compose up --build
```

然后浏览器访问：

```
http://localhost:8000
```

停止：`Ctrl+C`（后台运行用 `docker compose up -d --build`，停止用
`docker compose down`）。

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 使用说明

1. 打开 http://localhost:8000，选择 EPUB（换书会自动清空上一本书的裁决）。
2. 页面按来源文件/行号列出每条失效链接的 **链接文字** 与 **原 href**。
3. 在右侧两个下拉框选择目标 **章节** 与 **id**（第一项 = 章节顶部），
   下方显示该 id 的行号、标签与正文预览，以及编码后的新 href。
4. 点击"导出修复后的 EPUB"：服务端重新扫描成品，仍有失效跳转即拒绝下载；
   通过后下载 `<原名>.fixed.epub`。

## 上传限制

| 限制 | 值 |
| --- | --- |
| ZIP 大小 | ≤ 20 MB |
| 解压总大小 | ≤ 50 MB |
| XHTML 篇数（manifest 中 `application/xhtml+xml`） | ≤ 30 |
| 加密 EPUB（含 `META-INF/encryption.xml`） | 拒绝 |

## 链接判定规则

- 由 `META-INF/container.xml` 找 OPF，再以 **manifest / spine / nav**
  确定正文与目录；nav（EPUB3 `properties="nav"`）用于章节显示名。
- 只扫描 XHTML 中 `a[href]` 的两类引用：**篇内锚点** 与 **指向 XHTML 的
  相对链接**。带 scheme（`http:`、`mailto:`、`data:` …）与协议相对
  （`//host/...`）的外部链接不校验、不改动。
- href 相对 **所在文件** 解析；路径与片段 **各只百分号解码一次**；
  文件名与 id **区分大小写**；无片段或空片段指向 **章节顶部**。
- 非 XHTML 资源（图片、CSS、字体等）链接不在校验范围内。

## 字节精确改写保证

- 修改过的 XHTML，**除所选 href 属性值对应的原文字节外，其余字节逐字节相同**，
  包括未选链接、正文、实体、注释、CDATA 与空白。
- 解析出的链接若 **无法唯一对应** 到原文属性位置（扫描器与 XML 树核对
  失败），页面会指出文件与链接并 **禁止导出**。
- 未修改的 ZIP 条目解压内容保持一致；`mimetype` 仍是第一项且不压缩。
- 导出后对成品重新扫描，仍有失效跳转则禁止下载。

## 定位报错（结构问题，需先修源文件）

以下问题不进入编辑界面，而是列出带文件与行列号的错误：

- `container.xml` / OPF / nav 缺失或引用缺失（manifest 指向不存在的文件、
  spine idref 无效等）；
- 归一化后路径重名（ZIP 条目之间或 manifest 条目之间）；
- 同一篇 XHTML 内重复 id；
- 相对路径越出包根（`..` 逃逸）、ZIP 条目名非法；
- 任意 XML 解析失败（给出文件、行、列）。

## 目录结构

```
app/
  main.py        FastAPI 路由
  analyze.py     EPUB 结构解析与失效链接扫描
  xmlscan.py     lxml 解析 + 字节级属性定位
  export.py      精确改写、重打包、成品复检
  urltools.py    href 解析/解码/编码
  storage.py     内存会话（换书清裁决）
  static/        原生前端
Dockerfile  docker-compose.yml  requirements.txt
```

## 本地自测

```bash
python tests/make_samples.py        # 生成 /tmp 下的样例 EPUB
python tests/test_core.py
```
