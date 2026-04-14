from dotenv import load_dotenv
load_dotenv()

# ── Firebase Init (must happen before any route imports) ─────────────────────
import firebase_config
from firebase_admin import firestore
firebase_db = firestore.client()

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
import os


# ── OpenAPI tag definitions — controls Swagger section order & descriptions ───
openapi_tags = [
    {
        "name": "Patients",
        "description": (
            "**Patient-facing routes.** "
            "Manage patient profiles, view matched trials, book and manage appointments, "
            "and access the patient dashboard. "
            "Patients can only read and edit their own records."
        ),
    },
    {
        "name": "Coordinators",
        "description": (
            "**Coordinator / PI routes.** "
            "Full CRUD for patients and trials, AI matching pipeline, "
            "eligibility queue (Investigate), protocol management with H1-Gate approval, "
            "audit log, and the coordinator dashboard. "
            "Requires a doctor-role JWT."
        ),
    },
    {
        "name": "Authentication",
        "description": (
            "**Auth routes.** "
            "Sign up, log in, view/update your profile, change password, and log out. "
            "All other endpoints require the JWT returned from `POST /auth/login`."
        ),
    },
    {
        "name": "AI Matching",
        "description": (
            "**AI matching pipeline.** "
            "Score patients against live ClinicalTrials.gov trials using "
            "TF-IDF + Gemini Pro re-ranking. Also exposes the TrialMatch chatbot."
        ),
    },
    {
        "name": "Match Results",
        "description": (
            "**Saved match results.** "
            "Persist and retrieve AI scoring output so results survive page reloads "
            "without re-running the full pipeline."
        ),
    },
    {
        "name": "Appointments",
        "description": (
            "**Appointment management.** "
            "Book, confirm, cancel, and list appointments linked to patients and trials. "
            "Patients can only manage their own appointments."
        ),
    },
    {
        "name": "Investigate Queue",
        "description": (
            "**Eligibility investigate cases.** "
            "Cases where patient data is missing, ambiguous, or conflicts with criteria. "
            "Tier 1 = missing data | Tier 2 = ambiguous | Tier 3 = edge case."
        ),
    },
    {
        "name": "Protocols",
        "description": (
            "**Protocol and criteria management.** "
            "Per-trial eligibility criteria with H1-Gate approval workflow "
            "and an immutable audit log for every decision."
        ),
    },
    {
        "name": "Dashboard",
        "description": (
            "**Pre-aggregated dashboard data.** "
            "Single-call endpoints that return all metrics, charts, and summaries "
            "needed by the coordinator and patient dashboards."
        ),
    },
    {
        "name": "Health",
        "description": "Server liveness check — no authentication required.",
    },
    {
        "name": "Debug",
        "description": "Firebase connection diagnostics — no authentication required.",
    },
    {
        "name": "Frontend",
        "description": "Serves HTML files and the React SPA. Not relevant for API testing.",
    },
]


