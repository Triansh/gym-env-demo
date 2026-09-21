import time, requests, sys
timeout = 300
start = time.time()
while time.time() - start < timeout:
    try:
        r = requests.get("http://localhost:8000/api/jobs/history")
        active = r.json().get("summary", {}).get("active_jobs_count", 0)
        jobs = r.json().get("jobs", [])
        
        with open('monitor_status.txt', 'w') as f:
            f.write(f"Active Jobs: {active}\n")
            for j in [x for x in jobs if x['status'] not in ['COMPLETE', 'COMPLETED', 'FAILED', 'CANCELLED']]:
                f.write(f"Job {j.get('job_id')[:8]} | Status: {j.get('status')} | Completes: {j.get('completed')}/{j.get('total_rollouts')}\n")
        
        if active == 0:
            sys.exit(0)
    except Exception as e:
        pass
    time.sleep(5)
sys.exit(1)
