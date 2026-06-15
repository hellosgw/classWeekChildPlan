"""
classWeekChildPlan — 家园共育周计划生成器
===========================================
DeepSeek API 驱动的幼儿教育 AI 工具。
上传幼儿园周活动计划 PDF → 自动生成家庭同步计划。
"""

import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from dotenv import load_dotenv, find_dotenv

from pdf_parser import parse_pdf
from prompt import SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv(find_dotenv())
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="classWeekChildPlan", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
PLANS_DIR = Path(__file__).resolve().parent.parent / ".whale" / "output" / "weekly_plans"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    """Serve the single-page frontend."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/login.html")
async def login():
    """Serve the login page."""
    return FileResponse(FRONTEND_DIR / "login.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "model": DEEPSEEK_MODEL}


@app.get("/api/plans")
async def list_plans():
    """Return the 50 most recent plan summaries (no full markdown)."""
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    plans = []
    for f in sorted(PLANS_DIR.glob("*.json"), key=lambda p: p.stem, reverse=True)[:50]:
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            plans.append({
                "id": data["id"],
                "title": data.get("title", ""),
                "created_at": data["created_at"],
                "pdf_name": data.get("pdf_name", ""),
                "preview": data.get("preview", ""),
            })
        except Exception:
            continue
    return plans


@app.get("/api/plans/{plan_id}")
async def get_plan(plan_id: str):
    """Return a single plan's full JSON record."""
    plan_path = PLANS_DIR / f"{plan_id}.json"
    if not plan_path.exists():
        raise HTTPException(404, "计划不存在")
    with open(plan_path, encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/generate-plan")
async def generate_plan(file: UploadFile = File(...)):
    """Accept a PDF, extract text, call DeepSeek, return the weekly plan."""

    # --- Validate input ----------------------------------------------------
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "请上传 PDF 文件（.pdf）")

    raw = await file.read()
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(400, f"PDF 文件不能超过 {MAX_PDF_BYTES // 1024 // 1024} MB")

    # --- Parse PDF ---------------------------------------------------------
    try:
        result = parse_pdf(raw)
    except Exception as exc:
        logger.exception("PDF parsing failed")
        raise HTTPException(400, f"PDF 解析失败：{exc}")

    pdf_text = result["text"]

    if not pdf_text.strip():
        raise HTTPException(400, "PDF 内容为空，请检查文件是否包含可提取的文字（扫描件不支持）")

    # Log parse metadata for diagnostics
    logger.info(
        "PDF parsed — %d chars, %d pages, engine=%s, has_tables=%s, is_scanned=%s",
        len(pdf_text),
        result["pages"],
        result["engine"],
        result["has_tables"],
        result["is_scanned"],
    )
    for w in result["warnings"]:
        logger.warning("PDF parser: %s", w)

    # --- Call DeepSeek API -------------------------------------------------
    if not DEEPSEEK_API_KEY:
        raise HTTPException(500, "未配置 DEEPSEEK_API_KEY 环境变量，请在 .env 或启动环境中设置")

    try:
        plan_markdown = _call_deepseek(pdf_text)
    except Exception as exc:
        logger.exception("DeepSeek API call failed")
        raise HTTPException(500, f"AI 生成失败：{exc}")

    # --- Persist plan to disk ------------------------------------------------
    plan_id = _save_plan(plan_markdown, file.filename)

    return JSONResponse({"plan": plan_markdown, "plan_id": plan_id})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _call_deepseek(pdf_text: str) -> str:
    """Send the system prompt + PDF text to DeepSeek and return the response."""
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

    user_message = (
        "这是本周的 PDF 文件内容：\n\n"
        f"{pdf_text}\n\n"
        "请严格按照我的 4 岁孩子及时间表（18:30-19:10 自主，20:00-20:40 陪伴）生成本周计划。"
    )

    response = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.7,
        max_tokens=4096,
    )

    content = response.choices[0].message.content
    return content or ""


def _save_plan(plan_markdown: str, pdf_name: str) -> str:
    """Save a generated plan to disk and return the plan_id."""
    plan_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Extract title and preview from the markdown
    lines = plan_markdown.strip().split("\n")
    title = pdf_name.replace(".pdf", "") if pdf_name else "未命名计划"
    preview = ""
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith(">") and stripped != "---":
            preview = stripped[:100]
            break

    record = {
        "id": plan_id,
        "title": title,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pdf_name": pdf_name,
        "model": DEEPSEEK_MODEL,
        "preview": preview,
        "plan_markdown": plan_markdown,
    }

    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    with open(PLANS_DIR / f"{plan_id}.json", "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

    logger.info("Plan saved: %s / %s", plan_id, title)
    return plan_id
