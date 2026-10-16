"""llm-gateway-demo: simulatore di client accreditato + finto OpenAI ispezionabile + interfaccia web (porta 8090).

Il ciclo completo gira senza il backend DentalCare e, in modalita' mock, senza chiave OpenAI:
  UI -> /api/run -> (JWT ES256 firmato qui) -> gateway :8095 / :8096 -> /mock-openai (cattura il payload
  pseudonimizzato e risponde riusando i codici) -> gateway ripristina -> UI.
"""
import asyncio
import copy
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent


def load_demo_config() -> tuple[dict, Path | None]:
    """Cerca e carica config.yaml dalla cartella ./config_ner_demo (o percorsi alternativi)."""
    candidates = [
        Path(os.environ["DEMO_CONFIG"]) if os.environ.get("DEMO_CONFIG") else None,
        Path("/app/config_ner_demo/config.yaml"),
        Path("/app/config_ner_demo/config.yml"),
        ROOT / "config_ner_demo" / "config.yaml",
        ROOT / "config_ner_demo" / "config.yml",
        ROOT / "config.yaml",
    ]
    cfg_path = next((p for p in candidates if p is not None and p.is_file()), None)
    cfg: dict = {}
    if cfg_path is not None:
        try:
            import yaml
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            print(f"[WARN] Impossibile leggere configurazione {cfg_path}: {exc}")
    return cfg, cfg_path


_CFG, CONFIG_PATH = load_demo_config()

