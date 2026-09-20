import re
from pathlib import Path

files_to_patch = [
    "tests/test_m2_backend.py",
    "tests/test_mock_rollouts.py",
    "tests/test_sqlite_store.py"
]

for file_path in files_to_patch:
    path = Path(file_path)
    if path.exists():
        content = path.read_text()
        
        # Remove await in front of store methods
        content = re.sub(r'await\s+(store|test_store|new_store|job_store)\.', r'\1.', content)
        
        # In test_m2_backend.py: test_store.db.get_job -> test_store.get_job
        content = re.sub(r'(store|test_store|new_store|job_store)\.db\.', r'\1.', content)
        
        # Remove load_from_disk
        content = re.sub(r'^\s*new_store\.load_from_disk\(\).*$', '', content, flags=re.MULTILINE)
        
        path.write_text(content)
