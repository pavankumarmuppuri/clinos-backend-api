"""
routes/match_routes.py — ClinOS TrialMatch AI Matching Router (Firebase version)
Complete file with all functions including Gemini re-ranking.
"""
import json
import logging
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import firebase_config
from firebase_admin import firestore
from auth import get_current_doctor

logger      = logging.getLogger(__name__)
router      = APIRouter(prefix="/api/match", tags=["Coordinators"])
firebase_db = firestore.client()

# ── Environment ───────────────────────────────────────────────────────────────
ENGINE        = os.getenv("CLINOS_ENGINE", "tfidf")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL_NAME    = os.getenv("CLINOS_MODEL", "neuml/pubmedbert-base-embeddings")
CTGOV_BASE    = "https://clinicaltrials.gov/api/v2/studies"
GOOGLE_KEY    = os.getenv("GOOGLE_API_KEY", "")

_embedder = None


def get_embedder():
    global _embedder
    if _embedder is not None:
        return _embedder
    if ENGINE == "tfidf":
        return None
    try:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(MODEL_NAME)
        return _embedder
    except Exception as exc:
        logger.warning("Embedding model load failed (%s) — using TF-IDF", exc)
        return None


# ── Schemas ───────────────────────────────────────────────────────────────────
class AdhocPatient(BaseModel):
    name: str
    age: int
    sex: str
    conditions: list[str]
    prior_treatments: list[str] = []
    prior_treatment_note: str = ""
    medications: list[str] = []
    lab_values: dict[str, str] = {}

class TrialInput(BaseModel):
    nct_id: str
    title: str
    inclusion: str = ""
    exclusion: str = ""
    combined_text: str = ""

class ScoreRequest(BaseModel):
    patient_id: str = ""
    patient_name: str = ""
    cancer_type: str = ""
    stage: str = ""
    ecog: str = ""
    biomarkers: str = ""
    notes: str = ""
    trials: list[TrialInput]

class TrialMatch(BaseModel):
    nct_id: str
    title: str
    eligibility_pct: int
    raw_similarity: float
    engine: str
    inclusion: str = ""
    exclusion: str = ""
    claude_verdict: Optional[str] = None
    claude_confidence: Optional[int] = None
    inclusion_met: list[str] = []
    exclusions_triggered: list[str] = []
    claude_reasoning: Optional[str] = None

class MatchResponse(BaseModel):
    patient_id: str = ""
    patient_name: str
    engine_used: str
    trials_fetched: int
    top_matches: list[TrialMatch]

class ChatMessage(BaseModel):
    role: str = "user"
    content: str = ""

class ChatRequest(BaseModel):
    messages: list[ChatMessage] = []
    patient_context: str = ""
    system_prompt: str = ""
    system: str = ""

class ChatResponse(BaseModel):
    reply: str


# ── Rerank Prompt ─────────────────────────────────────────────────────────────
_RERANK_PROMPT = (
    "You are an oncology clinical trial eligibility specialist.\n"
    "Evaluate each trial for this patient. Return ONLY valid JSON, no markdown:\n"
    '{{"rankings":[{{"nct_id":"NCT...","verdict":"ELIGIBLE","confidence":85,'
    '"inclusion_met":["criterion"],"exclusions_triggered":[],"reasoning":"rationale"}}]}}\n'
    "Verdicts: ELIGIBLE | POTENTIALLY ELIGIBLE | INELIGIBLE\n\n"
    "PATIENT: {patient_text}\n\n"
    "CANDIDATE TRIALS:\n{trials_text}"
)


# ── Routes ────────────────────────────────────────────────────────────────────
@router.get("/engine")
async def engine_status():
    embedder = get_embedder()
    rerank   = "gemini" if GOOGLE_KEY else ("claude" if ANTHROPIC_KEY else "inactive")
    return {
        "engine":           "embedding" if embedder else "tfidf-fallback",
        "embedding_model":  MODEL_NAME if embedder else None,
        "claude_reranking": rerank,
        "rerank_provider":  "Gemini 2.5 Flash" if GOOGLE_KEY else ("Claude Sonnet" if ANTHROPIC_KEY else "none — set GOOGLE_API_KEY"),
    }