# Sezione server
_server_cfg = _CFG.get("server") or {}
DEFAULT_HOST = str(os.environ.get("HOST") or _server_cfg.get("host") or "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("PORT") or _server_cfg.get("port") or 8090)

# Sezione keys
_keys_cfg = _CFG.get("keys") or {}
KEYS_DIR = Path(os.environ.get("KEYS_DIR") or _keys_cfg.get("dir") or str(ROOT / "keys"))
PRIVATE_KEY = Path(os.environ.get("PRIVATE_KEY") or _keys_cfg.get("private_key") or str(KEYS_DIR / "client-private.pem"))
PUBLIC_KEY = Path(os.environ.get("PUBLIC_KEY") or _keys_cfg.get("public_key") or str(KEYS_DIR / "client-public.pem"))

# Sezione OpenAI
_openai_cfg = _CFG.get("openai") or {}
OPENAI_URL = str(os.environ.get("OPENAI_URL") or _openai_cfg.get("url") or "https://api.openai.com/v1/chat/completions")
OPENAI_API_KEY = str(os.environ.get("OPENAI_API_KEY") or _openai_cfg.get("api_key") or "").strip()

# Sezione gateways
_gw_cfg = _CFG.get("gateways") or {}
_ner_gw = _gw_cfg.get("llm_ner_gateway") or {}
_orig_gw = _gw_cfg.get("llm_gateway") or {}

LLM_GATEWAY_URL = str(os.environ.get("LLM_GATEWAY_URL") or _orig_gw.get("url") or "http://127.0.0.1:8095").rstrip("/")
LLM_GATEWAY_NER_URL = str(os.environ.get("LLM_GATEWAY_NER_URL") or _orig_gw.get("ner_url") or "http://127.0.0.1:8097").rstrip("/")
LLM_GATEWAY_LABEL = str(os.environ.get("LLM_GATEWAY_LABEL") or _orig_gw.get("label") or f"llm-gateway (Standard spaCy — {LLM_GATEWAY_URL})")
LLM_GATEWAY_SHORT_LABEL = str(os.environ.get("LLM_GATEWAY_SHORT_LABEL") or _orig_gw.get("short_label") or "llm-gateway (spaCy)")

LLM_NER_GATEWAY_URL = str(os.environ.get("LLM_NER_GATEWAY_URL") or _ner_gw.get("url") or "http://127.0.0.1:8096").rstrip("/")
LLM_NER_GATEWAY_NER_URL = str(os.environ.get("LLM_NER_GATEWAY_NER_URL") or _ner_gw.get("ner_url") or "http://127.0.0.1:8098").rstrip("/")
LLM_NER_GATEWAY_LABEL = str(os.environ.get("LLM_NER_GATEWAY_LABEL") or _ner_gw.get("label") or f"llm-ner-gateway (Dual-Engine / 0.3B — {LLM_NER_GATEWAY_URL})")
LLM_NER_GATEWAY_SHORT_LABEL = str(os.environ.get("LLM_NER_GATEWAY_SHORT_LABEL") or _ner_gw.get("short_label") or "llm-ner-gateway")

GATEWAYS = {
    "llm-gateway": {
        "label": LLM_GATEWAY_LABEL,
        "shortLabel": LLM_GATEWAY_SHORT_LABEL,
        "url": LLM_GATEWAY_URL,
        "checks": [("llm-gateway", f"{LLM_GATEWAY_URL}/actuator/health"),
                   ("llm-gateway-ner", f"{LLM_GATEWAY_NER_URL}/health")],
    },
    "llm-ner-gateway": {
        "label": LLM_NER_GATEWAY_LABEL,
        "shortLabel": LLM_NER_GATEWAY_SHORT_LABEL,
        "url": LLM_NER_GATEWAY_URL,
        "checks": [("llm-ner-gateway", f"{LLM_NER_GATEWAY_URL}/actuator/health"),
                   ("llm-ner-gateway-ner", f"{LLM_NER_GATEWAY_NER_URL}/health")],
    },
}

POLICIES = [
    {"id": "dentalcare/copilot-admin", "application": "dentalcare", "purpose": "copilot-admin",
     "profile": "copilot-standard", "label": "dentalcare / copilot-admin (Tokenize reversibile + filtro DTO amministrativo)"},
    {"id": "dentalcare/clinical-copilot", "application": "dentalcare", "purpose": "clinical-copilot",
     "profile": "copilot-standard", "label": "dentalcare / clinical-copilot (Tokenize reversibile + note cliniche/anamnesi)"},
    {"id": "dentalcare/smart-error", "application": "dentalcare", "purpose": "smart-error",
     "profile": "smart-error", "label": "dentalcare / smart-error (Redact irreversibile [NOME], [TELEFONO], [ID])"},
    {"id": "legal/case-analysis", "application": "legal", "purpose": "case-analysis",
     "profile": "copilot-standard", "label": "legal / case-analysis (Policy giurisprudenza su llm-ner-gateway)"},
]

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ALIAS_RE = {kind: re.compile(rf"\b{kind}_\d{{1,6}}\b") for kind in ("PERSONA", "TEL", "EMAIL")}
UNRESTORED_RE = re.compile(r"\b(?:PERSONA|TEL|EMAIL)_\d{1,6}\b")


def ensure_keys() -> None:
    """Coppia EC P-256 di sviluppo: se esiste la coppia di DentalCare la usa, altrimenti ne crea una."""
    if PRIVATE_KEY.exists() and PUBLIC_KEY.exists():
        return
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    dentalcare_priv = Path(r"D:\dentalcare\backend\config\llm-gateway-private.pem")
    gateway_pub = Path(r"D:\llm-gateway\config\client-public.pem")
    if dentalcare_priv.exists() and gateway_pub.exists():
        try:
            PRIVATE_KEY.write_bytes(dentalcare_priv.read_bytes())
            PUBLIC_KEY.write_bytes(gateway_pub.read_bytes())
            return
        except Exception:
            pass
    key = ec.generate_private_key(ec.SECP256R1())
    PRIVATE_KEY.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    PUBLIC_KEY.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


ensure_keys()
app = FastAPI(title="llm-gateway-demo", docs_url=None, redoc_url=None, openapi_url=None)

# Un'esecuzione alla volta: il finto OpenAI non sa quale gateway lo sta chiamando, quindi il payload
# catturato si attribuisce all'esecuzione in corso. Il confronto "entrambi" lancia i due gateway uno dopo l'altro.
_run_lock = asyncio.Lock()
_state: dict = {"captured": None, "mode": "mock", "hallucinate": False, "api_key": ""}


def sign_jwt(claims: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": claims.get("iss") or "dentalcare",
        "aud": "llm-gateway",
        "iat": now,
        "exp": now + timedelta(seconds=120),
        "application": claims["application"],
        "tenantRef": claims.get("tenantRef") or "tenant-demo",
        "actorRef": claims.get("actorRef") or "actor-demo",
        "conversationRef": claims["conversationRef"],
        "purpose": claims["purpose"],
    }
    return jwt.encode(payload, PRIVATE_KEY.read_text(), algorithm="ES256")


# ---------------------------------------------------------------- strumenti e costanti DentalCare (D:\dentalcare)

PATIENT_ROSSI_ID = "3f2c1a9e-8b7d-4c6e-9f10-112233445566"
PATIENT_FERRERO_ID = "7a91c4e2-5d3b-4f80-9a12-667788990011"
PATIENT_BENEDETTI_ID = "1c5e8b20-9a44-4d11-8e77-223344556677"
PATIENT_LUNARDI_ID = "9d4b2f10-3e6c-4a89-b123-445566778899"
PATIENT_BORTOLOTTI_ID = "5e7a1c33-2b4d-4e90-8c11-998877665544"
PATIENT_DAL_MOLIN_ID = "6f8b2d44-1a3c-4f70-9d22-887766554433"
PATIENT_BIANCHI_ID = "2b4d6f88-7c9e-4a12-8b34-556677889900"
PATIENT_CATTANEO_ID = "8a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
PROVIDER_BIANCHI_ID = "a1111111-2222-4333-8444-555555555555"
PROVIDER_GALLI_ID = "b2222222-3333-4444-8555-666666666666"
APPT_1_ID = "c3333333-4444-4555-8666-777777777777"
APPT_2_ID = "d4444444-5555-4666-8777-888888888888"
RECALL_1_ID = "e5555555-6666-4777-8888-999999999999"
RECALL_2_ID = "f6666666-7777-4888-8999-000000000000"
ESTIMATE_1_ID = "12345678-9abc-4def-8123-456789abcdef"
INVOICE_1_ID = "fedcba98-7654-4321-8fed-cba987654321"

TOOL_GET_PATIENT_DETAIL = {"type": "function", "function": {
    "name": "getPatientDetail",
    "description": "Recupera la scheda dettagliata di un paziente (PatientDetailDto) dato il suo UUID",
    "parameters": {"type": "object", "properties": {"patientId": {"type": "string"}}, "required": ["patientId"]}}}

TOOL_GET_DASHBOARD = {"type": "function", "function": {
    "name": "getDashboard",
    "description": "Restituisce i KPI dello studio e la lista todayAppointments (DashboardDto)",
    "parameters": {"type": "object", "properties": {}}}}

TOOL_GET_RECALLS = {"type": "function", "function": {
    "name": "getRecalls",
    "description": "Cerca i richiami periodici dei pazienti filtrati per stato e priorità (List<RecallDto>)",
    "parameters": {"type": "object", "properties": {
        "status": {"type": "string"}, "priority": {"type": "string"}}, "required": ["status"]}}}

TOOL_GET_INVOICES = {"type": "function", "function": {
    "name": "getInvoices",
    "description": "Recupera l'elenco delle fatture dello studio filtrate per stato (List<InvoiceDto>)",
    "parameters": {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}}}

TOOL_GET_ESTIMATES = {"type": "function", "function": {
    "name": "getEstimates",
    "description": "Recupera i preventivi di un paziente (List<EstimateDto>)",
    "parameters": {"type": "object", "properties": {
        "patientId": {"type": "string"}, "status": {"type": "string"}}, "required": ["patientId"]}}}

SYSTEM_ADMIN = (
    "Sei l'assistente AI (Copilot) dello studio dentistico DentalCare. "
    "Rispondi sempre in italiano in modo chiaro, conciso e professionale."
)
SYSTEM_CLINICAL = (
    "Sei il Copilot clinico di DentalCare a supporto dell'odontoiatra. "
    "Analizza anamnesi, allergie, farmaci e piani di cura in italiano in modo rigoroso."
)
SYSTEM_SMART_ERROR = (
    "Sei l'assistente tecnico di DentalCare. Spiega all'operatore in 2-3 frasi chiare in italiano "
    "perché l'operazione è fallita e come risolvere il problema."
)

SCENARIOS = [
    {
        "id": "short-command",
        "category": "DentalCare · Segreteria",
        "title": "1 · Comando breve e verbi di contatto",
        "description": (
            "Testa i comandi rapidi di segreteria con verbi imperativi («Chiama», «scrivi a»). "
            "Osserva nel pannello 2 come Rizzo-PII isoli esattamente «Giulia Verdi» in PERSONA_1 lasciando il verbo «Chiama» intatto, "
            "mentre spaCy in llm-gateway ingloba per errore il verbo nel nome («Chiama Giulia Verdi» → PERSONA_1)."
        ),
        "policy": "dentalcare/copilot-admin",
        "hallucinate": False,
        "sensitive": ["Giulia Verdi", "+39 333 123 4567", "giulia.verdi@email.it"],
        "payload": {"model": "copilot-standard", "messages": [
            {"role": "system", "content": SYSTEM_ADMIN + "\n[CONTESTO UI] Vista corrente: agenda."},
            {"role": "user", "content": "📞 Chiama Giulia Verdi al +39 333 123 4567 o scrivi a giulia.verdi@email.it "
                                        "per spostare l'appuntamento di igiene dentale di giovedì alle 15:00"}]},
    },
    {
        "id": "ui-context-patient-detail",
        "category": "DentalCare · Segreteria",
        "title": "2 · Contesto UI + PatientDetailDto (2 passate)",
        "description": (
            "Riproduce il flusso reale di ChatService.java in DentalCare: il system prompt contiene il [CONTESTO UI] col paziente aperto "
            "Mario Rossi, l'utente scrive solo il cognome «Rossi» (propagazione 2-pass sullo stesso PERSONA_1) e il tool getPatientDetail "
            "restituisce il PatientDetailDto completo. La policy copilot-admin elimina (DROP) 13 campi clinici e fiscali!"
        ),
        "policy": "dentalcare/copilot-admin",
        "hallucinate": False,
        "sensitive": [
            "Mario Rossi", "Rossi", "Dott. Luca Bianchi", "RSSMRA85M01H501Z", "IT60X0542811101000000123456",
            "01234567897", "+39 347 1122334", "mario.rossi@email.it", "Via Giuseppe Garibaldi 12",
            PATIENT_ROSSI_ID, PROVIDER_BIANCHI_ID,
        ],
        "payload": {"model": "copilot-standard", "tools": [TOOL_GET_PATIENT_DETAIL], "messages": [
            {"role": "system", "content": (
                SYSTEM_ADMIN + f"\n\n[CONTESTO UI] L'utente sta guardando il paziente Mario Rossi (patientId={PATIENT_ROSSI_ID}). "
                "Quando dice 'questo paziente' usa questo UUID senza ricerca.\n[CONTESTO UI] Vista corrente: patients."
            )},
            {"role": "user", "content": "Quanto ha di preventivi accettati il signor Rossi e chi è il suo medico di riferimento?"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_pat_01", "type": "function",
                "function": {"name": "getPatientDetail", "arguments": json.dumps({"patientId": PATIENT_ROSSI_ID})}}]},
            {"role": "tool", "tool_call_id": "call_pat_01", "content": json.dumps({
                "id": PATIENT_ROSSI_ID,
                "patientFullName": "Mario Rossi",
                "firstName": "Mario",
                "lastName": "Rossi",
                "phone": "+39 347 1122334",
                "email": "mario.rossi@email.it",
                "ageYears": 41,
                "active": True,
                "primaryProviderId": PROVIDER_BIANCHI_ID,
                "primaryProviderName": "Dott. Luca Bianchi",
                "treatmentPlansCount": 2,
                "openTreatmentItemsCount": 3,
                "acceptedEstimatesAmount": 1450.00,
                "overdueInvoiceCount": 1,
                "fiscalCode": "RSSMRA85M01H501Z",
                "birthDate": "1985-08-01",
                "addressLine1": "Via Giuseppe Garibaldi 12",
                "city": "Torino",
                "province": "TO",
                "postalCode": "10122",
                "billingIban": "IT60X0542811101000000123456",
                "vatNumber": "01234567897",
                "bloodType": "A+",
                "allergyPenicillin": True,
                "takingAnticoagulants": False,
                "anamnesisNotes": "Allergia documentata a beta-lattamici.",
                "notes": "Paziente ansioso, preferisce sedute mattutine.",
            })}]},
    },
    {
        "id": "clinical-anamnesis",
        "category": "DentalCare · Clinica",
        "title": "3 · Anamnesi e Note Cliniche (clinical-copilot vs admin)",
        "description": (
            "Confronta la policy clinica (dentalcare/clinical-copilot) con quella amministrativa: qui i flag clinici "
            "(bloodType, toothNumber, allergyPenicillin, takingAnticoagulants, heartDisease) sono AMMESSI per il medico, "
            "mentre anamnesisNotes, otherAllergies e notes vengono bonificati come TEXT (i nomi di medici/pazienti e telefoni diventano token, "
            "mentre CF e indirizzo nella nota clinica vengono rimossi da Rizzo-PII). I dati fiscali (fiscalCode, billingIban) restano in DROP."
        ),
        "policy": "dentalcare/clinical-copilot",
        "hallucinate": False,
        "sensitive": [
            "Lucia Ferrero", "Giorgio Ferri", "Sara Galli", "FRRLCU78B45L219M", "+39 338 9988776",
            "+39 011 9876543", "Via Po 14, 10123 Torino", "IT60X0542811101000000123456",
            PATIENT_FERRERO_ID, PROVIDER_GALLI_ID,
        ],
        "payload": {"model": "copilot-standard", "tools": [TOOL_GET_PATIENT_DETAIL], "messages": [
            {"role": "system", "content": (
                SYSTEM_CLINICAL + f"\n\n[CONTESTO UI] L'utente sta guardando il paziente Lucia Ferrero (patientId={PATIENT_FERRERO_ID})."
                "\n[CONTESTO UI] Vista corrente: odontogram."
            )},
            {"role": "user", "content": "Quali avvertenze cliniche, allergie e terapie anticoagulanti ha Lucia Ferrero per l'estrazione del dente 26?"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_clin_01", "type": "function",
                "function": {"name": "getPatientDetail", "arguments": json.dumps({"patientId": PATIENT_FERRERO_ID})}}]},
            {"role": "tool", "tool_call_id": "call_clin_01", "content": json.dumps({
                "id": PATIENT_FERRERO_ID,
                "patientFullName": "Lucia Ferrero",
                "phone": "+39 338 9988776",
                "ageYears": 48,
                "primaryProviderId": PROVIDER_GALLI_ID,
                "primaryProviderName": "Dott.ssa Sara Galli",
                "toothNumber": "26",
                "bloodType": "0-",
                "smoker": False,
                "hypertension": True,
                "diabetes": False,
                "heartDisease": True,
                "takingAnticoagulants": True,
                "takingBisphosphonates": False,
                "allergyPenicillin": True,
                "allergyLatex": False,
                "allergyAnesthetic": False,
                "otherAllergies": "Allergia severa a penicilline e cefalosporine segnalata dal Dott. Giorgio Ferri (+39 011 9876543).",
                "anamnesisNotes": (
                    "Paziente Lucia Ferrero (CF FRRLCU78B45L219M, residente in Via Po 14, 10123 Torino) in terapia con Warfarin "
                    "per protesi valvolare; contattare il cardiologo Dott. Giorgio Ferri al +39 011 9876543 per INR pre-operatorio."
                ),
                "notes": "Valutare profilassi con Claritromicina 500mg concordata con Dott.ssa Sara Galli.",
                "fiscalCode": "FRRLCU78B45L219M",
                "birthDate": "1978-02-05",
                "addressLine1": "Via Po 14",
                "city": "Torino",
                "billingIban": "IT60X0542811101000000123456",
            })}]},
    },
    {
        "id": "dashboard-agenda",
        "category": "DentalCare · Segreteria",
        "title": "4 · Briefing Mattutino e Agenda Annidata (getDashboard)",
        "description": (
            "Simula il briefing giornaliero di DentalCare (tool getDashboard): il DTO contiene i contatori di clinica "
            "e l'array annidato todayAppointments (regola NESTED nella policy). Ogni appuntamento nell'array viene filtrato ricorsivamente: "
            "patientFullName e providerFullName diventano PERSONA_n, patientPhone diventa TEL_n, gli UUID sono surrogati e il campo notes viene eliminato (DROP)."
        ),
        "policy": "dentalcare/copilot-admin",
        "hallucinate": False,
        "sensitive": [
            "Chiara Benedetti", "Davide Lunardi", "Dott. Luca Bianchi", "Dott.ssa Sara Galli",
            "+39 349 2233445", "+39 335 6677889",
            PATIENT_BENEDETTI_ID, PATIENT_LUNARDI_ID, PROVIDER_BIANCHI_ID, PROVIDER_GALLI_ID, APPT_1_ID, APPT_2_ID,
        ],
        "payload": {"model": "copilot-standard", "tools": [TOOL_GET_DASHBOARD], "messages": [
            {"role": "system", "content": SYSTEM_ADMIN + "\n[CONTESTO UI] Vista corrente: dashboard."},
            {"role": "user", "content": "Fammi il briefing di oggi: quanti appuntamenti abbiamo in programma e chi sono i pazienti di stamattina?"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_dash_01", "type": "function",
                "function": {"name": "getDashboard", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_dash_01", "content": json.dumps({
                "patientsCount": 412,
                "activeProvidersCount": 5,
                "treatmentPlansInProgress": 28,
                "sentEstimatesCount": 11,
                "todayTotal": 8,
                "todayConfirmed": 7,
                "todayCompleted": 2,
                "todayCancelled": 1,
                "overdueRecallCount": 14,
                "overdueInvoiceCount": 4,
                "todayAppointments": [
                    {
                        "appointmentId": APPT_1_ID,
                        "patientId": PATIENT_BENEDETTI_ID,
                        "patientFullName": "Chiara Benedetti",
                        "patientPhone": "+39 349 2233445",
                        "providerId": PROVIDER_BIANCHI_ID,
                        "providerFullName": "Dott. Luca Bianchi",
                        "chairLabel": "Poltrona 1 · Chirurgia",
                        "startsAt": "2026-10-16T09:30:00",
                        "endsAt": "2026-10-16T10:30:00",
                        "appointmentStatus": "confermato",
                        "serviceName": "Inserimento impianto endosseo",
                        "notes": "Paziente allergica al lattice, preparare guanti in nitrile.",
                    },
                    {
                        "appointmentId": APPT_2_ID,
                        "patientId": PATIENT_LUNARDI_ID,
                        "patientFullName": "Davide Lunardi",
                        "patientPhone": "+39 335 6677889",
                        "providerId": PROVIDER_GALLI_ID,
                        "providerFullName": "Dott.ssa Sara Galli",
                        "chairLabel": "Poltrona 2 · Conservativa",
                        "startsAt": "2026-10-16T11:00:00",
                        "endsAt": "2026-10-16T12:00:00",
                        "appointmentStatus": "in_poltrona",
                        "serviceName": "Devitalizzazione molare (3 canali)",
                        "notes": "Controllo radiografico endorale post-otturazione.",
                    },
                ],
            })}]},
    },
    {
        "id": "recalls-list",
        "category": "DentalCare · Segreteria",
        "title": "5 · Richiami Periodici Scaduti (getRecalls su lista JSON)",
        "description": (
            "Verifica la sanitizzazione di un tool che restituisce direttamente un array JSON di primo livello (List<RecallDto> da getRecalls). "
            "Il gateway ispeziona ogni elemento della lista, assegna token distinti ai pazienti (Elena Bortolotti, Gianni Dal Molin), "
            "tokenizza i numeri di cellulare e rimuove il campo riservato notes da ciascun richiamo."
        ),
        "policy": "dentalcare/copilot-admin",
        "hallucinate": False,
        "sensitive": [
            "Elena Bortolotti", "Gianni Dal Molin", "+39 340 1239876", "+39 339 8765432",
            RECALL_1_ID, RECALL_2_ID, PATIENT_BORTOLOTTI_ID, PATIENT_DAL_MOLIN_ID,
        ],
        "payload": {"model": "copilot-standard", "tools": [TOOL_GET_RECALLS], "messages": [
            {"role": "system", "content": SYSTEM_ADMIN + "\n[CONTESTO UI] Vista corrente: recalls."},
            {"role": "user", "content": "Quali pazienti hanno richiami da contattare con priorità alta? Dammi nome, tipo richiamo e telefono."},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_rec_01", "type": "function",
                "function": {"name": "getRecalls", "arguments": json.dumps({"status": "da_contattare", "priority": "alta"})}}]},
            {"role": "tool", "tool_call_id": "call_rec_01", "content": json.dumps([
                {
                    "recallId": RECALL_1_ID,
                    "patientId": PATIENT_BORTOLOTTI_ID,
                    "patientFullName": "Elena Bortolotti",
                    "patientPhone": "+39 340 1239876",
                    "recallType": "Controllo annuale impianti arcata superiore",
                    "dueDate": "2026-10-01",
                    "status": "da_contattare",
                    "priority": "alta",
                    "contactCount": 2,
                    "notes": "Paziente con pregressa perimplantite sul sito 14, sollecitare con urgenza.",
                },
                {
                    "recallId": RECALL_2_ID,
                    "patientId": PATIENT_DAL_MOLIN_ID,
                    "patientFullName": "Gianni Dal Molin",
                    "patientPhone": "+39 339 8765432",
                    "recallType": "Igiene orale professionale semestrale",
                    "dueDate": "2026-10-05",
                    "status": "da_contattare",
                    "priority": "alta",
                    "contactCount": 1,
                    "notes": "Paziente diabetico in mantenimento parodontale.",
                },
            ])}]},
    },
    {
        "id": "invoices-piva",
        "category": "DentalCare · Amministrazione",
        "title": "6 · Fatturazione, P.IVA, SDI e Indirizzi (spaCy vs Rizzo-PII)",
        "description": (
            "Confronto diretto consigliato in modalità Side-by-Side (Entrambi): nel messaggio utente e nel risultato di getInvoices compaiono "
            "Partita IVA (01234567897), Codice Fiscale, IBAN, indirizzo di fatturazione, documento, targa e dati catastali. "
            "Su llm-gateway (spaCy) la P.IVA viene confusa con un telefono (TEL_n) e indirizzo/documento/catasto/targa restano in chiaro; "
            "su llm-ner-gateway vengono rimossi come [PARTITA IVA RIMOSSA], [INDIRIZZO RIMOSSO], [DOCUMENTO RIMOSSO], [TARGA RIMOSSA] e [DATO CATASTALE RIMOSSO], "
            "oltre al DROP dei campi fiscali dell'InvoiceDto."
        ),
        "policy": "dentalcare/copilot-admin",
        "hallucinate": False,
        "sensitive": [
            "Marco Bianchi", "Elena Conti", "BNCMRC80A01L219K", "01234567897", "Via Giuseppe Garibaldi 12",
            "10122 Torino (TO)", "IT60X0542811101000000123456", "Foglio 45 Particella 123 Sub 7",
            "CA12345AB", "AB123CD", "+39 011 1234567", "marco.bianchi@pec.it", INVOICE_1_ID, PATIENT_BIANCHI_ID,
        ],
        "payload": {"model": "copilot-standard", "tools": [TOOL_GET_INVOICES], "messages": [
            {"role": "system", "content": SYSTEM_ADMIN + "\n[CONTESTO UI] Vista corrente: billing."},
            {"role": "user", "content": (
                "Verifica la fattura scaduta del paziente Marco Bianchi (CF BNCMRC80A01L219K, P.IVA 01234567897) e "
                "della garante Elena Conti, residenti in Via Giuseppe Garibaldi 12, 10122 Torino (TO), con addebito su "
                "IBAN IT60X0542811101000000123456 (studio odontoiatrico accatastato al Foglio 45 Particella 123 Sub 7, "
                "documento CA12345AB, accesso ZTL targa AB123CD, tel +39 011 1234567)."
            )},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_inv_01", "type": "function",
                "function": {"name": "getInvoices", "arguments": json.dumps({"status": "scaduta"})}}]},
            {"role": "tool", "tool_call_id": "call_inv_01", "content": json.dumps([
                {
                    "id": INVOICE_1_ID,
                    "invoiceNumber": "FT-2026/00142",
                    "documentType": "fattura",
                    "invoiceDate": "2026-09-15",
                    "patientId": PATIENT_BIANCHI_ID,
                    "patientFullName": "Marco Bianchi",
                    "status": "scaduta",
                    "taxableAmount": 850.00,
                    "vatAmount": 0.00,
                    "totalAmount": 852.00,
                    "patientFiscalCode": "BNCMRC80A01L219K",
                    "vatNumber": "01234567897",
                    "billingIban": "IT60X0542811101000000123456",
                    "billingPec": "marco.bianchi@pec.it",
                    "billingSdiCode": "M5UXCR1",
                    "billingAddressStreet": "Via Giuseppe Garibaldi 12",
                    "billingAddressZip": "10122",
                    "billingAddressCity": "Torino",
                    "billingAddressProvince": "TO",
                }
            ])}]},
    },
    {
        "id": "odontogram-estimate",
        "category": "DentalCare · Clinica",
        "title": "7 · Preventivo AI da Odontogramma (getEstimates)",
        "description": (
            "Simula l'analisi di un preventivo generato dal rilevamento AI sull'odontogramma (generateEstimateFromOdontogram / getEstimates). "
            "Con la policy clinical-copilot, i numeri dei denti (#16, #24, #36), gli importi e le prestazioni restano visibili al modello, "
            "mentre i nomi della paziente (Chiara Benedetti) e dell'odontoiatra (Dott. Luca Bianchi) dentro estimateTitle e notes vengono tokenizzati."
        ),
        "policy": "dentalcare/clinical-copilot",
        "hallucinate": False,
        "sensitive": [
            "Chiara Benedetti", "Dott. Luca Bianchi", "Luca Bianchi",
            ESTIMATE_1_ID, PATIENT_BENEDETTI_ID, PROVIDER_BIANCHI_ID,
        ],
        "payload": {"model": "copilot-standard", "tools": [TOOL_GET_ESTIMATES], "messages": [
            {"role": "system", "content": SYSTEM_CLINICAL + "\n[CONTESTO UI] Vista corrente: estimates."},
            {"role": "user", "content": "Riassumi il preventivo odontoiatrico in bozza per Chiara Benedetti preparato dal Dott. Luca Bianchi."},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_est_01", "type": "function",
                "function": {"name": "getEstimates", "arguments": json.dumps({"patientId": PATIENT_BENEDETTI_ID, "status": "bozza"})}}]},
            {"role": "tool", "tool_call_id": "call_est_01", "content": json.dumps([
                {
                    "id": ESTIMATE_1_ID,
                    "estimateNumber": "PRV-2026-089",
                    "patientId": PATIENT_BENEDETTI_ID,
                    "patientFullName": "Chiara Benedetti",
                    "createdByProviderId": PROVIDER_BIANCHI_ID,
                    "providerFullName": "Dott. Luca Bianchi",
                    "estimateTitle": "Riabilitazione quadranti 1-2-3 per Chiara Benedetti (denti #16, #24, #36)",
                    "estimateStatus": "bozza",
                    "toothNumber": "16, 24, 36",
                    "lineCount": 3,
                    "subtotalAmount": 1320.00,
                    "discountAmount": 120.00,
                    "totalAmount": 1200.00,
                    "validUntil": "2026-11-30",
                    "notes": (
                        "Piano generato da AI Odontogramma dal Dott. Luca Bianchi per Chiara Benedetti: "
                        "corona in zirconia su #16, ricostruzione composito su #24, endodonzia + intarsio su #36."
                    ),
                }
            ])}]},
    },
    {
        "id": "smart-error-invoice",
        "category": "DentalCare · Errori Intelligenti",
        "title": "8 · Smart Error Sistema TS / SDI (dentalcare/smart-error)",
        "description": (
            "Riproduce esattamente ErrorExplanationService.java di DentalCare (OPERAZIONE / CODICE / MESSAGGIO) con la policy "
            "dentalcare/smart-error. Poiché la spiegazione dell'errore non richiede di re-iniettare i dati anagrafici, la policy applica "
            "REDACT irreversibile ([NOME], [ID], [TELEFONO], [EMAIL]) e DELETE ([CODICE FISCALE RIMOSSO], [PARTITA IVA RIMOSSA], [INDIRIZZO RIMOSSO], [IBAN RIMOSSO])."
        ),
        "policy": "dentalcare/smart-error",
        "hallucinate": False,
        "sensitive": [
            "Franco Cattaneo", PATIENT_CATTANEO_ID, "CTTFNC70H12F205Z", "00743110157",
            "+39 348 1122334", "franco.cattaneo@pec.it", "Corso Vittorio Emanuele II 88, 10121 Torino",
            "IT60X0542811101000000123456",
        ],
        "payload": {"model": "smart-error", "messages": [
            {"role": "system", "content": SYSTEM_SMART_ERROR},
            {"role": "user", "content": (
                "OPERAZIONE: Emissione fattura sanitaria e trasmissione Sistema Tessera Sanitaria (STS)\n"
                "CODICE: STS_BILLING_VALIDATION_FAILED\n"
                f"MESSAGGIO: Invio rifiutato per il paziente Franco Cattaneo (patientId={PATIENT_CATTANEO_ID}, "
                "CF CTTFNC70H12F205Z, P.IVA 00743110157, tel +39 348 1122334, email franco.cattaneo@pec.it, "
                "residente in Corso Vittorio Emanuele II 88, 10121 Torino): coordinata IBAN "
                "IT60X0542811101000000123456 non censita sull'anagrafica contabile dello studio e codice destinatario SDI mancante."
            )}]},
    },
    {
        "id": "app-words-homonyms",
        "category": "DentalCare · Robustezza NER",
        "title": "9 · Voci di Menu DentalCare, MAIUSCOLO e Nomi Ambigui (Rosa, Viola, Speranza)",
        "description": (
            "Mette alla prova il filtro anti-falsi-positivi (APP_WORDS e COMMAND_VERBS), la normalizzazione MAIUSCOLO (_upper_runs) "
            "e il riconoscimento di nomi omografi di Rizzo-PII (DPIA D-04): le sezioni di DentalCare (Agenda, Dashboard, Magazzino, Preventivi) "
            "NON vengono scambiate per persone, mentre «🔍 Cerca Rosa Pellegrini», «Viola Santoro», «Speranza Gallo» e il nome tutto maiuscolo "
            "«CONTI LUCIA» vengono riconosciuti al 100% senza inglobare il verbo «Cerca»."
        ),
        "policy": "dentalcare/copilot-admin",
        "hallucinate": False,
        "sensitive": [
            "Rosa Pellegrini", "Viola Santoro", "Speranza Gallo", "CONTI LUCIA", "+39 340 5566778",
        ],
        "payload": {"model": "copilot-standard", "messages": [
            {"role": "system", "content": SYSTEM_ADMIN + "\n[CONTESTO UI] Vista corrente: agenda."},
            {"role": "user", "content": (
                "🔍 Cerca Rosa Pellegrini e chiama Viola Santoro al +39 340 5566778. "
                "Apri l'Agenda di Martedì, mostra la Dashboard, vai in Magazzino per le scorte basse "
                "e apri la scheda di Speranza Gallo e di CONTI LUCIA."
            )}]},
    },
    {
        "id": "unknown-token",
        "category": "DentalCare · Sicurezza",
        "title": "10 · Sicurezza Fail-Closed su Tool Call (502 UNKNOWN_TOKEN)",
        "description": (
            "Dimostra la protezione fail-closed di ResponseRestorer: il mock LLM riceve la richiesta pseudonimizzata ma tenta di invocare "
            "il tool getPatientDetail passando un token inventato/allucinato (PERSONA_99) che non esiste nel TokenVault della conversazione. "
            "Il gateway blocca immediatamente la risposta con HTTP 502 UNKNOWN_TOKEN."
        ),
        "policy": "dentalcare/copilot-admin",
        "hallucinate": True,
        "sensitive": ["Mario Rossi"],
        "payload": {"model": "copilot-standard", "tools": [TOOL_GET_PATIENT_DETAIL], "messages": [
            {"role": "system", "content": SYSTEM_ADMIN},
            {"role": "user", "content": "Mostrami la scheda dettagliata del paziente Mario Rossi"}]},
    },
    {
        "id": "legal-case",
        "category": "Giurisprudenza",
        "title": "11 · Pratica Legale / Perizia Odontoiatrica (legal/case-analysis)",
        "description": (
            "Scenario per lo studio legale / consulenza medico-legale con la policy dedicata legal/case-analysis "
            "(configurata su llm-ner-gateway): tokenizza i nomi delle parti e dei legali e rimuove P.IVA, Codice Fiscale e indirizzi."
        ),
        "policy": "legal/case-analysis",
        "hallucinate": False,
        "sensitive": [
            "Luigi Neri", "Anna Ferrari", "FRRNNA75C41F205X", "01234567897",
            "Via Roma 5, 20121 Milano (MI)", "anna.ferrari@studio.it", "+39 347 765 4321",
        ],
        "payload": {"model": "copilot-standard", "messages": [
            {"role": "system", "content": "Sei un assistente giuridico: analizza la pratica medico-legale senza rivelare dati personali."},
            {"role": "user", "content": (
                "Analizza la perizia dell'avvocato Luigi Neri per conto di Anna Ferrari (CF FRRNNA75C41F205X), titolare "
                "dello studio con P.IVA 01234567897, con sede in Via Roma 5, 20121 Milano (MI). "
                "Contatti di riferimento: anna.ferrari@studio.it, +39 347 765 4321."
            )}]},
    },
]


