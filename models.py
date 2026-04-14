from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Enum
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base
import enum


# ─── Enums ────────────────────────────────────────────────────────────────────

class EligibilityType(str, enum.Enum):
    inclusion = "inclusion"
    exclusion = "exclusion"


# ─── Doctor ───────────────────────────────────────────────────────────────────

class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(50), default="doctor", nullable=False, server_default="doctor")  # "doctor" or "patient"
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    patients = relationship("Patient", back_populates="doctor", cascade="all, delete-orphan")
    trials = relationship("Trial", back_populates="doctor", cascade="all, delete-orphan")


# ─── Patient ──────────────────────────────────────────────────────────────────

class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True, index=True)
    mrn = Column(String(100), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    age = Column(Integer, nullable=False)
    gender = Column(String(50), nullable=False)
    ecog_status = Column(String(50), nullable=True)
    cancer_type = Column(String(255), nullable=False)
    stage = Column(String(100), nullable=True)
    diagnosis_description = Column(Text, nullable=True)
    location = Column(String(255), nullable=True)
    additional_notes = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    doctor = relationship("Doctor", back_populates="patients")
    biomarkers = relationship("Biomarker", back_populates="patient", cascade="all, delete-orphan")
    trial_links = relationship("TrialPatient", back_populates="patient", cascade="all, delete-orphan")


# ─── Biomarker ────────────────────────────────────────────────────────────────

class Biomarker(Base):
    __tablename__ = "biomarkers"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    name = Column(String(255), nullable=False)
    value = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    patient = relationship("Patient", back_populates="biomarkers")


# ─── Trial ────────────────────────────────────────────────────────────────────

class Trial(Base):
    __tablename__ = "trials"

    id = Column(Integer, primary_key=True, index=True)
    nct_id = Column(String(50), unique=True, index=True, nullable=False)
    short_title = Column(String(255), nullable=False)
    full_title = Column(Text, nullable=True)
    phase = Column(String(50), nullable=True)
    status = Column(String(100), nullable=True)
    target_enrollment = Column(Integer, nullable=True)
    sponsor = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("doctors.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    doctor = relationship("Doctor", back_populates="trials")
    conditions = relationship("TrialCondition", back_populates="trial", cascade="all, delete-orphan")
    interventions = relationship("TrialIntervention", back_populates="trial", cascade="all, delete-orphan")
    eligibility = relationship("Eligibility", back_populates="trial", cascade="all, delete-orphan")
    patient_links = relationship("TrialPatient", back_populates="trial", cascade="all, delete-orphan")


# ─── TrialCondition ───────────────────────────────────────────────────────────

class TrialCondition(Base):
    __tablename__ = "trial_conditions"

    id = Column(Integer, primary_key=True, index=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    condition_name = Column(String(255), nullable=False)

    trial = relationship("Trial", back_populates="conditions")


# ─── TrialIntervention ────────────────────────────────────────────────────────

class TrialIntervention(Base):
    __tablename__ = "trial_interventions"

    id = Column(Integer, primary_key=True, index=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    intervention_name = Column(String(255), nullable=False)

    trial = relationship("Trial", back_populates="interventions")


# ─── Eligibility ──────────────────────────────────────────────────────────────

class Eligibility(Base):
    __tablename__ = "eligibility"

    id = Column(Integer, primary_key=True, index=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    type = Column(Enum(EligibilityType), nullable=False)
    description = Column(Text, nullable=False)

    trial = relationship("Trial", back_populates="eligibility")


# ─── TrialPatient (Link Table) ────────────────────────────────────────────────

class TrialPatient(Base):
    __tablename__ = "trial_patients"

    id = Column(Integer, primary_key=True, index=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)

    trial = relationship("Trial", back_populates="patient_links")
    patient = relationship("Patient", back_populates="trial_links")