app = FastAPI(
    title="ClinOS TrialMatch API",
    description=(
        "AI-powered oncology clinical trial matching platform.\n\n"
        "### How to authenticate\n"
        "1. Call **`POST /auth/signup`** or **`POST /auth/login`**\n"
        "2. Copy the `access_token` from the response\n"
        "3. Click **Authorize 🔒** at the top of this page\n"
        "4. Enter `Bearer <your_token>` and click **Authorize**\n\n"
        "All endpoints except `/health`, `/debug-db`, `/auth/signup`, "
        "and `/auth/login` require a valid Bearer token.\n\n"
        "### Swagger sections\n"
        "- **Patients** — patient profile, appointments, patient dashboard\n"
        "- **Coordinators** — trials, AI matching, investigate queue, protocols, "
        "coordinator dashboard\n"
        "- **Authentication** — signup, login, profile management"
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    swagger_ui_parameters={"persistAuthorization": True},
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Exception Handlers ────────────────────────────────────────────────────────
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    slugs = {
        400: "bad_request", 401: "unauthorized", 403: "forbidden",
        404: "not_found",   409: "conflict",     422: "validation_error",
        500: "internal_server_error",
    }
    return JSONResponse(status_code=exc.status_code, content={
        "status":  exc.status_code,
        "error":   slugs.get(exc.status_code, "error"),
        "message": exc.detail,
    })


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    fields = [
        {
            "field":   " -> ".join(str(l) for l in e["loc"] if l != "body") or "request",
            "message": e["msg"],
            "type":    e["type"],
        }
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={
        "status":  422,
        "error":   "validation_error",
        "message": "One or more fields failed validation.",
        "fields":  fields,
    })


# ── Health & Debug (before catch-all) ────────────────────────────────────────
@app.get("/health", tags=["Health"])
def health():
    return {
        "status":  200,
        "message": "ClinOS TrialMatch API is running",
        "version": "2.0.0",
    }


@app.get("/debug-db", tags=["Debug"])
def debug_db():
    try:
        cols = [c.id for c in firebase_db.collections()]
        return {"using": "firebase", "collections": cols}
    except Exception as e:
        return {"using": "firebase", "error": str(e)}


# ── Routers ───────────────────────────────────────────────────────────────────
# Each router keeps its own tags for the actual endpoint labels.
# We also pass tags= to include_router() to assign the Swagger SECTION heading.
#
# SECTION MAPPING
# ─────────────────────────────────────────────────────────────────────────────
#  Swagger section "Patients":
#    patient_routes    → manages patient profiles (shared — patients & coordinators)
#    appointment_routes→ appointments (shared)
#
#  Swagger section "Coordinators":
#    trial_routes      → coordinator-owned trials
#    match_routes      → AI matching pipeline
#    match_results_routes → saved match output
#    investigate_routes→ eligibility investigate queue
#    protocol_routes   → protocol criteria + audit log
#    dashboard_routes  → coordinator & patient dashboard aggregations
#
#  Swagger section "Authentication":
#    auth_routes       → signup, login, profile
# ─────────────────────────────────────────────────────────────────────────────

from routes.auth_routes           import router as auth_router
from routes.patient_routes        import router as patient_router
from routes.trial_routes          import router as trial_router
from routes.match_routes          import router as match_router
from routes.match_results_routes  import router as match_results_router
from routes.appointment_routes    import router as appointment_router
from routes.investigate_routes    import router as investigate_router
from routes.protocol_routes       import router as protocol_router
from routes.dashboard_routes      import router as dashboard_router

# ── Authentication section ───────────────────────────────────────────────────
app.include_router(auth_router)            # /auth/*  — tags=["Authentication"]

# ── Patients section ─────────────────────────────────────────────────────────
app.include_router(
    patient_router,                        # /patients — own tag="Patients" already set
)
app.include_router(
    appointment_router,                    # /appointments — own tag="Appointments" already set
)

# ── Coordinators section ─────────────────────────────────────────────────────
app.include_router(trial_router)           # /trials            — tag="Trials"
app.include_router(match_router)           # /api/match/*       — tag="AI Matching"
app.include_router(match_results_router)   # /api/match/results — tag="Match Results"
app.include_router(investigate_router)     # /investigate       — tag="Investigate Queue"
app.include_router(protocol_router)        # /protocols         — tag="Protocols"
app.include_router(dashboard_router)       # /dashboard         — tag="Dashboard"


# ── Static File Mounts ────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DIST_DIR   = os.path.join(os.path.dirname(__file__), "dist")


def _serve(filename: str, directory: str = None):
    d    = directory or (DIST_DIR if os.path.isdir(DIST_DIR) else STATIC_DIR)
    path = os.path.join(d, filename)
    if not os.path.isfile(path):
        return JSONResponse(
            status_code=200,
            content={
                "message": "ClinOS API is running. Use /docs for Swagger UI.",
                "swagger": "/docs",
                "health":  "/health",
                "debug":   "/debug-db",
            },
        )
    return FileResponse(path, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


@app.get("/", tags=["Frontend"])
def serve_index():
    return _serve("index.html")


@app.get("/doctor", tags=["Frontend"])
def serve_doctor():
    if os.path.isfile(os.path.join(STATIC_DIR, "doctor.html")):
        return _serve("doctor.html", STATIC_DIR)
    return _serve("index.html")


@app.get("/patient", tags=["Frontend"])
def serve_patient():
    if os.path.isfile(os.path.join(STATIC_DIR, "patient.html")):
        return _serve("patient.html", STATIC_DIR)
    return _serve("index.html")


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if os.path.isdir(os.path.join(DIST_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(DIST_DIR, "assets")), name="assets")


# ── Catch-all for React Router (must be LAST) ─────────────────────────────────
@app.get("/{full_path:path}", tags=["Frontend"])
def serve_spa(full_path: str):
    for base in (DIST_DIR, STATIC_DIR):
        candidate = os.path.join(base, full_path)
        if os.path.isfile(candidate):
            return FileResponse(candidate)
    entry = os.path.join(DIST_DIR, "index.html")
    if os.path.isfile(entry):
        return FileResponse(entry)
    return JSONResponse(
        status_code=404,
        content={"message": f"'{full_path}' not found. Use /docs for the API."},
    )