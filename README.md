# `llm-gateway-demo` — Suite di Collaudo e Ispettore Side-by-Side

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Web UI](https://img.shields.io/badge/UI-HTML5%20%2F%20CSS3%20Responsive-orange.svg)](#funzionalità-della-ui)
[![Port](https://img.shields.io/badge/Port-8090-blueviolet.svg)](#architettura-e-porte)

Applicazione web standalone (porta **`8090`**) progettata per collaudare, ispezionare visivamente a 3 pannelli e confrontare in tempo reale (Side-by-Side):
1. **`llm-ner-gateway`** (repo: [`fpapale/llm-ner-gateway`](https://github.com/fpapale/llm-ner-gateway)): Gateway primario con motore Dual-Engine specializzato **`llm-ner-dentalcare-0.3B`** (F1 0.985, immunità falsi positivi sui menu DentalCare), validazione checksum Partita IVA Luhn, categorie estese (`ADDRESS`, `CATASTO`, `DOCID`, `TARGA`) e modalità Pass-Through.
2. **`llm-gateway`** (baseline di riferimento, solo lettura): Gateway con motore standard **`spaCy` (`it_core_news_lg`)**.

> [!NOTE]
> **Vincolo di Sola Lettura su `D:\llm-gateway`**:
> La cartella `D:\llm-gateway` non viene mai alterata. Lo script `start-all.ps1` crea una copia isolata in `.runtime\llm-gateway` ed esegue i servizi configurando le proprietà e la chiave pubblica crittografica EC `ES256` da riga di comando.

---

## 1. Avvio Rapido Locale (Windows — 1 Solo Comando)

Dalla cartella radice `D:\llm-gateway-demo` in PowerShell:

```powershell
.\start-all.ps1
```

Lo script orchestra automaticamente l'intero stack di test:
1. Crea l'ambiente virtuale Python `.venv` della demo e genera/verifica la coppia di chiavi EC P-256 (`ES256`) in `keys/client-private.pem` e `keys/client-public.pem`.
2. Compila e avvia `llm-gateway-ner` (`:8097`) e `llm-gateway` (`:8095`).
3. Compila e avvia `llm-ner-gateway-ner` (`:8098`) e `llm-ner-gateway` (`:8096`).
4. Avvia l'interfaccia web della demo su **`http://localhost:8090`** e apre automaticamente il browser.

Per arrestare tutti i servizi avviati:
```powershell
.\stop-all.ps1
```

---

## 2. Funzionalità della Web UI (`http://localhost:8090`)

### 2.1 Health Check Dinamico con Badge di Stato (Polling ogni 4s)
Interroga costantemente lo stato di salute dei gateway e dei rispettivi microservizi NER:
- 🟢 Pallino verde + badge verde **`[NER ATTIVO]`**: Gateway operativo con anonimizzazione e sanitizzazione attive.
- 🟠 Pallino verde + badge arancione **`[PASS-THROUGH]`**: Gateway operativo in modalità trasparente (richieste inoltrate all'LLM senza sanitizzazione).
- 🔴 Pallino rosso: Servizio non raggiungibile o offline.
- Aggiornamento in tempo reale dei titoli dei gateway nel menu a tendina di selezione.

### 2.2 Ispezione a 3 Pannelli
Visualizzazione chiara del flusso end-to-end con evidenziazione sintattica colorata:
1. **Pannello 1 (Client $\rightarrow$ Gateway)**: Payload originario dell'applicazione con evidenziazione in giallo dei dati personali identificativi (PII) rilevati.
2. **Pannello 2 (Gateway $\rightarrow$ Fornitore LLM)**: Payload effettivamente intercettato prima dell'inoltro a monte, con token pseudonimi (`PERSONA_1`, `TEL_1`), segnaposto irreversibili (`[CATASTO]`), UUID surrogati e lista dei campi DTO rimossi con `DROP`.
3. **Pannello 3 (Gateway $\rightarrow$ Client)**: Risposta finale restituita all'applicazione con i dati personali correttamente ripristinati e misurazione della latenza in millisecondi.

### 2.3 Modalità Fornitore Flessibile
- **Mock Intelligente Offline (Zero Costi)**:
  - Replica il comportamento di OpenAI senza consumare token API né inviare dati a terzi.
  - Risponde riutilizzando i token pseudonimi ricevuti per verificare il ripristino automatico.
  - **Toggle Allucinazione Token**: inietta deliberatamente un token sconosciuto (`PERSONA_99`) per verificare sul campo la protezione fail-closed **`502 UNKNOWN_TOKEN`**.
- **OpenAI Reale**:
  - Inoltra la richiesta pseudonimizzata all'endpoint ufficiale OpenAI.
  - Supporta sia la chiave preconfigurata a livello server (`config_ner_demo/config.yaml`), sia l'inserimento dinamico a runtime dall'interfaccia utente.

### 2.4 Confronto Side-by-Side (Dual Execution)
Consente di inviare simultaneamente lo stesso messaggio a **Gateway 1 (`llm-ner-gateway`)** e **Gateway 2 (`llm-gateway`)**, mettendo a confronto diretto:
- Capacità di isolare i verbi imperativi (*"Chiama"* vs *"Mario Rossi"*).
- Gestione dei menu applicativi DentalCare (zero falsi positivi nel modello 0.3B vs falsi allarmi in spaCy).
- Checksum Luhn su Partita IVA (validazione corretta vs riconoscimento come generico numero).

---

## 3. Gli 11 Scenari Preconfigurati di DentalCare

I test predefiniti simulano i flussi e le strutture DTO reali del repository `dentalcare`:

| # | Scenario | Policy Attiva | Obiettivo del Test |
|---|:---|:---|:---|
| **1** | **Comando Breve e Verbi di Contatto** | `dentalcare / copilot-admin` | Dimostra come il modello 0.3B isoli il nome proprio senza inglobare il verbo imperativo (*"Chiama"*). |
| **2** | **Contesto UI + `PatientDetailDto` (2 Passate)** | `dentalcare / copilot-admin` | Verifica la risoluzione a 2 passate del contesto UI (`[CONTESTO UI]`), surrogato UUID e il `DROP` di 13 campi clinici/fiscali. |
| **3** | **Anamnesi e Note Cliniche** | `dentalcare / clinical-copilot` | Verifica che i flag clinici (`bloodType`, `toothNumber`, `allergyPenicillin`) passino in chiaro (`ALLOW`), mentre le note libere vengono bonificate (`TEXT`) e i campi fiscali eliminati (`DROP`). |
| **4** | **Briefing Mattutino e Agenda (`getDashboard`)** | `dentalcare / copilot-admin` | Verifica la sanitizzazione ricorsiva dell'array `todayAppointments` (`NESTED`) e il `DROP` di note riservate. |
| **5** | **Richiami Periodici Scaduti (`getRecalls`)** | `dentalcare / copilot-admin` | Verifica la sanitizzazione di liste `List<RecallDto>` con pazienti multipli. |
| **6** | **Fatturazione, P.IVA, SDI e Indirizzi (`getInvoices`)** | `dentalcare / copilot-admin` | Confronto su Partita IVA (checksum Luhn 11 cifre), CF, IBAN e categorie estese (`ADDRESS`, `CATASTO`, `DOCID`, `TARGA`). |
| **7** | **Preventivo AI da Odontogramma (`getEstimates`)** | `dentalcare / clinical-copilot` | Conservazione dei numeri dentali (#16, #24, #36) e importi economici con pseudonimizzazione di paziente e medico. |
| **8** | **Smart Error Sistema TS / SDI (#54)** | `dentalcare / smart-error` | Verifica del formato `OPERAZIONE / CODICE / MESSAGGIO` con redazione irreversibile (`[NOME]`, `[ID]`, `[TELEFONO]`, `[EMAIL]`). |
| **9** | **Voci di Menu DentalCare e Nomi Ambigui** | `dentalcare / copilot-admin` | Verifica dell'assenza totale di falsi positivi sulle sezioni applicative (*Agenda*, *Fatturazione*, *Magazzino*) e riconoscimento di nomi omografi. |
| **10**| **Sicurezza Fail-Closed su Tool Call** | `dentalcare / copilot-admin` | Blocca all'istante con errore `502 UNKNOWN_TOKEN` il tentativo del modello di invocare `getPatientDetail` con un token allucinato (`PERSONA_99`). |
| **11**| **Pratica Legale / Perizia Odontoiatrica** | `legal / case-analysis` | Scenario giuridico completo con policy estesa. |

---

## 4. Architettura e Assegnazione Porte

### 4.1 Ambiente di Produzione / Staging Docker (Server `192.168.0.72`)

Operativo sulla rete Docker condivisa `dentalcarepro_dentalcarepro`:

| Servizio | Porta Esposta | Ruolo |
| :--- | :---: | :--- |
| **`llm-gateway-demo`** | **`8090`** | Web UI, Simulatore Client JWT `ES256`, Mock/Inspector OpenAI |
| **`llm-ner-gateway`** | **`8080`** | Gateway primario con modello odontoiatrico 0.3B |
| **`llm-ner-gateway-ner`**| — | Microservizio Dual-Engine (rete interna porta 8000) |
| **`llm-gateway`** | **`8085`** | Gateway secondario di confronto con spaCy |
| **`llm-gateway-ner`** | — | Microservizio spaCy (rete interna porta 8000) |

### 4.2 Ambiente di Sviluppo Locale (Windows)

| Servizio | Porta HTTP | Ruolo |
| :--- | :---: | :--- |
| **`llm-gateway-demo`** | **`8090`** | Interfaccia Web e Client Simulator |
| **`llm-ner-gateway`** | **`8096`** | Gateway Java (0.3B Dual-Engine) |
| **`llm-ner-gateway-ner`**| **`8098`** | Microservizio Python (0.3B / spaCy) |
| **`llm-gateway`** | **`8095`** | Gateway Java originale (copia `.runtime` sola lettura) |
| **`llm-gateway-ner`** | **`8097`** | Microservizio Python originale (spaCy) |

---

## 5. Configurazione: `./config_ner_demo/config.yaml`

Tutti i parametri di runtime sono centralizzati nel file `config_ner_demo/config.yaml`:

```yaml
server:
  host: "0.0.0.0"
  port: 8090

keys:
  dir: "./keys"
  private_key: "./keys/client-private.pem"
  public_key: "./keys/client-public.pem"

gateways:
  llm_ner_gateway:
    url: "http://127.0.0.1:8096"
    ner_url: "http://127.0.0.1:8098"
    label: "llm-ner-gateway (Dual-Engine / 0.3B — :8096)"
    short_label: "llm-ner-gateway"

  llm_gateway:
    url: "http://127.0.0.1:8095"
    ner_url: "http://127.0.0.1:8097"
    label: "llm-gateway (Standard spaCy — :8095)"
    short_label: "llm-gateway (spaCy)"

openai:
  url: "https://api.openai.com/v1/chat/completions"
```

---

## 6. Esecuzione con Docker

È possibile eseguire `llm-gateway-demo` in un container Docker isolato montando le configurazioni e le chiavi crittografiche:

```bash
# Avvio con Docker Compose
docker compose up -d --build
```

Il file `docker-compose.yml` monta `./config_ner_demo` in sola lettura (`/app/config_ner_demo:ro`) e persiste la cartella `./keys`: qualsiasi modifica a `config.yaml` viene caricata all'avvio senza ricompilare l'immagine.