@app.get("/api/config")
def config() -> dict:
    return {
        "gateways": [{"id": k, "label": v["label"], "shortLabel": v["shortLabel"]} for k, v in GATEWAYS.items()],
        "policies": POLICIES,
        "scenarios": SCENARIOS,
        "configFile": str(CONFIG_PATH) if CONFIG_PATH else None,
        "hasOpenAiKey": bool(OPENAI_API_KEY),
        "openAiKeyMasked": (OPENAI_API_KEY[:7] + "..." + OPENAI_API_KEY[-4:]) if len(OPENAI_API_KEY) > 12 else ("Configurata" if OPENAI_API_KEY else ""),
    }


@app.get("/api/health")
async def health() -> dict:
    async def check(client: httpx.AsyncClient, name: str, url: str) -> dict:
        try:
            response = await client.get(url, timeout=1.5)
            body = response.json()
            up = response.status_code == 200 and body.get("status") in ("UP", "ok")
        except Exception:
            up = False
        return {"name": name, "up": up}

    checks = [(name, url) for gateway in GATEWAYS.values() for name, url in gateway["checks"]]
    async with httpx.AsyncClient() as client:
        return {"services": await asyncio.gather(*(check(client, n, u) for n, u in checks))}


# ---------------------------------------------------------------- esecuzione e metriche

