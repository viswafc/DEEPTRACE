# Architecture Overview

DeepTrace is built as a three-tier system:

## 1. Frontend (Vanilla JS + Chart.js)
A lightweight SPA that renders interactive visualizations. It uses a custom `ApiClient` to manage REST and WebSocket connections. State is minimally maintained per-job, allowing comparison modes to store baselines.

## 2. Backend (FastAPI)
The orchestration layer.
- **REST API**: Job submission, status polling, and export.
- **WebSocket**: Streams real-time progress and completion events.
- **Job Store**: In-memory dict backed by aiofiles for JSON persistence.
- **Sandbox Engine**: Evaluates AST (Python) or source text (Java) for malicious signatures, then forks a resource-limited subprocess to execute code.

## 3. Profiling Engine
- **Python**: Injects a custom wrapper (`_PYTHON_WRAPPER`) that attaches `tracemalloc`, `gc.callbacks`, and polls `psutil` in a background thread.
- **Java**: Compiles via `javac` and executes with `-Xlog:gc*`.
- **System**: Uses `strace -c` on Linux to hook into syscall counts/timing.
- **Heuristics**: `_analyse_bottlenecks` evaluates peak metrics against thresholds to generate actionable flags.
