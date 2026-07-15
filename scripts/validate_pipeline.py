#!/usr/bin/env python
"""
DeepTrace Pipeline Validator
Tests that data flows from code submission through profiling to metric output.
Runs standalone (no server needed) by importing engine directly.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

# Add backend directory to sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.insert(0, backend_dir)

from storage.store import job_store
from models.job import Job
from profiler.engine import run_profiling_job

def print_result(name, success, details=""):
    status = "PASS" if success else "FAIL"
    color = "\033[92m" if success else "\033[91m"
    reset = "\033[0m"
    print(f"[{color}{status}{reset}] {name}")
    if details:
        print(f"       {details}")
    return success

async def test_python_pipeline():
    print("\n--- Running Python Pipeline Test ---")
    job_id = str(uuid.uuid4())
    code = """
def slow_function():
    a = []
    for i in range(1000):
        a.append(i * 2)
    return sum(a)
print(slow_function())
"""
    job = Job(id=job_id, language="python", code=code, created_at=datetime.now(timezone.utc))
    job_store._jobs[job_id] = job
    
    try:
        await run_profiling_job(job_id)
        updated_job = await job_store.get_job(job_id)
        
        if updated_job.status != "DONE":
            return print_result("Python profiling completion", False, f"Job ended in state {updated_job.status}. Error: {updated_job.error}")
            
        metrics = updated_job.metrics
        if not metrics:
            return print_result("Python metrics generation", False, "No metrics returned")
            
        memory_ok = metrics.get('memory', {}).get('peak_mb', 0) > 0
        runtime_ok = metrics.get('runtime_ms', 0) > 0
        
        if memory_ok and runtime_ok:
            return print_result("Python Pipeline End-to-End", True)
        else:
            return print_result("Python Pipeline End-to-End", False, f"Missing critical metrics. Memory peak: {metrics.get('memory', {}).get('peak_mb')}, Runtime: {metrics.get('runtime_ms')}")
            
    except Exception as e:
        return print_result("Python Pipeline", False, f"Exception occurred: {e}")

async def main():
    print("=== DeepTrace Pipeline Validator ===")
    
    # Init store
    os.environ['DEEPTRACE_JOBS_DIR'] = os.path.join(os.environ.get('TEMP', '/tmp'), 'deeptrace_test_jobs')
    await job_store.initialise()
    
    success_count = 0
    total_count = 0
    
    res = await test_python_pipeline()
    success_count += int(res)
    total_count += 1
    
    print("\n=== Validation Summary ===")
    print(f"Passed: {success_count}/{total_count}")
    
    if success_count == total_count:
        print("✅ Pipeline validation successful!")
        sys.exit(0)
    else:
        print("❌ Pipeline validation failed.")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