@router.post("/score", response_model=MatchResponse)
async def score_trials(payload: ScoreRequest, current_user=Depends(get_current_doctor)):
    if not payload.trials:
        return MatchResponse(patient_id=payload.patient_id, patient_name=payload.patient_name,
                             engine_used="none", trials_fetched=0, top_matches=[])

    cancer = payload.cancer_type or "cancer"
    stage  = payload.stage or ""
    ecog   = payload.ecog or ""
    bm     = payload.biomarkers or ""
    notes  = payload.notes or ""

    patient_text = (
        f"Patient diagnosis: {cancer} {stage}. Cancer type: {cancer}. "
        f"ECOG performance status: {ecog}. Biomarkers: {bm}. "
        f"Clinical history: {notes}. Diagnosis: {cancer} {stage} {bm}."
    )

    trials = []
    for t in payload.trials:
        inc = t.inclusion or ""
        exc = t.exclusion or ""
        if not inc and not exc and t.combined_text:
            inc = t.combined_text
        combined = f"Trial: {t.title}. Inclusion Criteria: {inc} Exclusion Criteria: {exc}"
        trials.append({"nct_id": t.nct_id, "title": t.title,
                        "inclusion": inc, "exclusion": exc, "combined_text": combined})

    candidates = match_with_embeddings(patient_text, trials, top_k=8)
    final      = await claude_rerank(patient_text, candidates)
    final      = final[:3]

    return MatchResponse(
        patient_id=payload.patient_id,
        patient_name=payload.patient_name,
        engine_used=final[0].get("engine", "tfidf") if final else "none",
        trials_fetched=len(trials),
        top_matches=[_build_trial_match(t) for t in final],
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, current_user=Depends(get_current_doctor)):
    if not payload.messages:
        return ChatResponse(reply="Please send a message.")
    if not GOOGLE_KEY and not ANTHROPIC_KEY:
        return ChatResponse(reply="Add GOOGLE_API_KEY or ANTHROPIC_API_KEY to your .env file.")

    sys_prompt = payload.system_prompt or payload.system or (
        "You are TrialMatch AI for ClinOS. Help with patients, trials, eligibility, and matching. Be concise."
    )

    if GOOGLE_KEY:
        try:
            full_contents = [
                {"role": "user",  "parts": [{"text": sys_prompt}]},
                {"role": "model", "parts": [{"text": "Understood. I am TrialMatch AI, ready to help."}]},
            ]
            for m in payload.messages:
                full_contents.append({
                    "role":  "user" if m.role == "user" else "model",
                    "parts": [{"text": m.content}],
                })
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.5-flash-lite:generateContent?key={GOOGLE_KEY}"
            )
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(url, json={
                    "contents": full_contents,
                    "generationConfig": {
                        "temperature": 0.7,
                        "maxOutputTokens": 600,
                        "thinkingConfig": {"thinkingBudget": 0},
                    },
                })
                resp.raise_for_status()
                data = resp.json()
            return ChatResponse(reply=data["candidates"][0]["content"]["parts"][0]["text"])
        except Exception as exc:
            logger.warning("Gemini chat failed: %s", exc)
            return ChatResponse(reply=f"Chat error: {str(exc)}")

    if ANTHROPIC_KEY:
        try:
            import anthropic
            client  = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            message = client.messages.create(
                model="claude-sonnet-4-20250514", max_tokens=600, system=sys_prompt,
                messages=[{"role": m.role, "content": m.content} for m in payload.messages],
            )
            return ChatResponse(reply=message.content[0].text)
        except Exception as exc:
            return ChatResponse(reply=f"Chat error: {str(exc)}")

    return ChatResponse(reply="No AI provider configured.")


@router.post("/adhoc", response_model=MatchResponse)
async def match_adhoc(payload: AdhocPatient, top_k: int = 3,
                      current_user=Depends(get_current_doctor)):
    return await run_match_pipeline(payload.model_dump(), patient_id="", top_k=top_k)


