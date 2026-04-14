"""
utils.py — Shared helper functions for ClinOS API routes.
Provides consistent JSON response envelopes and error raising.
"""
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from typing import Any, Optional


# ── Success envelope ──────────────────────────────────────────────────────────
def success(
    data: Any = None,
    message: str = "OK",
    http_status: int = 200,
) -> JSONResponse:
    """
    Return a consistent success JSON envelope:
    {
        "status":  200,
        "message": "...",
        "data":    ...
    }
    """
    return JSONResponse(
        status_code=http_status,
        content={
            "status":  http_status,
            "message": message,
            "data":    data,
        },
    )


# ── Error helper ──────────────────────────────────────────────────────────────
def route_error(
    http_status: int,
    error: str,
    message: str,
) -> HTTPException:
    """
    Raise a structured HTTP exception that the global exception handler
    will convert into:
    {
        "status":  4xx,
        "error":   "snake_case_slug",
        "message": "Human-readable message."
    }
    """
    return HTTPException(
        status_code=http_status,
        detail={
            "status":  http_status,
            "error":   error,
            "message": message,
        },
    )
