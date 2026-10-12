# Deploying to Render (free tier)

The app deploys as **one free Render Web Service** built from the repository's `Dockerfile`. FastAPI serves the compiled React frontend and every API endpoint from a single process. No database service, disk or paid plan is needed.

| File | Purpose |
|---|---|
| [`Dockerfile`](../Dockerfile) | Two stages. Node 20 builds `frontend/dist`. Python 3.12-slim installs `backend/requirements.txt` and copies the backend, the policy config, the 12 demo receipts with `fixtures.json`, and the built frontend. Runs as a non-root user with one uvicorn worker on `0.0.0.0:$PORT`. |
| [`.dockerignore`](../.dockerignore) | Keeps `.env` files, keys, local databases, `data/`, virtual environments, `node_modules`, caches, tests, docs and git metadata out of the build context. |
| [`render.yaml`](../render.yaml) | Render Blueprint: Docker runtime, `plan: free`, health check `/api/health`, demo environment variables. |
| [`scripts/verify_deployment.py`](../scripts/verify_deployment.py) | End-to-end check of a running deployment, using only the Python standard library. |

## 1. Deploy

**Option A — Blueprint (recommended).**

1. Push the repository, including `Dockerfile`, `.dockerignore` and `render.yaml`, to GitHub.
2. In the [Render Dashboard](https://dashboard.render.com), choose **New → Blueprint**, connect GitHub and select the `exp-reimb-agent` repository.
3. Render reads `render.yaml` and proposes one web service, **exp-reimb-agent**, on the **Free** plan. Click **Apply**.
4. The first build takes a few minutes (`npm ci`, the Vite build, `pip install`). When the deploy log shows `Uvicorn running on http://0.0.0.0:10000` and the health check passes, the service is **Live** at `https://exp-reimb-agent.onrender.com`. If that name is taken, Render shows a different subdomain on the service page.

**Option B — manual Web Service.**

1. **New → Web Service** → connect the repository.
2. **Language:** Docker. **Branch:** `main`. **Root directory:** leave empty. **Dockerfile path:** `./Dockerfile`.
3. **Instance type:** Free.
4. **Advanced → Health Check Path:** `/api/health`.
5. Add the environment variables from the table below, then **Create Web Service**.

Do not set a start command. The Dockerfile's `CMD` already starts uvicorn on Render's `PORT` (10000 by default).

## 2. Environment variables

| Variable | Value | Notes |
|---|---|---|
| `EXTRACTION_MODE` | `offline` | Default in both the image and the Blueprint. The public demo replays the recorded extractions for the 12 bundled receipts and never calls an AI API. |
| `DATA_DIR` | `/tmp/expense-agent` | SQLite database and uploads. Writable but **ephemeral** on the free tier. |
| `REFERENCE_DATE` | `2026-10-07` | Pins "today" for the 60-day and future-date rules, so the September 2026 demo receipts behave identically on any day. Remove it to use the real date. |
| `OFFLINE_PROCESSING_DELAY` | `0.9` | Seconds of simulated processing per document, so viewers see the pipeline run. |
| `WORKER_THREADS` | `2` | Document-processing threads. Kept low for the free plan's 0.1 CPU and 512 MB. |
| `PORT` | *(set by Render)* | Do not set it yourself. |

**Optional: live Claude extraction.** This consumes your Anthropic credits, and anyone with the URL can trigger it.

1. Service → **Environment** → **Add Environment Variable**: key `ANTHROPIC_API_KEY`, value = your key. Render stores it as a server-side secret. It is never sent to the browser and is not in the repository.
2. Change `EXTRACTION_MODE` to `auto` (Claude when the key is present, offline otherwise) or `claude`.
3. Optionally set `ANTHROPIC_MODEL` (default `claude-opus-5-5`) and `ANTHROPIC_EFFORT` (default `low`).
4. **Save, rebuild and deploy.** `GET /api/health` should now report `"mode": "claude"`.

**Optional: OpenAI photo and PDF reading.** This lets the hosted app read real receipt photos and scanned PDFs with an OpenAI key. It consumes your OpenAI credits, and anyone with the URL can trigger it, so set a monthly spend limit on the key (OpenAI dashboard → Limits) before enabling it.

1. Service → **Environment** → **Add Environment Variable**: key `OPENAI_API_KEY`, value = your key (a server-side secret, as above).
2. Change `EXTRACTION_MODE` to `auto` (OpenAI is used when no Anthropic key is present) or `openai`.
3. Optionally set `OPENAI_MODEL` (default `gpt-5.4-mini`, a vision-capable model).
4. **Save, rebuild and deploy.** `GET /api/health` should report `"mode": "openai"` and `"chat": "rules"`.

OpenAI is used only to read documents. Free-text chat stays on the deterministic interpreter (Claude tool use needs an Anthropic key). In every mode, the 12 bundled demo receipts replay their recorded extractions and never call an API. The demo therefore stays at 8/2/1/1 and costs nothing; only new uploads are sent to the AI.

The Blueprint deliberately does not declare `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. Render prompts for every secret declared with `sync: false` when a Blueprint is created, and the public demo needs no key.

## 3. Verify

1. **Health:** `https://<service>.onrender.com/api/health` returns
   `{"ok": true, "ai": {"mode": "offline", "model": null}, "referenceDate": "2026-10-07"}`.
2. **Automated end-to-end check**, from a machine with Python 3.10+ and no extra packages:
   ```bash
   python scripts/verify_deployment.py https://<service>.onrender.com
   ```
   It checks the health endpoint and the UI bundle, uploads the 12 demo receipts and checks the summary (8 ready / 2 need information / 1 over policy / 1 duplicate). It then answers the client-dinner and parking-date questions, caps the hotel, confirms the duplicate, checks READY_TO_SUBMIT, confirms that a submit without confirmation is refused, and submits. It ends with an `ER-YYYY-NNNNNN` reference. Every line should read `PASS`. Each run creates one demo claim on the service.
3. **Manual check in the browser:** open the URL, click **Use the 12 demo receipts** and follow the demo script in the [README](../README.md#9-demo-scenario-about-2-minutes).

If the first request is slow, see *spin-down* below. If the deploy fails, the **Logs** tab shows the Docker build and uvicorn output.

## 4. Free-tier limitations

From [Render's free instance documentation](https://render.com/docs/free) and [Blueprint reference](https://render.com/docs/blueprint-spec):

* **Spin-down:** a free web service spins down after **15 minutes** without inbound traffic. The next request wakes it in **about one minute**. Open the URL a minute before a live demo.
* **Ephemeral filesystem:** the SQLite database and uploaded receipts in `/tmp/expense-agent` are **lost on every redeploy, restart or spin-down**. Free services cannot attach a persistent disk. This is acceptable for a demo, because claims are expected to be short-lived. Render may also restart free services at any time.
* **Resources:** the free plan is listed at **0.1 CPU / 512 MB RAM**. In offline mode the app's work is light, but expect slower responses than locally.
* **Single instance:** free services can't scale beyond one instance. That matches the app's design constraint: it must run exactly one worker, because document processing uses an in-process thread pool and lock, and SQLite is local.
* **Monthly hours:** **750 free instance hours** per workspace per month. If they run out, free services are suspended until the next month.
* **No shell access**, no outbound SMTP ports (the app doesn't use email), and no persistent disks.
* **Public and unauthenticated:** anyone with the URL can create and submit demo claims. The data is fictional and the finance API is a mock. Do not upload real receipts to the public demo.

## 5. Running the image elsewhere

The image doesn't depend on Render:

```bash
docker build -t exp-reimb-agent .
docker run --rm -p 8000:8000 exp-reimb-agent                         # offline demo
docker run --rm -p 8000:8000 -e EXTRACTION_MODE=auto -e ANTHROPIC_API_KEY=... exp-reimb-agent
python scripts/verify_deployment.py http://127.0.0.1:8000
```

The container reports its own health through the Dockerfile's `HEALTHCHECK` (`GET /api/health`). Local development without Docker is unchanged: `run.sh` / `run.ps1`.

## 6. How this was verified

Docker was not available on the machine used to prepare this deployment, so **the image itself has not yet been built** there. The following was verified instead:

* The clean frontend build (`npm ci && npm run build`, as in stage 1) succeeds.
* The image's `/app` layout was assembled from exactly the paths the Dockerfile copies (`backend/app`, `backend/config`, `demo_receipts`, `frontend/dist`), with no `.env`. It was started with the Dockerfile's exact `uvicorn … --host 0.0.0.0 --port ${PORT} --workers 1` command and the Render environment (`PORT=10000`, `EXTRACTION_MODE=offline`, `DATA_DIR` in a temp directory). `scripts/verify_deployment.py` passed every check, including submission with reference `ER-2026-001029`. The database and uploads were written only to the temp directory.
* A simulation of the `.dockerignore` rules confirmed that the build context contains every path the Dockerfile copies (52 files) and excludes `.env` files, virtual environments, databases, `node_modules`, caches and git metadata.
* Render's first build is therefore the first real `docker build`. Check its log, then run `scripts/verify_deployment.py` against the live URL.
