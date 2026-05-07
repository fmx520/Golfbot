# Golfbot Frontend MVP

This frontend calls FastAPI endpoints for:
- `GET /courses`
- `POST /jobs`
- `GET /jobs/:id/stream` (SSE)
- `GET /jobs/:id` (polling fallback)

## 1) Run backend API

From repo root:

```bash
GOLFBOT_CORS_ORIGINS=http://localhost:3000 uvicorn src.web.app:app --host 0.0.0.0 --port 8000
```

## 2) Run frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000`.

If your API is not on `http://localhost:8000`, set:

```bash
NEXT_PUBLIC_GOLFBOT_API_BASE=http://your-host:port npm run dev
```

## Current behavior

- Uses a stream-first model (SSE) and automatically falls back to polling.
- `course_id` input is backed by `GET /courses` suggestions.
- Backend worker remains simulated for final booking submit path.