def _walk_keys(node, prefix="") -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(prefix + key)
            keys |= _walk_keys(value, prefix + key + ".")
    elif isinstance(node, list):
        for item in node:
            keys |= _walk_keys(item, prefix)
    return keys


def _tool_json(messages: list) -> list:
    found = []
    for message in messages or []:
        if message.get("role") == "tool":
            try:
                found.append(json.loads(message.get("content") or ""))
            except (TypeError, ValueError):
                pass
    return found


def dropped_fields(client_payload: dict, upstream: dict | None) -> list[str]:
    """Campi dei risultati degli strumenti presenti nel client e assenti verso il fornitore (eliminati dal gateway)."""
    if not upstream:
        return []
    before = set().union(*[_walk_keys(t) for t in _tool_json(client_payload.get("messages"))] or [set()])
    after = set().union(*[_walk_keys(t) for t in _tool_json(upstream.get("messages"))] or [set()])
    return sorted(before - after)


def extract_complete_response(status: int, response: dict | str) -> dict:
    """Estrae il testo completo o la tool call dalla risposta finale restituita al client."""
    if isinstance(response, dict):
        if status >= 200 and status < 300 and response.get("choices"):
            msg = (response["choices"][0] or {}).get("message") or {}
            if msg.get("content"):
                return {"kind": "assistant", "title": "Risposta completa del Copilot (ripristinata al Client)", "text": msg["content"]}
            if msg.get("tool_calls"):
                calls = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function") or {}
                    calls.append(f"{fn.get('name')}({fn.get('arguments')})")
                return {"kind": "tool_call", "title": "Chiamata a strumento (Tool Call ripristinata al Client)", "text": "🔧 " + " ; ".join(calls)}
        err = response.get("error") if isinstance(response.get("error"), dict) else {}
        code = err.get("code") or f"HTTP_{status}"
        msg_text = err.get("message") or json.dumps(response, ensure_ascii=False)
        if status == 502 and code == "UNKNOWN_TOKEN":
            return {"kind": "security_block", "title": "Blocco di sicurezza Fail-Closed (502 UNKNOWN_TOKEN)",
                    "text": f"🛡️ Richiesta bloccata dal gateway ({code}): {msg_text}"}
        return {"kind": "error", "title": f"Errore Gateway (HTTP {status} {code})", "text": f"❌ {code}: {msg_text}"}
    return {"kind": "raw", "title": f"Risposta (HTTP {status})", "text": str(response)}


