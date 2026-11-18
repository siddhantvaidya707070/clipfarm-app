"""Create the first local owner after the fresh Docker installation starts."""
import getpass
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
password = getpass.getpass("Choose local owner password (12+ characters): ")
if len(password) < 12:
    raise SystemExit("Use at least 12 characters.")
if password != getpass.getpass("Confirm password: "):
    raise SystemExit("Passwords differ.")
login = {"email": "owner@clipfarm.local", "password": password}
compat = (root / "scripts/native_frontend_compat.sql").read_text()
subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "supoclip", "-d", "supoclip"], input=compat, text=True, cwd=root, check=True)
helper = (root / "scripts/native_login.cjs").read_text()
subprocess.run(["docker", "compose", "exec", "-T", "frontend", "node", "-e", helper], input=json.dumps(login), text=True, cwd=root, check=True)
print("Owner ready: owner@clipfarm.local at http://localhost:3107. Use your chosen password.")
