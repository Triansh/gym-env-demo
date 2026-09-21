import time, requests, sys
timeout = 300
start = time.time()
while time.time() - start < timeout:
    try:
        r = requests.get("http://localhost:8000/api/jobs/history")
        r.raise_for_status()
        summary = r.json().get("summary", {})
        active = summary.get("active_jobs_count", 0)
        jobs = r.json().get("jobs", [])
        
        print(f"[{time.strftime('%H:%M:%S')}] Active: {active}")
        for j in [job for job in jobs if job['status'] not in ['COMPLETE', 'COMPLETED', 'FAILED', 'CANCELLED']][:3]:
           print(f"  Job {j.get('job_id')[:8]} | Status: {j.get('status')} | Completes: {j.get('completed')}/{j.get('total_rollouts')} [{j.get('duration_seconds', 0)}s]")
           
        if active == 0:
            print("Done")
            sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
    sys.stdout.flush()
    time.sleep(5)
print("Timeout")
sys.exit(1)
