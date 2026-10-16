# `llm-gateway-demo` — Ambiente di Test e Confronto Interattivo

Applicazione web standalone (porta **`8090`**) progettata per testare, confrontare e ispezionare visivamente il comportamento di:
1. **`llm-gateway`** (`D:\llm-gateway`, porta `8095`) con motore NER `spaCy` (`it_core_news_lg`, porta `8097`).
2. **`llm-ner-gateway`** (`D:\llm-rizzo-gateway`, porta `8096`) con motore NER [`rizzoaiacademy/rizzo-pii-0.3B`](https://huggingface.co/rizzoaiacademy/rizzo-pii-0.3B) (ModernBERT/mmBERT a 22 categorie PII italiane, porta `8098`) e validazione checksum Partita IVA.

> **Vincolo di Sola Lettura**: `D:\llm-gateway` non viene mai modificato. Lo script `start-all.ps1` compila una copia isolata in `.runtime\llm-gateway` ed esegue i servizi passando le proprietà e la chiave pubblica EC `ES256` da riga di comando.

---

## Avvio Rapido (1 Comando)

Da PowerShell nella cartella `D:\llm-gateway-demo`:

```powershell
.\start-all.ps1
```

Lo script:
1. Crea l'ambiente virtuale Python `.venv` della demo e genera la coppia di chiavi EC `P-256` (`ES256`) in `keys\client-private.pem` e `keys\client-public.pem`.
2. Compila e avvia `llm-gateway-ner` (`:8097`) e `llm-gateway` (`:8095`).
3. Compila e avvia `llm-ner-gateway-ner` (`:8098`) e `llm-ner-gateway` (`:8096`).
4. Avvia la demo web su **`http://localhost:8090`** e apre il browser.

Per fermare tutti i servizi avviati:

```powershell
.\stop-all.ps1
```

---

## Configurazione con Cartella `./config_ner_demo`

Tutti i parametri di configurazione di `llm-gateway-demo` sono definiti nel file **`./config_ner_demo/config.yaml`**:

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

## Avvio con Docker

È possibile eseguire `llm-gateway-demo` in un container Docker isolato montando sia `./config_ner_demo` che `./keys`:

### 1. Build ed esecuzione tramite Docker Compose
```bash
docker compose up -d --build
```

Il file `docker-compose.yml` monta automaticamente `./config_ner_demo` in `/app/config_ner_demo:ro`:
qualsiasi modifica ai parametri nel file `config.yaml` viene caricata all'avvio senza dover ricostruire l'immagine.

### 2. Esecuzione manuale con Docker CLI
```bash
# Build dell'immagine
docker build -t llm-gateway-demo .

# Avvio del container con montaggio volumi
docker run -d --name llm-gateway-demo \
  -p 8090:8090 \
  -v ./config_ner_demo:/app/config_ner_demo:ro \
  -v ./keys:/app/keys \
  --add-host=host.docker.internal:host-gateway \
  llm-gateway-demo
```

---

## Architettura e Porte

| Servizio | Porta | Ruolo |
| :--- | :--- | :--- |
| **`llm-gateway-demo`** | `8090` | Interfaccia Web + Simulatore Client JWT `ES256` + Mock/Inspector OpenAI (`/mock-openai/v1/chat/completions`) |
| **`llm-gateway`** | `8095` | Gateway originale (sola lettura) |
| **`llm-gateway-ner`** | `8097` | Microservizio NER `spaCy` (`it_core_news_lg`) |
| **`llm-ner-gateway`** | `8096` | Nuovo Gateway con supporto `PIVA`, `ADDRESS`, `CATASTO`, `DOCID`, `TARGA` e policy `legal-copilot` |
| **`llm-ner-gateway-ner`** | `8098` | Microservizio NER `rizzoaiacademy/rizzo-pii-0.3B` |

---

## Funzionalità della UI (`http://localhost:8090`)

- **Selettore Gateway**: permette di interrogare `llm-gateway (:8095)`, `llm-ner-gateway (:8096)` oppure eseguire un **Confronto Side-by-Side** in parallelo.
- **Health Badges in Tempo Reale**: stato live dei 4 microservizi.
- **Selettore Policy (`application` + `purpose`)**:
  - `dentalcare / copilot-admin`
  - `dentalcare / clinical-copilot`
  - `dentalcare / smart-error`
  - `legal / case-analysis` (attiva su `llm-ner-gateway`)
- **Gestione `conversationRef`**: mantiene o azzera il `TokenVault` in RAM tra turni successivi.
- **Modalità Fornitore**:
  - *Mock LLM Ispezionabile (Offline)*: risponde riutilizzando i token pseudonimizzati (`PERSONA_1`, `TEL_1`, UUID surrogati) per verificare il ripristino automatico, oppure inietta un token allucinato (`PERSONA_99`) per testare il blocco `502 UNKNOWN_TOKEN`.
  - *OpenAI Reale*: inoltra la richiesta pseudonimizzata a `https://api.openai.com/v1/chat/completions`.
- **Vista a 3 Pannelli**:
  1. *Client → Gateway* (dati reali con evidenziazione delle PII in ingresso).
  2. *Gateway → Fornitore LLM* (payload intercettato dopo la pseudonimizzazione/rimozione e lista dei campi DTO rimossi con `DROP`).
  3. *Gateway → Client* (risposta finale ripristinata e latenza in millisecondi).
- **11 Scenari Preconfigurati (modellati sui flussi e DTO reali di `D:\dentalcare`)**:
  1. **Comando breve e verbi di contatto** (`dentalcare/copilot-admin`): evidenzia come Rizzo-PII isoli il nome proprio senza inglobare il verbo imperativo (`Chiama`).
  2. **Contesto UI + `PatientDetailDto` (2 passate)** (`dentalcare/copilot-admin`): riproduce `[CONTESTO UI]` di `ChatService.java`, la propagazione 2-pass del cognome (`Rossi` → `PERSONA_1`), il surrogato UUID e il `DROP` di 13 campi clinici e fiscali.
  3. **Anamnesi e Note Cliniche** (`dentalcare/clinical-copilot`): dimostra che per il medico i flag clinici (`bloodType`, `toothNumber`, `allergyPenicillin`, `takingAnticoagulants`) passano in chiaro (`ALLOW`), le note (`anamnesisNotes`, `otherAllergies`, `notes`) sono bonificate come `TEXT` (tokenizzando nomi/telefoni e rimuovendo CF e indirizzo), mentre `fiscalCode` e `billingIban` restano in `DROP`.
  4. **Briefing Mattutino e Agenda Annidata (`getDashboard`)** (`dentalcare/copilot-admin`): verifica la sanitizzazione ricorsiva di `todayAppointments` (`NESTED`) e il `DROP` di `todayAppointments.notes`.
  5. **Richiami Periodici Scaduti (`getRecalls`)** (`dentalcare/copilot-admin`): verifica la sanitizzazione di un array JSON `List<RecallDto>` con più pazienti e il `DROP` di `notes`.
  6. **Fatturazione, P.IVA, SDI e Indirizzi (`getInvoices`)** (`dentalcare/copilot-admin`): confronta `spaCy` e `Rizzo-PII` su Partita IVA, CF, IBAN, indirizzo, catasto, documento e targa, oltre al `DROP` dei campi fiscali di `InvoiceDto`.
  7. **Preventivo AI da Odontogramma (`getEstimates`)** (`dentalcare/clinical-copilot`): conserva i numeri dei denti (`#16, #24, #36`) e gli importi mentre pseudonimizza paziente e odontoiatra dentro `estimateTitle` e `notes`.
  8. **Smart Error Sistema TS / SDI** (`dentalcare/smart-error`): riproduce il formato `OPERAZIONE / CODICE / MESSAGGIO` di `ErrorExplanationService.java` con redazione irreversibile (`[NOME]`, `[ID]`, `[TELEFONO]`, `[EMAIL]`) e rimozione di CF, P.IVA, indirizzo e IBAN.
  9. **Voci di Menu DentalCare e Nomi Ambigui** (`dentalcare/copilot-admin`): verifica l'assenza di falsi positivi sulle sezioni applicative (`Agenda`, `Fatturazione`, `Preventivi`, `Richiami`, `Magazzino`) e il riconoscimento di nomi omografi (`Rosa Pellegrini`, `Viola Valsecchi`, `Celeste Riva`, `cerca Giulia Bianchi`, `Felice Serafini`).
  10. **Sicurezza Fail-Closed su Tool Call (`502 UNKNOWN_TOKEN`)** (`dentalcare/copilot-admin`): blocca il tentativo del modello di invocare `getPatientDetail` con un token allucinato (`PERSONA_99`).
  11. **Pratica Legale / Perizia Odontoiatrica** (`legal/case-analysis`): scenario giuridico completo su `llm-ner-gateway`.