def evaluate_gateway_run(result: dict, sensitive: list[str], hallucinate: bool) -> dict:
    """Calcola percentuali di protezione PII, qualità risposta completa, anomalie NER e note esplicative."""
    status = result["status"]
    client_text = json.dumps(result["clientRequest"], ensure_ascii=False)
    upstream = result["upstreamRequest"]
    upstream_text = json.dumps(upstream, ensure_ascii=False) if upstream else ""
    full_resp = extract_complete_response(status, result["response"])
    resp_text = full_resp["text"]

    # 1. Copertura dati sensibili (quanti elementi di sensitive presenti nel client NON sono trapelati in upstream)
    active_sensitive = [s for s in dict.fromkeys(sensitive or []) if s and s in client_text]
    leaked: list[str] = []
    if upstream:
        for item in active_sensitive:
            if item in upstream_text:
                leaked.append(item)

    # 2. Rilevamento anomalie semantiche NER (verbi inglobati, P.IVA scambiata per telefono, falsi positivi menu)
    anomalies: list[str] = []
    strengths: list[str] = []

    if upstream:
        # Verbi imperativi a inizio comando inglobati nel token PERSONA_n
        for verb in ("Chiama", "Cerca"):
            if re.search(rf"\b{verb}\s+[A-Z]", client_text) and not re.search(rf"\b{verb}\s+(?:PERSONA_\d+|\[NOME\])", upstream_text):
                anomalies.append(f"Verbo «{verb}» inglobato per errore dentro il token PERSONA")
            elif re.search(rf"\b{verb}\s+(?:PERSONA_\d+|\[NOME\])", upstream_text):
                strengths.append(f"Verbo «{verb}» scorporato correttamente dal nome proprio")

        # Partita IVA scambiata per TEL_n o lasciata in chiaro
        for piva in ("01234567897", "00743110157"):
            if piva in client_text:
                if "P.IVA [PARTITA IVA RIMOSSA]" in upstream_text:
                    strengths.append(f"P.IVA {piva} validata col checksum Luhn e rimossa ([PARTITA IVA RIMOSSA])")
                elif re.search(r"P\.IVA\s+TEL_\d+", upstream_text):
                    anomalies.append(f"P.IVA {piva} confusa con un numero di telefono (TEL_n)")

        # Voci di menu DentalCare preservate
        for menu_word in ("Agenda", "Dashboard", "Magazzino"):
            if menu_word in client_text and menu_word not in upstream_text:
                anomalies.append(f"Voce di menu «{menu_word}» scambiata per falso positivo PERSONA")

        # Categorie estese Rizzo-PII (indirizzi, catasto, documenti, targhe)
        for placeholder, label in (
            ("[INDIRIZZO RIMOSSO]", "Indirizzo postale"),
            ("[DATO CATASTALE RIMOSSO]", "Dato catastale"),
            ("[DOCUMENTO RIMOSSO]", "Numero documento"),
            ("[TARGA RIMOSSA]", "Targa veicolo"),
        ):
            if placeholder in upstream_text:
                strengths.append(f"{label} riconosciuto e rimosso ({placeholder})")

        if result.get("droppedFields"):
            strengths.append(f"Filtro DTO strutturato attivo: {len(result['droppedFields'])} campi sensibili eliminati (DROP)")

    # Calcolo percentuale Protezione & Precisione PII (privacyPct)
    if status == 0 or (not hallucinate and status >= 400):
        privacy_pct = 0
        anomalies.append(f"Chiamata fallita con HTTP {status} (policy non supportata o servizio non disponibile)")
    else:
        total_sens = max(1, len(active_sensitive))
        protected_count = max(0, total_sens - len(leaked))
        base_privacy = (protected_count / total_sens) * 100.0
        penalty = len(anomalies) * 8.0
        privacy_pct = max(0, min(100, round(base_privacy - penalty)))
        if not leaked and not anomalies:
            strengths.append(f"100% dei dati sensibili ({total_sens}/{total_sens}) protetti senza falsi positivi")
        elif leaked:
            anomalies.insert(0, f"{len(leaked)}/{total_sens} dati sensibili lasciati in chiaro verso l'LLM: {', '.join(leaked[:5])}")

    # Calcolo percentuale Completezza & Qualità Risposta Finale (responsePct)
    if hallucinate:
        if status == 502:
            response_pct = 100
            strengths.append("Blocco Fail-Closed (502 UNKNOWN_TOKEN) attivato correttamente sul token allucinato")
        else:
            response_pct = 0
            anomalies.append("Il gateway non ha bloccato il token allucinato")
    elif status < 200 or status >= 300:
        response_pct = 0
    else:
        unrestored = UNRESTORED_RE.findall(resp_text)
        resp_penalty = len(unrestored) * 25 + len(anomalies) * 10 + len(leaked) * 5
        response_pct = max(0, min(100, round(100 - resp_penalty)))
        if not unrestored and not anomalies and not leaked:
            strengths.append("Risposta completa e perfettamente ripristinata al Client senza artefatti")

    return {
        "fullResponse": full_resp,
        "privacyPct": privacy_pct,
        "responsePct": response_pct,
        "protectedCount": max(0, len(active_sensitive) - len(leaked)),
        "totalSensitive": len(active_sensitive),
        "leakedItems": leaked,
        "anomalies": anomalies,
        "strengths": strengths,
    }


