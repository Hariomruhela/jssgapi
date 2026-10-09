from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.exceptions import BadRequestException, ServiceUnavailableException
from app.core.responses import ok
from app.services.translate_service import (
    DEFAULT_MAX_LENGTH,
    SUPPORTED_LANGUAGES,
    translate_text,
)

router = APIRouter(prefix="/translate", tags=["Translation"])


class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=DEFAULT_MAX_LENGTH)
    target_language: str = Field(..., description="Target language code, e.g., 'hi' or 'en'")
    source_language: str | None = Field(
        default=None,
        description="Optional source language code; if not provided, auto-detection may be used",
    )


class TranslateResponse(BaseModel):
    originalText: str
    translatedText: str
    targetLanguage: str
    sourceLanguage: str | None = None
    detectedLanguage: str | None = None


@router.post("", response_model=TranslateResponse)
async def translate(body: TranslateRequest) -> TranslateResponse:
    try:
        result = translate_text(
            text=body.text,
            target_language=body.target_language,
            source_language=body.source_language,
            max_length=DEFAULT_MAX_LENGTH,
        )
    except BadRequestException as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc.detail)) from exc
    except ServiceUnavailableException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc.detail),
        ) from exc
    except Exception as exc:
        # Never expose raw exceptions or credentials
        raise HTTPException(status_code=503, detail="Translation service unavailable") from exc

    return TranslateResponse(
        originalText=result.original_text,
        translatedText=result.translated_text,
        targetLanguage=result.target_language,
        sourceLanguage=result.source_language or None,
        detectedLanguage=result.detected_language,
    )
