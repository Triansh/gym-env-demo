import pytest
from fastapi.testclient import TestClient
from backend.api.server import app, store
from backend.models import JobStatus

client = TestClient(app)

def test_ground_truth_redaction(tmp_path):
    # Submit a job with expected logic
    request_data = {
        "tasks": [
            {"id": "t1", "task": "What is 2+2?", "answer": "4"}
        ],
        "attempts": 1
    }
    
    response = client.post("/api/jobs", json=request_data)
    assert response.status_code in (200, 201), response.text
    job_id = response.json()["job_id"]
    
    # Wait for the job to appear in DB
    job_response = client.get(f"/api/jobs/{job_id}")
    assert job_response.status_code == 200
    
    # Because the job is freshly created, it should be PENDING or RUNNING
    # The expected answer should be redacted
    job_data = job_response.json()
    assert job_data["status"] in (JobStatus.RUNNING.value, JobStatus.QUEUED.value)
    
    # tasks inside the job
    assert "tasks" in job_data
    task_dict = job_data["tasks"][0]
    assert task_dict["expected_answer"] == "[REDACTED UNTIL JOB COMPLETES]"
    
    # Also test tasks.json endpoint
    tasks_json_response = client.get(f"/api/jobs/{job_id}/tasks.json")
    assert tasks_json_response.status_code == 200
    tasks_json_data = tasks_json_response.json()
    # It should be a list
    assert tasks_json_data[0]["answer"] == "[REDACTED UNTIL JOB COMPLETES]"
    
    # Test artifact blocking
    artifact_response = client.get(f"/api/artifacts/{job_id}/tasks.json")
    assert artifact_response.status_code == 403
    assert "access control" in artifact_response.json()["detail"]
    
    # Now manually set the job to COMPLETED in the store
    job_obj = store.get_job(job_id)
    job_obj.status = JobStatus.COMPLETED
    with store._write_lock:
        store._conn.execute("UPDATE jobs SET status = 'COMPLETED' WHERE job_id = ?", (job_id,))
        store._conn.commit()
        
    # Re-fetch
    completed_response = client.get(f"/api/jobs/{job_id}")
    completed_task = completed_response.json()["tasks"][0]
    assert completed_task["expected_answer"] == "4"
    
    completed_tasks_json_response = client.get(f"/api/jobs/{job_id}/tasks.json")
    assert completed_tasks_json_response.json()[0]["answer"] == "4"