def enrich_comparison(results: list[dict], sensitive: list[str], hallucinate: bool) -> dict | None:
    """Calcola l'efficienza di velocità relativa, il punteggio globale per ogni gateway e il verdetto side-by-side."""
    valid_latencies = [r["latencyMs"] for r in results if r["status"] > 0 and r["latencyMs"] > 0]
    min_lat = min(valid_latencies) if valid_latencies else 1

    for r in results:
        eval_data = evaluate_gateway_run(r, sensitive, hallucinate)
        lat = max(1, r["latencyMs"])
        if r["status"] == 0 or (not hallucinate and r["status"] >= 400):
            speed_pct = 0
        else:
            ratio = min_lat / lat
            speed_pct = max(40, min(100, round((ratio ** 0.5) * 100)))
        overall_pct = round(eval_data["privacyPct"] * 0.65 + eval_data["responsePct"] * 0.25 + speed_pct * 0.10)
        eval_data["speedPct"] = speed_pct
        eval_data["overallPct"] = overall_pct
        r["metrics"] = eval_data
        r["shortLabel"] = GATEWAYS.get(r["gateway"], {}).get("shortLabel", r["label"])

    if len(results) < 2:
        return None

    g_std, g_rizzo = results[0], results[1]
    m_std, m_rizzo = g_std["metrics"], g_rizzo["metrics"]

    fastest = g_std if g_std["latencyMs"] <= g_rizzo["latencyMs"] else g_rizzo
    slowest = g_rizzo if fastest is g_std else g_std
    speed_gain_pct = round(((slowest["latencyMs"] - fastest["latencyMs"]) / max(1, slowest["latencyMs"])) * 100)

    if m_rizzo["overallPct"] > m_std["overallPct"]:
        winner = g_rizzo["shortLabel"]
        verdict = (
            f"🏆 {g_rizzo['shortLabel']} è migliore in questo scenario con il {m_rizzo['overallPct']}% di efficienza globale "
            f"(contro il {m_std['overallPct']}% di {g_std['shortLabel']}): garantisce una protezione PII del "
            f"{m_rizzo['privacyPct']}% vs {m_std['privacyPct']}% e una qualità di risposta del "
            f"{m_rizzo['responsePct']}% vs {m_std['responsePct']}%, mentre {fastest['shortLabel']} è più rapido del {speed_gain_pct}% "
            f"({fastest['latencyMs']} ms vs {slowest['latencyMs']} ms)."
        )
    elif m_std["overallPct"] > m_rizzo["overallPct"]:
        winner = g_std["shortLabel"]
        verdict = (
            f"⚡ {g_std['shortLabel']} prevale in questo scenario con il {m_std['overallPct']}% (contro il {m_rizzo['overallPct']}% di {g_rizzo['shortLabel']}): "
            f"a parità di protezione PII ({m_std['privacyPct']}% vs {m_rizzo['privacyPct']}%), risponde il {speed_gain_pct}% più velocemente "
            f"({g_std['latencyMs']} ms vs {g_rizzo['latencyMs']} ms)."
        )
    else:
        winner = "Parità complessiva"
        verdict = (
            f"⚖️ Parità di punteggio globale ({m_rizzo['overallPct']}%): {g_rizzo['shortLabel']} primeggia in accuratezza semantica "
            f"({m_rizzo['privacyPct']}%), mentre {g_std['shortLabel']} è più rapido di {slowest['latencyMs'] - fastest['latencyMs']} ms."
        )

    return {
        "winner": winner,
        "verdict": verdict,
        "speedFastest": fastest["shortLabel"],
        "speedGainPct": speed_gain_pct,
        "latencyDiffMs": abs(g_rizzo["latencyMs"] - g_std["latencyMs"]),
    }


