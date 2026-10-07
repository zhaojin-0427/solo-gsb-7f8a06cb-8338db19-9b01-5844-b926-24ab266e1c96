"""FastAPI 入口：上传分析、读取结果、暂存裁决、导出修复后的 EPUB。"""

from __future__ import annotations

import pathlib

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import analyze, export
from .errors import StructureErrors, UploadError
from .storage import JobStore

app = FastAPI(title="EPUB3 章节跳转修复", version="1.0")
store = JobStore()

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"


class Target(BaseModel):
    file: str = ""
    id: str = ""  # "" 表示章节顶部


class DecisionIn(BaseModel):
    decisions: dict[str, Target] = Field(default_factory=dict)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/static/app.js")
def app_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="application/javascript")


@app.get("/static/style.css")
def style_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "style.css", media_type="text/css")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    name = file.filename or "book.epub"
    data = await file.read()
    try:
        result = analyze.analyze_epub(data)
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StructureErrors as exc:
        return JSONResponse(
            status_code=422,
            content={"structural_errors": [e.to_dict() for e in exc.errors]},
        )
    job = store.create(name, data, result)
    return {"job_id": job.job_id, "filename": job.filename, **result}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新上传")
    return {"job_id": job.job_id, "filename": job.filename, **job.analysis}


@app.put("/api/jobs/{job_id}/decisions")
def put_decisions(job_id: str, payload: DecisionIn):
    decisions = {k: v.model_dump() for k, v in payload.decisions.items()}
    job = store.save_decisions(job_id, decisions)
    if job is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新上传")
    return {"saved": len(decisions)}


@app.post("/api/jobs/{job_id}/export")
def export_job(job_id: str, payload: DecisionIn | None = None):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新上传")
    decisions = (
        {k: v.model_dump() for k, v in payload.decisions.items()}
        if payload is not None
        else dict(job.decisions)
    )
    try:
        fixed = export.export_epub(job.epub, job.analysis, decisions)
    except export.ExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StructureErrors as exc:
        return JSONResponse(
            status_code=422,
            content={"structural_errors": [e.to_dict() for e in exc.errors]},
        )
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job.decisions = decisions

    stem = pathlib.Path(job.filename).stem or "book"
    download_name = f"{stem}.fixed.epub"
    return Response(
        content=fixed,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}
