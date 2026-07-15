# DeepTrace Setup Guide

## Docker (Recommended)
Docker provides an isolated Linux environment where `strace` and all required SDKs are pre-installed.
1. Run `docker-compose up --build`
2. Access `http://localhost:3000`

## Local Setup (Linux)
1. Run `./scripts/setup_local.sh`
2. Follow the instructions to start the backend and frontend.

## Local Setup (Windows)
Note: Windows does not support `strace`. DeepTrace will gracefully fall back to `psutil`.
1. Run `.\scripts\setup_local.ps1` from PowerShell.
2. Start backend: `cd backend; .\venv\Scripts\activate; uvicorn main:app`
3. Start frontend: `cd frontend; python -m http.server 3000`

## Verifying the Installation
Run the pipeline validation script to ensure profiling metrics are correctly captured:
```bash
python scripts/validate_pipeline.py
```
