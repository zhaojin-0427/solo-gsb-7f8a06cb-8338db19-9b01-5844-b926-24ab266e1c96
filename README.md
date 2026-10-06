# EPUB3 章节跳转修复（本地网页工具）

在本地浏览器中扫描 EPUB3 里的失效章节跳转（锚点 / XHTML 相对链接），
逐条选择新目标后导出修复版 EPUB。后端 **Python + FastAPI + lxml**，
前端 **原生 JavaScript**（无构建、无前端依赖）。

## 一条命令启动（Docker Compose）

```bash
docker compose up --build
```

然后用浏览器打开：

> **http://localhost:8000**

停止：`docker compose down`。

## 不用 Docker 时的本地启动

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

访问地址同样是 <http://localhost:8000>。

## 使用流程

1. 在首页选择或拖入一个**未加密** `.epub` 文件。
2. 后端通过 `META-INF/container.xml` 找到 OPF，再由 manifest / spine /
   `properties="nav"` 的 nav 文档确定正文与目录。
3. 页面列出所有失效链接的**来源文件、链接文字、原始 href 与失效原因**；
   每条可从 manifest 内全部 XHTML 的 id（或“章节顶部”）中选择新目标，
   并实时预览将写入的相对 href。
4. 点击“导出修复后的 EPUB”。后端会重写字节、重新打包并**重扫成品**：
   仍存在失效跳转时拒绝下载。
5. 换一本书时重新上传即可，旧书的所有裁决会被清空。

## 限制与校验规则

- ZIP 文件 ≤ **20 MB**，解压后总大小 ≤ **50 MB**，manifest 中 XHTML ≤ **30 篇**。
- 仅支持未加密 EPUB：检测到 ZIP 加密标志或 `META-INF/encryption.xml` 会报错。
- href 相对**所在 XHTML 文件**的目录解析；路径与片段各只做**一次**百分号解码；
  文件名与 id **区分大小写**；`#` 后为空片段表示指向该章节顶部。
- 外部链接（带 URL scheme，如 `http://`、`mailto:`）不检查、不修改。
- 结构问题都会定位到具体文件，按两级呈现：
  - **【阻断导出】**：不可唯一定位的失效链接、加密、`mimetype` 缺失或内容错误等；
  - **【提示】**：`mimetype` 非首项或被压缩（导出时**自动纠正**为首项且 STORE）、
    同一 XHTML 内重复 id 等——会提示但不阻断，重扫仍以“所有失效跳转已修复”为准。
- 另外下列情况直接作为致命错误返回，无法进入编辑：
  - 不是合法 ZIP、缺少 `container.xml` 或其中无 OPF rootfile、OPF 不存在；
  - manifest/spine/nav 引用缺失、media-type 不符、缺少 nav 文档；
  - 归一化后路径重名（ZIP 条目或 manifest 引用）；
  - 相对路径越出包根（在链接级别标注并视为失效）；
  - XML 解析失败。

## 字节级保真

导出时，对每篇被修改的 XHTML，后端先用 lxml 解析语义，再用自写的
XML 字节扫描器把每个 `a[href]` 与**原文字节区间**一一对应并做内容校验；
只有被选中的链接，其 `href` 属性值（引号内）字节会被替换为新的、
正确百分号编码的相对 href。

**除所选属性值外的其余字节全部保持不变**，包括：未选链接、正文文本、
实体引用、注释、DOCTYPE、空白与引号风格。无法把解析出的链接唯一对应到
原文属性位置时（如无法解释的实体引用、标签结构无法对齐），会逐条指出
文件与链接并禁止导出。

未修改的 ZIP 条目解压内容保持一致；`mimetype` 仍为首项且以 STORE
（不压缩）方式写入。

## 接口（供调试）

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/upload` | multipart 上传 EPUB，返回分析结果 |
| POST | `/api/preview` | 给定链接与新目标，预览新 href |
| POST | `/api/export` | 提交裁决，重扫通过后返回修复版 EPUB |
| GET  | `/api/health` | 健康检查 |

## 目录结构

```
app/
  main.py      # FastAPI 路由、会话、序列化
  epub.py      # ZIP/OPF/nav/XHTML 解析、字节定位、重打包、重扫
static/
  index.html   # 单页界面
  style.css
  app.js       # 原生 JS 交互
Dockerfile
docker-compose.yml
requirements.txt
```
