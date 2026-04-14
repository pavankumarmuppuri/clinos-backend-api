from fastapi import APIRouter, Depends, status
from auth import (
    hash_password, verify_password, create_access_token, get_current_doctor
)
from utils import route_error, success
import firebase_config
from firebase_admin import firestore
from datetime import datetime
from pydantic import BaseModel, EmailStr
from typing import Optional

firebase_db = firestore.client()
router      = APIRouter(prefix="/auth", tags=["Authentication"])


# ── Schemas ───────────────────────────────────────────────────────────────────
class SignupPayload(BaseModel):
    name:     str
    email:    EmailStr
    password: str
    role:     str = "doctor"   # "doctor" | "patient"

class LoginPayload(BaseModel):
    email:    EmailStr
    password: str

class ChangePasswordPayload(BaseModel):
    current_password: str
    new_password:     str

class UpdateProfilePayload(BaseModel):
    name:  Optional[str] = None
    email: Optional[EmailStr] = None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _user_dict(data: dict, doc_id: str) -> dict:
    return {
        "id":         doc_id,
        "name":       data.get("name"),
        "email":      data.get("email"),
        "role":       data.get("role", "doctor"),
        "created_at": data.get("created_at", ""),
    }


# ── POST /auth/signup ─────────────────────────────────────────────────────────
@router.post("/signup", status_code=201)
def signup(payload: SignupPayload):
    """Register a new coordinator or patient account."""
    # Validate role
    role = payload.role if payload.role in ("doctor", "patient") else "doctor"

    # Check duplicate email
    existing = firebase_db.collection("users").where("email", "==", payload.email).get()
    if existing:
        raise route_error(
            status.HTTP_409_CONFLICT,
            "email_already_exists",
            f"An account with email '{payload.email}' already exists.",
        )

    # Validate password length
    if len(payload.password) < 6:
        raise route_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "password_too_short",
            "Password must be at least 6 characters.",
        )

    # Create user document
    user_data = {
        "name":          payload.name,
        "email":         payload.email,
        "password_hash": hash_password(payload.password),
        "role":          role,
        "created_at":    datetime.utcnow().isoformat(),
    }
    doc_ref       = firebase_db.collection("users").document()
    user_data["id"] = doc_ref.id
    doc_ref.set(user_data)

    token = create_access_token(data={"sub": doc_ref.id})
    return success(
        data={
            "access_token": token,
            "token_type":   "bearer",
            "doctor":       _user_dict(user_data, doc_ref.id),
        },
        message="Account created successfully.",
        http_status=201,
    )


# ── POST /auth/login ──────────────────────────────────────────────────────────
@router.post("/login", status_code=200)
def login(payload: LoginPayload):
    """Authenticate and receive a JWT token."""
    docs = firebase_db.collection("users").where("email", "==", payload.email).get()
    if not docs:
        raise route_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_credentials",
            "The email or password you entered is incorrect.",
        )

    data   = docs[0].to_dict()
    doc_id = docs[0].id

    if not verify_password(payload.password, data.get("password_hash", "")):
        raise route_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_credentials",
            "The email or password you entered is incorrect.",
        )

    token = create_access_token(data={"sub": doc_id})
    return success(
        data={
            "access_token": token,
            "token_type":   "bearer",
            "doctor":       _user_dict(data, doc_id),
        },
        message="Login successful.",
    )


# ── GET /auth/me ──────────────────────────────────────────────────────────────
@router.get("/me", status_code=200)
def get_me(current_user: dict = Depends(get_current_doctor)):
    """
    Return the profile of the currently authenticated user.
    Used by doctor.html and patient.html to restore sessions
    without storing sensitive data in sessionStorage.
    """
    return success(
        data=_user_dict(current_user, current_user["id"]),
        message="Profile retrieved successfully.",
    )


# ── PUT /auth/me ──────────────────────────────────────────────────────────────
@router.put("/me", status_code=200)
def update_me(
    payload: UpdateProfilePayload,
    current_user: dict = Depends(get_current_doctor),
):
    """Update display name or email for the logged-in user."""
    updates: dict = {}

    if payload.name:
        updates["name"] = payload.name

    if payload.email:
        # Check the new email is not taken by someone else
        existing = firebase_db.collection("users").where("email", "==", payload.email).get()
        for doc in existing:
            if doc.id != current_user["id"]:
                raise route_error(
                    status.HTTP_409_CONFLICT,
                    "email_already_exists",
                    f"The email '{payload.email}' is already in use.",
                )
        updates["email"] = payload.email

    if not updates:
        raise route_error(
            status.HTTP_400_BAD_REQUEST,
            "no_changes",
            "No fields to update were provided.",
        )

    updates["updated_at"] = datetime.utcnow().isoformat()
    firebase_db.collection("users").document(current_user["id"]).update(updates)

    updated = firebase_db.collection("users").document(current_user["id"]).get().to_dict()
    return success(
        data=_user_dict(updated, current_user["id"]),
        message="Profile updated successfully.",
    )


# ── POST /auth/change-password ────────────────────────────────────────────────
@router.post("/change-password", status_code=200)
def change_password(
    payload: ChangePasswordPayload,
    current_user: dict = Depends(get_current_doctor),
):
    """Change password — requires current password for verification."""
    if not verify_password(payload.current_password, current_user.get("password_hash", "")):
        raise route_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_current_password",
            "The current password you entered is incorrect.",
        )

    if len(payload.new_password) < 6:
        raise route_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "password_too_short",
            "New password must be at least 6 characters.",
        )

    firebase_db.collection("users").document(current_user["id"]).update({
        "password_hash": hash_password(payload.new_password),
        "updated_at":    datetime.utcnow().isoformat(),
    })
    return success(data={}, message="Password changed successfully.")


# ── POST /auth/logout ─────────────────────────────────────────────────────────
@router.post("/logout", status_code=200)
def logout(current_user: dict = Depends(get_current_doctor)):
    """
    Stateless logout — JWT tokens cannot be invalidated server-side.
    The client must discard the token from sessionStorage.
    This endpoint exists so the frontend can call it for audit logging.
    """
    return success(
        data={"user_id": current_user["id"]},
        message="Logged out successfully. Please discard your token on the client.",
    )