@router.post("/{patient_id}", response_model=MatchResponse)
async def match_patient_by_id(patient_id: str, top_k: int = 3,
                               current_user=Depends(get_current_doctor)):
    doc = firebase_db.collection("patients").document(str(patient_id)).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    return await run_match_pipeline(db_patient_to_dict(doc.to_dict()),
                                    patient_id=patient_id, top_k=top_k)


# ── Helpers ───────────────────────────────────────────────────────────────────
def db_patient_to_dict(data: dict) -> dict:
    cancer_type = data.get("cancer_type", "")
    conditions  = [cancer_type] if cancer_type else data.get("conditions", ["cancer"])
    if data.get("stage"):
        conditions.append(f"Stage {data['stage']}")
    if data.get("diagnosis_description"):
        conditions.append(data["diagnosis_description"])
    for bm in data.get("biomarkers", []):
        n, v = bm.get("name"), bm.get("value")
        if n and v:
            conditions.append(f"{n} {v}")
    lab_values = {}
    if data.get("ecog_status") is not None:
        lab_values["ECOG Performance Status"] = str(data["ecog_status"])
    return {
        "name":                 data.get("name", "Unknown"),
        "age":                  data.get("age", 0) or 0,
        "sex":                  data.get("gender", "Unknown") or "Unknown",
        "conditions":           conditions or ["oncology"],
        "prior_treatments":     [data["additional_notes"]] if data.get("additional_notes") else [],
        "prior_treatment_note": "",
        "medications":          [],
        "lab_values":           lab_values,
    }


def patient_to_text(p: dict) -> str:
    conditions = ", ".join(p.get("conditions", []))
    meds       = ", ".join(p.get("medications", []))
    labs       = "; ".join(f"{k}: {v}" for k, v in p.get("lab_values", {}).items())
    prior      = "; ".join(p.get("prior_treatments", [])) or "None"
    note       = p.get("prior_treatment_note", "")
    text = (
        f"{p['name']} is a {p.get('age', 0)}-year-old "
        f"{(p.get('sex') or 'unknown').lower()} with diagnosis: {conditions}. "
        f"Prior treatments: {prior}. "
    )
    if note: text += f"Clinical context: {note}. "
    if meds: text += f"Current medications: {meds}. "
    if labs: text += f"Lab values: {labs}."
    return text


def _tfidf_score_to_pct(score: float) -> int:
    floor, ceiling = 0.01, 0.10
    if score <= floor:
        return 0
    return int(max(0, min(100, round((score - floor) / (ceiling - floor) * 100))))


def _embedding_score_to_pct(score: float) -> int:
    floor, ceiling = 0.50, 0.92
    if score <= floor:
        return 0
    return int(max(0, min(100, round((score - floor) / (ceiling - floor) * 100))))


def match_with_embeddings(patient_text: str, trials: list[dict], top_k: int = 5) -> list[dict]:
    embedder = get_embedder()
    if embedder is None:
        return _match_tfidf(patient_text, trials, top_k)
    all_texts  = [patient_text] + [t["combined_text"] for t in trials]
    embeddings = embedder.encode(
        all_texts, batch_size=32, show_progress_bar=False,
        convert_to_numpy=True, normalize_embeddings=True,
    )
    raw_scores = embeddings[1:] @ embeddings[0]
    results    = [
        {**t,
         "eligibility_pct": _embedding_score_to_pct(float(raw_scores[i])),
         "raw_similarity":  round(float(raw_scores[i]), 4),
         "engine":          "embedding"}
        for i, t in enumerate(trials)
    ]
    results.sort(key=lambda x: x["eligibility_pct"], reverse=True)
    return results[:top_k]


