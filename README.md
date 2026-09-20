# DeepTune Hack - Metabase RL Evaluation Platform

A high-performance evaluation platform for running, monitoring, and grading computer-use Reinforcement Learning (RL) agent rollouts on Metabase.

## Components Summary

- **`backend/`**: FastAPI REST server, SQLite persistence (`deeptune.db`), job/rollout worker engine, Playwright browser environment, and benchmark grader.
- **`frontend/`**: Dark-themed console UI (`frontend/index.html`) for real-time monitoring of evaluation runs, agent trajectories, screenshots, and task rollouts.
- **`metabase/`**: Metabase repository submodule serving as the benchmark evaluation environment.
- **`computer-use-preview/`**: Gemini Computer Use agent integration submodule.
- **`tasks.json`**: Benchmark task definitions, prompts, and evaluation criteria.
- **`tests/`**: Pytest suite covering API endpoints, grader logic, job persistence, and failure taxonomy.

## Prerequisites

- Docker & Docker Compose
- Python 3.10+
- Node.js & Playwright dependencies

## Running Instructions

### 1. Start Target Environment (Metabase & PostgreSQL)

```bash
docker compose -f backend/docker-compose.yml up -d
```

### 2. Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 3. Launch Backend API

```bash
uvicorn backend.api.server:app --reload --port 8000
```
- API Base URL: `http://localhost:8000`
- Interactive API Docs: `http://localhost:8000/docs`

### 4. Open Frontend Console

Open `frontend/index.html` in your browser, or host it locally:

```bash
python -m http.server 3000 --directory frontend
```
Navigate to `http://localhost:3000`.

### 5. Run Test Suite

```bash
pytest
```
