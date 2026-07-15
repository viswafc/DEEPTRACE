# API Reference

## REST API

### `GET /api/health`
Returns connection status and platform capabilities (e.g., whether Java or Strace are available).

### `POST /api/submit`
**Body:** `{"code": "print('hello')", "language": "python"}`
**Response:** `{"job_id": "...", "status": "queued"}`

### `GET /api/status/{job_id}`
Returns current job status and progress (0-100%).

### `GET /api/results/{job_id}`
Returns the full `ProfileMetrics` JSON payload containing memory timelines, GC events, syscall aggregations, and bottlenecks.

### `POST /api/compare`
**Body:** `{"job_id_a": "...", "job_id_b": "..."}`
Calculates deltas across key metrics and returns a comparison object.

### `GET /api/export/{job_id}?format=json`
Triggers a download of the profile metrics.

## WebSocket API

### `WS /ws/{job_id}`
Streams real-time updates as JSON:
- `{"type": "progress", "data": {"progress": 45}}`
- `{"type": "done", "data": {...}}`
- `{"type": "error", "data": {"error": "..."}}`