def _match_tfidf(patient_text: str, trials: list[dict], top_k: int) -> list[dict]:
    if not trials:
        return []
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as sk_cos
    all_texts  = [patient_text] + [t["combined_text"] for t in trials]
    vec        = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
    matrix     = vec.fit_transform(all_texts)
    raw_scores = sk_cos(matrix[0], matrix[1:]).flatten()
    results    = [
        {**t,
         "eligibility_pct": _tfidf_score_to_pct(float(raw_scores[i])),
         "raw_similarity":  round(float(raw_scores[i]), 4),
         "engine":          "tfidf"}
        for i, t in enumerate(trials)
    ]
    results.sort(key=lambda x: x["eligibility_pct"], reverse=True)
    return results[:top_k]


async def claude_rerank(patient_text: str, candidates: list[dict]) -> list[dict]:
    if GOOGLE_KEY:
        return await _gemini_rerank(patient_text, candidates)
    if ANTHROPIC_KEY:
        return await _claude_rerank(patient_text, candidates)
    return candidates


async def _gemini_rerank(patient_text: str, candidates: list[dict]) -> list[dict]:
    trials_text = "\n\n".join(
        f"[{t['nct_id']}] {t['title']}\n"
        f"INCLUSION: {t['inclusion'][:400]}\n"
        f"EXCLUSION: {t['exclusion'][:250]}"
        for t in candidates
    )
    prompt = _RERANK_PROMPT.format(patient_text=patient_text, trials_text=trials_text)
    try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash-lite:generateContent?key={GOOGLE_KEY}"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 4000,
                    "thinkingConfig": {"thinkingBudget": 0},
                },
            })
            resp.raise_for_status()
            data = resp.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        raw = raw.replace("```json", "").replace("```", "").strip()
        s, e = raw.find("{"), raw.rfind("}")
        if s > -1 and e > s:
            raw = raw[s:e + 1]
        parsed   = json.loads(raw)
        rankings = {r["nct_id"]: r for r in parsed.get("rankings", [])}
        order    = {"ELIGIBLE": 0, "POTENTIALLY ELIGIBLE": 1, "INELIGIBLE": 2}
        enriched = [
            {**c,
             "claude_verdict":       rankings.get(c["nct_id"], {}).get("verdict"),
             "claude_confidence":    rankings.get(c["nct_id"], {}).get("confidence"),
             "inclusion_met":        rankings.get(c["nct_id"], {}).get("inclusion_met", []),
             "exclusions_triggered": rankings.get(c["nct_id"], {}).get("exclusions_triggered", []),
             "claude_reasoning":     rankings.get(c["nct_id"], {}).get("reasoning"),
             "eligibility_pct":      rankings.get(c["nct_id"], {}).get("confidence", c["eligibility_pct"]),
             "engine":               "gemini+tfidf"}
            for c in candidates
        ]
        enriched.sort(key=lambda x: (
            order.get(x.get("claude_verdict", "INELIGIBLE"), 2),
            -x["eligibility_pct"],
        ))
        logger.info("Gemini re-ranking complete for %d candidates", len(enriched))
        return enriched
    except Exception as exc:
        logger.warning("Gemini re-ranking failed: %s", exc)
        return candidates


async def _claude_rerank(patient_text: str, candidates: list[dict]) -> list[dict]:
    trials_text = "\n\n".join(
        f"[{t['nct_id']}] {t['title']}\n"
        f"INCLUSION: {t['inclusion'][:800]}\n"
        f"EXCLUSION: {t['exclusion'][:500]}"
        for t in candidates
    )
    try:
        import anthropic
        client  = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=2000,
            messages=[{"role": "user", "content":
                       _RERANK_PROMPT.format(patient_text=patient_text, trials_text=trials_text)}],
        )
        raw      = message.content[0].text.replace("```json", "").replace("```", "").strip()
        parsed   = json.loads(raw)
        rankings = {r["nct_id"]: r for r in parsed.get("rankings", [])}
        order    = {"ELIGIBLE": 0, "POTENTIALLY ELIGIBLE": 1, "INELIGIBLE": 2}
        enriched = [
            {**c,
             "claude_verdict":       rankings.get(c["nct_id"], {}).get("verdict"),
             "claude_confidence":    rankings.get(c["nct_id"], {}).get("confidence"),
             "inclusion_met":        rankings.get(c["nct_id"], {}).get("inclusion_met", []),
             "exclusions_triggered": rankings.get(c["nct_id"], {}).get("exclusions_triggered", []),
             "claude_reasoning":     rankings.get(c["nct_id"], {}).get("reasoning"),
             "eligibility_pct":      rankings.get(c["nct_id"], {}).get("confidence", c["eligibility_pct"]),
             "engine":               "claude+embedding"}
            for c in candidates
        ]
        enriched.sort(key=lambda x: (
            order.get(x.get("claude_verdict", "INELIGIBLE"), 2),
            -x["eligibility_pct"],
        ))
        return enriched
    except Exception as exc:
        logger.warning("Claude re-ranking failed: %s", exc)
        return candidates


