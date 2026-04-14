from pydantic import BaseModel, EmailStr, validator
from typing import List, Optional
from datetime import datetime
from enum import Enum


class EligibilityTypeEnum(str, Enum):
    inclusion = "inclusion"
    exclusion = "exclusion"


# ─── Auth ─────────────────────────────────────────────────────────────────────

class DoctorSignup(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "doctor"   # "doctor" or "patient"

    @validator("password")
    def password_min_length(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v

    @validator("role")
    def role_must_be_valid(cls, v):
        if v not in ("doctor", "patient"):
            return "doctor"
        return v


class DoctorLogin(BaseModel):
    email: EmailStr
    password: str


class DoctorOut(BaseModel):
    id: int
    name: str
    email: str
    role: str = "doctor"   # included in all auth responses
    created_at: datetime

    class Config:
        from_attributes = True   # Pydantic v2 (replaces orm_mode)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    doctor: DoctorOut


# ─── Biomarkers ───────────────────────────────────────────────────────────────

class BiomarkerIn(BaseModel):
    name: str
    value: str


class BiomarkerOut(BaseModel):
    id: int
    name: str
    value: str
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Patients ─────────────────────────────────────────────────────────────────

class PatientCreate(BaseModel):
    mrn: str
    name: str
    age: int
    gender: str
    ecog_status: Optional[str] = None
    cancer_type: str
    stage: Optional[str] = None
    diagnosis_description: Optional[str] = None
    location: Optional[str] = None
    additional_notes: Optional[str] = None
    biomarkers: List[BiomarkerIn] = []

    @validator("age")
    def age_must_be_positive(cls, v):
        if v <= 0 or v > 130:
            raise ValueError("Age must be between 1 and 130")
        return v


class PatientOut(BaseModel):
    id: int
    mrn: str
    name: str
    age: int
    gender: str
    ecog_status: Optional[str]
    cancer_type: str
    stage: Optional[str]
    diagnosis_description: Optional[str]
    location: Optional[str]
    additional_notes: Optional[str]
    created_by: int
    created_at: datetime
    biomarkers: List[BiomarkerOut] = []

    class Config:
        from_attributes = True


# ─── Trials ───────────────────────────────────────────────────────────────────

class EligibilityCriteria(BaseModel):
    inclusion: List[str] = []
    exclusion: List[str] = []


class TrialCreate(BaseModel):
    nct_id: str
    short_title: str
    full_title: Optional[str] = None
    phase: Optional[str] = None
    status: Optional[str] = None
    target_enrollment: Optional[int] = None
    sponsor: Optional[str] = None
    description: Optional[str] = None
    conditions: List[str] = []
    interventions: List[str] = []
    eligibility: EligibilityCriteria = EligibilityCriteria()


class TrialOut(BaseModel):
    id: int
    nct_id: str
    short_title: str
    full_title: Optional[str]
    phase: Optional[str]
    status: Optional[str]
    target_enrollment: Optional[int]
    sponsor: Optional[str]
    description: Optional[str]
    created_by: int
    created_at: datetime
    conditions: List[str] = []
    interventions: List[str] = []
    eligibility: EligibilityCriteria = EligibilityCriteria()

    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    message: str
