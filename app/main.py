# -*- coding: utf-8 -*-
"""FastAPI 入口：上传分析 / 预览新 href / 导出修复后的 EPUB。"""

from __future__ import annotations

import os
import posixpath
import threading
import uuid
from collections import OrderedDict
from urllib.parse import quote as url_quote

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import epub

app = FastAPI(title="EPUB3 章节跳转修复", version="1.0")


# ---------------------------------------------------------------------------
# 会话（本地单用户工具，内存保存即可；换书上传会产生新会话）
# ---------------------------------------------------------------------------

_SESSIONS: "OrderedDict[str, epub.Book]" = OrderedDict()
_LOCK = threading.Lock()
_MAX_SESSIONS = 8


def put_session(book: epub.Book) -> str:
    sid = uuid.uuid4().hex
    with _LOCK:
        _SESSIONS[sid] = book
        _SESSIONS.move_to_end(sid)
        while len(_SESSIONS) > _MAX_SESSIONS:
            _SESSIONS.popitem(last=False)
    return sid


def get_session(sid: str) -> epub.Book:
    with _LOCK:
        book = _SESSIONS.get(sid)
    if book is None:
        raise HTTPException(status_code=404, detail={
            "code": "session_expired",
            "message": "会话不存在或已过期，请重新上传 EPUB",
        })
    return book


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def chapter_payload(book: epub.Book) -> list[dict]:
    out = []
    for name in book.spine_names:
        fm = book.files.get(name)
        if fm is None:
            continue
        title = fm.nav_label or fm.title or posixpath.basename(name)
        out.append({"file": name, "title": title, "isNav": fm.is_nav})
    return out


def files_payload(book: epub.Book) -> list[dict]:
    out = []
    for name in sorted(book.files):
        fm = book.files[name]
        out.append({
            "file": name,
            "title": fm.nav_label or fm.title or posixpath.basename(name),
            "ids": fm.ids,
            "inSpine": fm.in_spine,
            "isNav": fm.is_nav,
        })
    return out


def book_payload(sid: str, book: epub.Book) -> dict:
    broken = [l for l in book.links if not l.external and l.status == "broken"]
    return {
        "sessionId": sid,
        "opf": book.opf_name,
        "limits": {
            "uploadMB": epub.MAX_UPLOAD_BYTES // 1024 // 1024,
            "inflatedMB": epub.MAX_TOTAL_INFLATED // 1024 // 1024,
            "xhtml": epub.MAX_XHTML_FILES,
        },
        "stats": {
            "xhtmlCount": len(book.files),
            "linkCount": len(book.links),
            "brokenCount": len(broken),
            "externalCount": sum(1 for l in book.links if l.external),
        },
        "chapters": chapter_payload(book),
        "files": files_payload(book),
        "links": [l.to_payload() for l in book.links],
        "faults": book.faults,
    }


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > epub.MAX_UPLOAD_BYTES:
        return JSONResponse(
            status_code=422,
            content={"fatal": {
                "code": "too_large",
                "file": file.filename,
                "message": (
                    f"上传文件 {len(data) / 1024 / 1024:.1f} MB，"
                    f"超过 {epub.MAX_UPLOAD_BYTES // 1024 // 1024} MB 上限"
                ),
            }},
        )
    try:
        book = epub.parse_book(data)
    except epub.EpubError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "fatal": {
                    "code": exc.code,
                    "file": exc.file,
                    "message": exc.message,
                }
            },
        )
    # 解析成功（可能带非致命 faults，如重复 id / 不可定位链接）
    sid = put_session(book)
    return book_payload(sid, book)


class Decision(BaseModel):
    targetFile: str
    fragment: str | None = None  # None=整篇；""=章节顶部；其他=id


class PreviewRequest(BaseModel):
    sessionId: str
    linkId: str
    targetFile: str
    fragment: str | None = None


@app.post("/api/preview")
def preview(req: PreviewRequest):
    book = get_session(req.sessionId)
    link = next((l for l in book.links if l.uid == req.linkId), None)
    if link is None:
        raise HTTPException(404, detail={"code": "not_found", "message": "链接不存在"})
    if req.targetFile not in book.files:
        raise HTTPException(400, detail={
            "code": "invalid_target", "message": "目标文件不在 manifest XHTML 中",
        })
    frag = req.fragment
    if frag not in (None, "") and frag not in book.files[req.targetFile].ids_set:
        raise HTTPException(400, detail={
            "code": "invalid_target",
            "message": f"目标 id 不存在：{req.targetFile}#{frag}",
        })
    href = epub.build_href(link.file_name, req.targetFile, frag)
    return {"href": href}


class ExportRequest(BaseModel):
    sessionId: str
    # linkId -> 裁决；只包含用户改过/确认的失效链接
    decisions: dict[str, Decision]


@app.post("/api/export")
def export(req: ExportRequest):
    book = get_session(req.sessionId)

    # error 级故障（不可唯一定位的链接、加密、mimetype 缺失/错误等）阻断导出；
    # warning 级（mimetype 顺序/压缩、重复 id）仅提示，重打包会纠正 mimetype。
    error_faults = [f for f in book.faults if f.get("severity", "error") == "error"]
    if error_faults:
        raise HTTPException(409, detail={
            "code": "faults_present",
            "message": "存在无法唯一定位的链接或致命结构问题，禁止导出",
            "faults": error_faults,
        })

    broken = [l for l in book.links if not l.external and l.status == "broken"]
    decisions = {k: v.model_dump() for k, v in req.decisions.items()}

    # 不可定位的失效链接已在 error_faults 中阻断；这里只要求可定位链接被裁决
    missing = [l.uid for l in broken if l.span is not None and l.uid not in decisions]
    if missing:
        raise HTTPException(409, detail={
            "code": "undecided",
            "message": "仍有失效链接未选择新目标",
            "linkIds": missing,
        })

    # 只允许对失效链接做裁决（有效/外部链接不应被改）
    broken_ids = {l.uid for l in broken}
    extra = [uid for uid in decisions if uid not in broken_ids]
    if extra:
        raise HTTPException(400, detail={
            "code": "invalid_decision",
            "message": f"只允许修改失效链接：{extra}",
        })

    try:
        epub.validate_decisions(book, decisions)
        changed = epub.apply_decisions(book, decisions)
        out_bytes = epub.repackage(book, changed)
        error_faults2, still_broken, _warnings = epub.rescan(out_bytes)
    except epub.EpubError as exc:
        raise HTTPException(422, detail={
            "code": exc.code, "file": exc.file, "message": exc.message,
        })

    if error_faults2 or still_broken:
        raise HTTPException(409, detail={
            "code": "rescan_failed",
            "message": "重扫成品仍存在失效跳转或致命结构问题，禁止下载",
            "faults": error_faults2,
            "broken": still_broken,
        })

    encoded_name = (posixpath.splitext(book.opf_name)[0].replace("/", "_") or "book")
    download_name = f"{encoded_name}.fixed.epub"
    ascii_name = download_name.encode("ascii", "ignore").decode() or "book.fixed.epub"
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{url_quote(download_name)}"
        ),
    }
    return Response(
        content=out_bytes,
        media_type="application/epub+zip",
        headers=headers,
    )


# 静态前端（放在最后，避免与 /api 冲突）
_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
