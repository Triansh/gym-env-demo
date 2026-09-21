import time, requests, sys
while True:
    try:
        r = requests.get("http://localhost:8000/api/jobs/history")
        r.raise_for_status()
        data = r.json()
        active = data.get("summary", {}).get("active_jobs_count", 0)
        jobs = data.get("jobs", [])
        
        out = f"[{time.strftime('%H:%M:%S')}] Active: {active}\n"
        for j in jobs:
            if j['status'] not in ['COMPLETE', 'COMPLETED', 'FAILED', 'CANCELLED', 'PASS', 'PASSED']:
                out += f"  Job {j['job_id'][:8]} | Status: {j['status']} | Done: {j.get('completed', 0)}/{j.get('total_rollouts', 0)}\n"
        sys.stdout.write(out)
        sys.stdout.flush()
                
        if active == 0:
            print("All jobs completed!")
            break
    except Exception as e:
        print(f"Error checking status: {e}")
    time.sleep(15)
