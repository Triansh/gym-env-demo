import time, requests, sys
timeout = 450
start = time.time()
while time.time() - start < timeout:
    try:
        r = requests.get("http://localhost:8000/api/jobs/history")
        active = r.json().get("summary", {}).get("active_jobs_count", 0)
        out = f"Active jobs: {active}\n"
        if active == 0:
            print("Done")
            sys.exit(0)
    except Exception:
        pass
    time.sleep(15)
sys.exit(1)
