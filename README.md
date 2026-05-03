# quizmd-server

Backend multiplayer server for QuizMD rooms (Cloud Run ready).

## Features (v1)

- Room creation and join via REST
- Real-time gameplay via WebSockets
- Host-authoritative state and scoring
- Modes:
  - `compete`: top 3 fastest correct answers score `3,2,1`; wrong `-3`
  - `collaborate`: discussion phase (chat) then voting phase; unanimous correct required, otherwise retry same question
- Random funny default names with uniqueness handling
- Single-instance in-memory state (v1)

## Project Structure

- `app/main.py`: FastAPI app and endpoints
- `app/room_store.py`: room/session state machine
- `app/game_engine.py`: scoring and consensus logic
- `app/models.py`: API and event schema models
- `app/namegen.py`: nickname generation
- `tests/test_server.py`: unit/integration smoke tests

## Local Run

Python version note:
- Use **Python 3.13** for local server development.
- Python 3.14 may fail when installing dependencies (`pydantic-core` build issue).

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

Health check:

```bash
curl http://127.0.0.1:8080/healthz
```

Run tests:

```bash
python -m pytest tests -q
```

## API Quickstart

Validation note:
- `questions[].time_limit` must be `>= 5` seconds (otherwise room creation returns HTTP `422`).
- Optional `questions[].discussion_time` controls collaborate chat seconds per question (`0` disables discussion phase for that question).

Create room:

```bash
curl -X POST http://127.0.0.1:8080/rooms \
  -H 'content-type: application/json' \
  -d '{
    "mode": "compete",
    "token_required": false,
    "quiz_title": "Demo Quiz",
    "host_name": "Host",
    "questions": [
      {
        "title": "Question 1",
        "question": "2+2?",
        "options": ["3", "4", "5", "6"],
        "correct": [2],
        "type": "single",
        "time_limit": 30,
        "explanation": "2+2 is 4"
      }
    ]
  }'
```

Join room:

```bash
curl -X POST http://127.0.0.1:8080/rooms/<ROOM_CODE>/join \
  -H 'content-type: application/json' \
  -d '{"player_name":"Mary"}'
```

If `token_required=true` at room creation, include `"room_token":"<ROOM_TOKEN>"` in join payloads.

WebSocket endpoint:

```text
ws(s)://<host>/rooms/<ROOM_CODE>/ws?player_id=<PLAYER_ID>&token=<PLAYER_TOKEN>
```

Client events:

- `ready_toggle` payload `{ "ready": true }`
- `start_game` payload `{}`
- `submit_answer` payload `{ "question_index": 0, "answers": [2] }`
- `chat_message` payload `{ "text": "..." }`
- `ping` payload `{}`
- `leave_room` payload `{}`

## Cloud Run Deploy (europe-west1)

Prerequisites:

- Billing-enabled GCP project
- `gcloud` CLI installed and authenticated

Enable APIs:

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

Deploy:

```bash
gcloud config set project <YOUR_PROJECT_ID>

gcloud run deploy quizmd-server \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated \
  --port 8080 \
  --min-instances 1 \
  --max-instances 1 \
  --timeout 3600 \
  --set-env-vars ROOM_MAX_PLAYERS=16,QUESTION_TIMEOUT_SECONDS=30,ROOM_TTL_MINUTES=30,HOST_REJOIN_SECONDS=60,ROOM_TRANSITION_COUNTDOWN_SECONDS=5,COLLABORATE_MAX_RETRIES=3,COLLABORATE_DISCUSSION_SECONDS=40
```

Optional public URL override (for join links):

```bash
gcloud run services update quizmd-server \
  --region europe-west1 \
  --set-env-vars QUIZMD_PUBLIC_BASE_URL=https://<your-service-url>
```

## Cloud Build Continuous Deploy

This repo includes `cloudbuild.yaml`, so you can connect GitHub -> Cloud Build -> Cloud Run.

In Cloud Run UI ("Set up with Cloud Build"):

1. Source repository: `steliosot/quizmd-server`
2. Branch: `main`
3. Build type: Dockerfile (repo root)
4. Build config file: `cloudbuild.yaml`
5. Service name: `quizmd-server`
6. Region: `europe-west1`

After first deploy, set:

```bash
gcloud run services update quizmd-server \
  --region europe-west1 \
  --set-env-vars QUIZMD_PUBLIC_BASE_URL=https://<cloud-run-service-url>
```

## Notes

- v1 uses in-memory room state. If container restarts, active rooms are lost.
- For multi-instance scaling and persistence, add Redis in v2.