def _build_trial_match(t: dict) -> TrialMatch:
    return TrialMatch(
        nct_id=t.get("nct_id", ""),
        title=t.get("title", ""),
        eligibility_pct=t.get("eligibility_pct", 0),
        raw_similarity=t.get("raw_similarity", 0.0),
        engine=t.get("engine", "tfidf"),
        inclusion=t.get("inclusion", ""),
        exclusion=t.get("exclusion", ""),
        claude_verdict=t.get("claude_verdict"),
        claude_confidence=t.get("claude_confidence"),
        inclusion_met=t.get("inclusion_met", []),
        exclusions_triggered=t.get("exclusions_triggered", []),
        claude_reasoning=t.get("claude_reasoning"),
    )


async def run_match_pipeline(patient_dict: dict, patient_id: str = "", top_k: int = 3) -> MatchResponse:
    query  = patient_dict.get("conditions", ["cancer"])[0]
    params = {
        "query.cond":           query,
        "filter.overallStatus": "RECRUITING",
        "pageSize":             25,
        "fields":               "NCTId,BriefTitle,EligibilityModule",
        "format":               "json",
    }
    trials = []
    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        ) as client:
            resp = await client.get(CTGOV_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()
        for study in data.get("studies", []):
            try:
                proto    = study.get("protocolSection", {})
                id_mod   = proto.get("identificationModule", {})
                elig     = proto.get("eligibilityModule", {})
                nct_id   = id_mod.get("nctId", "N/A")
                title    = id_mod.get("briefTitle", "").strip()
                raw_crit = elig.get("eligibilityCriteria", "")
                if not raw_crit or len(raw_crit) < 300:
                    continue
                lower = raw_crit.lower()
                inc_s = next((lower.find(m) for m in ["inclusion criteria", "inclusion:"] if m in lower), -1)
                exc_s = next((lower.find(m) for m in ["exclusion criteria", "exclusion:"] if m in lower), -1)
                if inc_s != -1 and exc_s != -1:
                    inc = (raw_crit[inc_s:exc_s] if inc_s < exc_s else raw_crit[inc_s:]).strip()
                    exc = (raw_crit[exc_s:] if inc_s < exc_s else raw_crit[exc_s:inc_s]).strip()
                elif inc_s != -1:
                    inc, exc = raw_crit[inc_s:].strip(), ""
                else:
                    inc, exc = raw_crit.strip(), ""
                trials.append({
                    "nct_id": nct_id, "title": title,
                    "inclusion": inc, "exclusion": exc,
                    "combined_text": f"Trial: {title}. Inclusion: {inc} Exclusion: {exc}",
                })
            except Exception:
                continue
    except Exception as exc:
        logger.warning("ClinicalTrials.gov fetch failed: %s", exc)

    if not trials:
        return MatchResponse(patient_id=patient_id, patient_name=patient_dict["name"],
                             engine_used="none", trials_fetched=0, top_matches=[])

    patient_text = patient_to_text(patient_dict)
    candidates   = match_with_embeddings(patient_text, trials, top_k=5)
    final        = await claude_rerank(patient_text, candidates)
    final        = final[:top_k]

    return MatchResponse(
        patient_id=patient_id,
        patient_name=patient_dict["name"],
        engine_used=final[0].get("engine", "tfidf") if final else "none",
        trials_fetched=len(trials),
        top_matches=[_build_trial_match(t) for t in final],
    )