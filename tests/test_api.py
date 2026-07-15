"""
API integration tests (requires running backend at localhost:8000)
Use: pytest tests/test_api.py -v
"""
import requests
import time
import pytest

BASE = 'http://localhost:8000/api'

def test_health():
    res = requests.get(f"{BASE}/health")
    assert res.status_code == 200
    assert res.json()['status'] == 'ok'

def test_submit_python():
    code = "print('hello')"
    res = requests.post(f"{BASE}/submit", json={"code": code, "language": "python"})
    assert res.status_code == 200
    data = res.json()
    assert "job_id" in data
    assert data["status"] == "queued"

def test_full_flow():
    code = "sum(range(1000))"
    res = requests.post(f"{BASE}/submit", json={"code": code, "language": "python"})
    job_id = res.json()["job_id"]
    
    # Poll until done
    for _ in range(10):
        status_res = requests.get(f"{BASE}/status/{job_id}")
        if status_res.json()["status"] in ["DONE", "ERROR"]:
            break
        time.sleep(1)
        
    assert status_res.json()["status"] == "DONE"
    
    # Get results
    results_res = requests.get(f"{BASE}/results/{job_id}")
    assert results_res.status_code == 200
    metrics = results_res.json()
    
    assert "runtime_ms" in metrics
    assert "memory" in metrics
    assert "gc" in metrics

def test_security_block():
    code = "import os; os.system('echo pwned')"
    res = requests.post(f"{BASE}/submit", json={"code": code, "language": "python"})
    assert res.status_code == 400
    assert "Security risk detected" in res.json()["detail"]