async def call_gateway(gateway_id: str, body: dict, request_data: dict) -> dict:
    gateway = GATEWAYS[gateway_id]
    policy = next((p for p in POLICIES if p["id"] == request_data["policy"]), POLICIES[0])
    payload = copy.deepcopy(body)
    payload["model"] = policy["profile"]
    token = sign_jwt({"iss": request_data.get("issuer"), "application": policy["application"],
                      "purpose": policy["purpose"], "tenantRef": request_data.get("tenantRef"),
                      "actorRef": request_data.get("actorRef"), "conversationRef": request_data["conversationRef"]})
    api_key = request_data.get("openaiKey") or OPENAI_API_KEY
    _state.update(captured=None, mode=request_data.get("providerMode", "mock"),
                  hallucinate=bool(request_data.get("hallucinate")), api_key=api_key)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(gateway["url"] + "/v1/chat/completions", json=payload,
                                         headers={"Authorization": f"Bearer {token}", "X-Request-Id": uuid.uuid4().hex[:16]})
        status, text = response.status_code, response.text
        try:
            answer = response.json()
        except ValueError:
            answer = text
    except httpx.HTTPError as error:
        status, answer = 0, {"error": {"code": "GATEWAY_UNREACHABLE", "message": f"{type(error).__name__}: gateway non raggiungibile"}}
    elapsed = round((time.perf_counter() - started) * 1000)
    upstream = _state["captured"]
    return {"gateway": gateway_id, "label": gateway["label"], "status": status, "latencyMs": elapsed,
            "clientRequest": payload, "upstreamRequest": upstream, "response": answer,
            "droppedFields": dropped_fields(payload, upstream)}


