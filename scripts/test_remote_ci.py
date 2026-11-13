"""Rapport de tests consultable dans les annotations, même sans accès au serveur de logs CI."""
import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
env = dict(os.environ, PYTHONIOENCODING="utf-8")
result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
                        cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        encoding="utf-8", errors="replace")
print(result.stdout, flush=True)
# Échapper les commandes de workflow, pas seulement les retours à la ligne.
text = result.stdout[-18000:].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
print(f"::{'error' if result.returncode else 'notice'} title=Tests calcul distant::{text}", flush=True)
sys.exit(result.returncode)
