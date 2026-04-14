from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import os

# ── Firebase Init (centralized) ──────────────────────────────────────────────
import firebase_config
from firebase_admin import firestore

firebase_db = firestore.client()

# ── Config ───────────────────────────────────────────────────────────────────
SECRET_KEY                 = os.getenv("SECRET_KEY", "fallback-secret-key-change-this")
ALGORITHM                  = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

# ── Password Hashing ─────────────────────────────────────────────────────────
pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = HTTPBearer()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ── JWT ──────────────────────────────────────────────────────────────────────
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire    = datetime.now(timezone.utc) + (
        expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload, ""
    except JWTError as e:
        return None, "token_expired" if "expired" in str(e).lower() else "token_invalid"


# ── Auth error helper ────────────────────────────────────────────────────────
def auth_error(http_status: int, error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"status": http_status, "error": error, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


# ── Dependency: get current user from Firebase "users" collection ────────────
async def get_current_doctor(credentials: HTTPAuthorizationCredentials = Depends(oauth2_scheme)) -> dict:
    token = credentials.credentials
    payload, reason = decode_access_token(token)

    if payload is None:
        raise auth_error(
            status.HTTP_401_UNAUTHORIZED,
            "token_expired" if reason == "token_expired" else "token_invalid",
            "Your session has expired. Please log in again."
            if reason == "token_expired"
            else "The token provided is invalid or malformed.",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "token_missing_subject",
                         "Token payload is missing the required subject claim.")

    # ✅ Look in "users" collection (same as auth_routes.py saves to)
    doc = firebase_db.collection("users").document(str(user_id)).get()
    if not doc.exists:
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "user_not_found",
                         "The account associated with this token no longer exists.")

    data = doc.to_dict()
    data["id"] = doc.id
    return data
