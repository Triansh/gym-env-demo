import requests
import json
with open('single_task.json', 'r') as f:
    text = f.read()

for i in range(3):
    r = requests.post("http://localhost:8000/api/jobs", files={"file": ("single_task.json", text)}, data={"attempts": 2}, timeout=10)
    print(r.status_code, r.text)
