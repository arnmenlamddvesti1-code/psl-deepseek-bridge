"""FastAPI HTTP Server для DeepSeek Session Bridge.

Поддерживает:
- GET /health -> проверка работоспособности
- POST /analyze -> PSL анализ лица (JSON base64 или multipart/form-data)
- POST /v1/chat/completions -> OpenAI-совместимый эндпоинт
"""

import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from deepseek_client import client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [DeepSeekBridge] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("DeepSeekBridge.Server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing DeepSeek Session Bridge...")
    yield
    logger.info("Shutting down DeepSeek Session Bridge...")
    await client.close()


app = FastAPI(
    title="DeepSeek Session Bridge",
    description="Autonomous headless bridge to chat.deepseek.com for facial PSL analysis",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware для прямого доступа из браузера и Telegram Mini Apps
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeJsonRequest(BaseModel):
    image: Optional[str] = Field(None, description="Base64 encoded image string or data URI")
    sex: Optional[str] = Field("male", description="Gender: 'male' or 'female'")
    prompt: Optional[str] = Field(None, description="Custom prompt text (optional)")
    prompt_variant: Optional[str] = Field("calibrated", description="'calibrated' or 'raw'")
    variant: Optional[str] = Field(None, description="Alias for prompt_variant")
    passes: Optional[int] = Field(None, description="Number of passes for median consensus (1-5)")
    expect: Optional[str] = Field("scoring", description="'scoring' or 'point_qa'")


class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]


class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = "deepseek-v4.1"
    temperature: Optional[float] = 0.0


def _extract_image_bytes(raw_input: Union[str, bytes]) -> bytes:
    if isinstance(raw_input, bytes):
        return raw_input
    s = str(raw_input).strip()
    if "base64," in s:
        s = s.split("base64,")[1]
    return base64.b64decode(s)


@app.get("/health")
@app.get("/healthz")
@app.get("/status")
@app.get("/")
async def health_check():
    """Health check endpoint for Railway and monitoring."""
    return {
        "status": "ok",
        "bridge": "deepseek-session",
        "version": "1.0.0",
        "port": int(os.environ.get("PORT", 8000)),
    }


@app.post("/analyze")
@app.post("/v1/evaluate")
@app.post("/evaluate")
async def analyze_face(
    request: Request,
    file: Optional[UploadFile] = File(None),
    image: Optional[str] = Form(None),
    sex: Optional[str] = Form(None),
    gender: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    prompt_variant: Optional[str] = Form(None),
    variant: Optional[str] = Form(None),
    passes: Optional[int] = Form(None),
    expect: Optional[str] = Form(None),
):
    """Анализирует фотографию лица через сессию DeepSeek.

    Поддерживает:
    - application/json: { "image": "base64...", "sex": "male" }
    - multipart/form-data: загрузка файла через поле 'file' или base64 через поле 'image'
    """
    img_bytes: Optional[bytes] = None
    req_sex = sex or gender or "male"
    req_prompt = prompt or ""
    req_variant = prompt_variant or variant or ""
    req_passes = passes
    req_expect = expect or "scoring"

    # 1. Проверяем Content-Type: если JSON
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            payload = AnalyzeJsonRequest(**body)
            if payload.image:
                img_bytes = _extract_image_bytes(payload.image)
            req_sex = payload.sex or req_sex
            req_prompt = payload.prompt or req_prompt
            req_variant = payload.prompt_variant or payload.variant or req_variant
            req_passes = payload.passes or req_passes
            req_expect = payload.expect or req_expect
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {e}")

    # 2. Если файл передан через multipart
    elif file is not None:
        img_bytes = await file.read()

    # 3. Если передан base64 параметр в форме
    elif image is not None:
        try:
            img_bytes = _extract_image_bytes(image)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to decode base64 image: {e}")

    if not img_bytes:
        raise HTTPException(
            status_code=400,
            detail="No image provided. Send 'image' in JSON body, 'file' in multipart, or 'image' in form-data.",
        )

    res = await client.analyze(
        img_bytes=img_bytes,
        prompt_text=req_prompt,
        sex=req_sex,
        variant=req_variant,
        expect=req_expect,
        passes=req_passes,
    )

    if res.get("status") == "error":
        status_code = 401 if res.get("code") == 40003 else 500
        return JSONResponse(status_code=status_code, content=res)

    return res


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """OpenAI-совместимый эндпоинт для подключения к любым LLM-инструментам."""
    img_bytes: Optional[bytes] = None
    prompt_text = ""
    sex = "male"

    for msg in req.messages:
        content = msg.content
        if isinstance(content, str):
            prompt_text += "\n" + content
            if "женский" in content.lower() or "female" in content.lower():
                sex = "female"
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_val = part.get("text", "")
                        prompt_text += "\n" + text_val
                        if "женский" in text_val.lower() or "female" in text_val.lower():
                            sex = "female"
                    elif part.get("type") == "image_url":
                        u = part.get("image_url", {}).get("url", "")
                        if "base64," in u:
                            img_bytes = _extract_image_bytes(u)
                        elif u.startswith("http"):
                            pass

    if not img_bytes:
        raise HTTPException(status_code=400, detail="No base64 image found in message content")

    res = await client.analyze(
        img_bytes=img_bytes,
        prompt_text=prompt_text,
        sex=sex,
        expect="scoring" if "point_qa" not in prompt_text else "point_qa",
    )

    reply_str = json.dumps(res, ensure_ascii=False)
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model or "deepseek-v4.1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply_str},
                "finish_reason": "stop",
            }
        ],
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", os.environ.get("DEEPSEEK_BRIDGE_PORT", 8000)))
    uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
