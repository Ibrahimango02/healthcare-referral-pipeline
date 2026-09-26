# healthcare-referral-pipeline

An intake pipeline for clinical referrals. It takes HL7 v2 referral messages
(`REF^I12`), pulls structured fields out of the free-text notes with an LLM,
validates the result, routes anything doubtful to a human reviewer, and writes
the approved referral to a FHIR R4 server as `Patient`, `Practitioner` and
`ServiceRequest` resources.

The pipeline is a [Temporal](https://temporal.io) workflow, so each referral
is durable: if the worker crashes it resumes from the last completed step, and
a referral waiting on review can sit for hours or days without holding any
resources.

## How it works

```
HL7 v2 message
   │
   ▼
parse_hl7 ──────────► malformed? fail fast (not retried)
   │
   ▼
extract_fields ─────► Claude (tool use, fixed JSON schema)
   │                  or a keyword heuristic if no API key is set
   ▼
validate_referral ──► errors?          → REJECTED
   │                  review reasons?  → AWAITING_REVIEW ─► approve / reject / timeout
   ▼                                                        (reviewer can correct fields)
write_fhir ─────────► FHIR transaction Bundle (or ./out/*.bundle.json)
   │
   ▼
COMPLETED
```

| Step | Module | What it does |
| --- | --- | --- |
| Parse | `intake/hl7_parser.py` | Reads MSH, RF1, PRD, PID, DG1 and NTE segments deterministically into a `ParsedReferral`. |
| Extract | `intake/extraction.py` | Sends the coded priority, diagnoses and clinical notes to Claude with a forced tool call. Returns requested procedure, body site, indication, urgency, suspected conditions, missing information and a confidence score. |
| Validate | `intake/validation.py` | Pure rules, shared with the AWS Lambda handler. See below. |
| Review | `intake/workflows.py` | Waits for a `submit_review` signal (default timeout 24 h). Reviewer corrections override the LLM output. |
| Write | `intake/fhir.py` | Builds a transaction Bundle and POSTs it to `FHIR_BASE_URL`. |

### Validation rules

**Hard errors** (the referral is rejected):
- missing patient MRN (PID-3), name (PID-5) or a valid date of birth (PID-7)

**Review reasons** (a human must approve before the FHIR write):
- no referring provider, or an NPI that fails the check digit
- a diagnosis code that isn't in ICD-10 format
- no requested procedure could be determined
- extraction confidence below 0.8
- the notes suggest a higher urgency than the coded HL7 priority
- anything the extractor lists as missing information

### Reliability

- **Duplicate messages.** The workflow ID is `referral-<MSH-10>`, and duplicate IDs are rejected. A resent HL7 message can't start a second intake.
- **Retries.** LLM calls retry with exponential backoff, and FHIR writes retry until they succeed. Parse errors are marked non-retryable.
- **Idempotent FHIR writes.** Patient and Practitioner use conditional create on MRN and NPI. The ServiceRequest is keyed on the message control ID, so a retried write doesn't create duplicates.

## Project layout

```
intake/
  hl7_parser.py    HL7 v2 → ParsedReferral
  extraction.py    LLM / heuristic field extraction
  validation.py    validation rules (pure functions)
  fhir.py          FHIR Bundle building and writing
  models.py        Pydantic models passed between steps
  activities.py    Temporal activities (the I/O steps)
  workflows.py     ReferralIntakeWorkflow
  worker.py        Temporal worker entry point
  cli.py           submit / status / review / result commands
lambda_handler.py  AWS Lambda entry point for the validation step
samples/           example referral messages
tests/             unit and workflow tests
```

## Getting started

Requires Python 3.10+ and Docker.

```bash
pip install -r requirements.txt

# Start the Temporal dev server (UI at http://localhost:8233)
# and a HAPI FHIR server (http://localhost:8080/fhir)
docker compose up -d

# Optional: without these, extraction uses the heuristic and
# bundles are written to ./out/ instead of a FHIR server
export ANTHROPIC_API_KEY=sk-ant-...
export FHIR_BASE_URL=http://localhost:8080/fhir

# Run the worker
python -m intake.worker
```

In a second terminal, use the CLI:

```bash
# Submit a clean referral and wait for the result
python -m intake.cli submit samples/referral_routine.hl7 --wait

# Submit one that needs review, then check on it
python -m intake.cli submit samples/referral_needs_review.hl7
python -m intake.cli status REF0002

# Approve it, correcting a field, or reject it
python -m intake.cli review REF0002 --approve --reviewer "dr.lee" \
    --set requested_procedure="CT head without contrast" --notes "Confirmed urgent"
python -m intake.cli review REF0002 --reject --reviewer "dr.lee" --notes "Send to ER"

# Get the final result
python -m intake.cli result REF0002
```

### Sample messages

| File | Control ID | Expected outcome |
| --- | --- | --- |
| `referral_routine.hl7` | REF0001 | Clean routine abdominal ultrasound. Completes without review when the LLM is used. |
| `referral_needs_review.hl7` | REF0002 | Coded routine but the notes describe red-flag symptoms. Goes to review. |
| `referral_malformed.hl7` | REF0003 | Missing patient identifiers. Rejected. |

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `TEMPORAL_ADDRESS` | `localhost:7233` | Temporal frontend address |
| `TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `ANTHROPIC_API_KEY` | unset | Enables LLM extraction. Without it, the heuristic runs and its low-confidence output goes to review. |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Model used for extraction |
| `FHIR_BASE_URL` | unset | FHIR server base URL. Without it, bundles go to `./out/`. |
| `VALIDATION_BACKEND` | local | Set to `lambda` to run validation in AWS Lambda |
| `VALIDATION_LAMBDA_NAME` | unset | Lambda function name, when `VALIDATION_BACKEND=lambda` |

### Running validation in AWS Lambda

Deploy the `intake/` package together with `lambda_handler.py` and set the
handler to `lambda_handler.handler`. Then start the worker with
`VALIDATION_BACKEND=lambda VALIDATION_LAMBDA_NAME=<function-name>`. The Lambda
runs the same rules as `intake/validation.py`, so the two modes behave the same.

## Tests

```bash
pytest
```

Parser, validation, extraction and FHIR tests run offline; the LLM and FHIR
server are mocked. `tests/test_workflow.py` runs the real workflow against a
Temporal dev server, which it downloads on first run. If you already have the
Temporal CLI installed, set `TEMPORAL_CLI_PATH` to skip the download.