@app.post("/api/run")
async def run(request: Request) -> dict:
    data = await request.json()
    targets = ["llm-gateway", "llm-ner-gateway"] if data.get("gateway") == "both" else [data["gateway"]]
    sensitive = data.get("sensitive") or []
    hallucinate = bool(data.get("hallucinate"))
    async with _run_lock:
        results = [await call_gateway(target, data["payload"], data) for target in targets]
    comparison = enrich_comparison(results, sensitive, hallucinate)
    return {"results": results, "comparison": comparison}


# ---------------------------------------------------------------- finto OpenAI

def _unique(pattern: re.Pattern, text: str) -> list[str]:
    return list(dict.fromkeys(pattern.findall(text)))


def smart_reply(upstream: dict, hallucinate: bool) -> dict:
    """Risposta realistica in stile DentalCare che riusa i codici ricevuti per mostrare il ripristino del gateway."""
    messages = upstream.get("messages") or []
    text = json.dumps(messages, ensure_ascii=False)
    people, phones, emails = (_unique(ALIAS_RE[k], text) for k in ("PERSONA", "TEL", "EMAIL"))
    uuids = _unique(UUID_RE, text)
    tools = upstream.get("tools") or []
    last_role = messages[-1].get("role") if messages else None
    message: dict = {"role": "assistant", "content": None}
    finish = "stop"

    if tools and (hallucinate or last_role == "user"):
        function = tools[0]["function"]
        parameter = next(iter((function.get("parameters") or {}).get("properties") or {}), "patientId")
        if hallucinate:
            value = "PERSONA_99"
        else:
            value = uuids[0] if uuids else (people[0] if people else "")
        message["tool_calls"] = [{"id": "call_demo_" + uuid.uuid4().hex[:8], "type": "function",
                                  "function": {"name": function["name"], "arguments": json.dumps({parameter: value})}}]
        finish = "tool_calls"
    else:
        parts = []
        last_user = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), "") or ""
        if "OPERAZIONE:" in str(last_user) and "CODICE:" in str(last_user):
            parts.append(
                "Spiegazione errore DentalCare: l'invio al Sistema Tessera Sanitaria / SDI è stato rifiutato perché "
                "mancano il codice destinatario SDI o una coordinata bancaria valida nell'anagrafica dello studio. "
                "Apri la sezione Fatturazione → Impostazioni Studio, verifica i dati contabili e ripeti l'invio."
            )
        else:
            if people:
                parts.append("Riepilogo per " + ", ".join(people) + ".")
            for tool_result in _tool_json(messages):
                if isinstance(tool_result, dict):
                    if "acceptedEstimatesAmount" in tool_result:
                        parts.append(
                            f"Preventivi accettati: {tool_result['acceptedEstimatesAmount']} € "
                            f"({tool_result.get('treatmentPlansCount', 0)} piani di cura attivi, "
                            f"medico referente: {tool_result.get('primaryProviderName', 'N/D')})."
                        )
                    if "bloodType" in tool_result or "takingAnticoagulants" in tool_result:
                        parts.append(
                            f"Quadro clinico (dente #{tool_result.get('toothNumber', 'N/D')}, gruppo {tool_result.get('bloodType', 'N/D')}): "
                            f"allergia penicillina={tool_result.get('allergyPenicillin')}, "
                            f"anticoagulanti={tool_result.get('takingAnticoagulants')}. "
                            f"Note: {tool_result.get('anamnesisNotes', '')} {tool_result.get('notes', '')}"
                        )
                    if "todayAppointments" in tool_result:
                        appts = tool_result.get("todayAppointments") or []
                        summary = "; ".join(
                            f"{a.get('startsAt', '')[11:16]} {a.get('patientFullName')} ({a.get('patientPhone')}) "
                            f"con {a.get('providerFullName')} in {a.get('chairLabel')} [{a.get('serviceName')}]"
                            for a in appts
                        )
                        parts.append(
                            f"Oggi ci sono {tool_result.get('todayTotal', len(appts))} appuntamenti "
                            f"({tool_result.get('todayConfirmed', 0)} confermati): {summary}."
                        )
                    elif "totalAmount" in tool_result:
                        parts.append(f"Totale documento: {tool_result['totalAmount']} €.")
                elif isinstance(tool_result, list):
                    for item in tool_result:
                        if not isinstance(item, dict):
                            continue
                        if "recallType" in item:
                            parts.append(
                                f"Richiamo [{item.get('priority')}]: {item.get('patientFullName')} "
                                f"({item.get('patientPhone')}) — {item.get('recallType')} (scadenza {item.get('dueDate')})."
                            )
                        elif "invoiceNumber" in item:
                            parts.append(
                                f"Fattura {item.get('invoiceNumber')} per {item.get('patientFullName')}: "
                                f"{item.get('totalAmount')} € (stato: {item.get('status')})."
                            )
                        elif "estimateNumber" in item:
                            parts.append(
                                f"Preventivo {item.get('estimateNumber')} ({item.get('estimateTitle')}): "
                                f"totale {item.get('totalAmount')} € — {item.get('notes', '')}"
                            )
            if phones:
                parts.append("Contatti telefonici: " + ", ".join(phones) + ".")
            if emails:
                parts.append("E-mail: " + ", ".join(emails) + ".")
            if uuids:
                parts.append("Riferimento ID: " + uuids[0] + ".")
        parts.append("Richiesta elaborata: «" + str(last_user)[:360] + "»")
        message["content"] = " ".join(parts)
    return {"id": "chatcmpl-demo-" + uuid.uuid4().hex[:10], "object": "chat.completion", "created": int(time.time()),
            "model": upstream.get("model", "gpt-4o"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": max(1, len(text) // 4), "completion_tokens": 55,
                      "total_tokens": max(1, len(text) // 4) + 55}}


@app.post("/mock-openai/v1/chat/completions")
async def mock_openai(request: Request):
    upstream = await request.json()
    _state["captured"] = upstream
    if _state["mode"] == "openai":
        if not _state["api_key"]:
            return JSONResponse(status_code=401, content={"error": {"message": "Chiave OpenAI mancante nella demo"}})
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(OPENAI_URL, json=upstream,
                                         headers={"Authorization": f"Bearer {_state['api_key']}"})
        return JSONResponse(status_code=response.status_code, content=response.json())
    return smart_reply(upstream, _state["hallucinate"])


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT, log_level="info")
