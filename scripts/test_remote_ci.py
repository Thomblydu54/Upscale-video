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
# GitHub tronque les longues annotations. Séparer le diagnostic pour conserver aussi
# les traces d'échec qui suivent la liste des tests, même sans accès au serveur de logs.
report = result.stdout[-18000:]
for i in range(0, len(report), 2800):
    text = report[i:i + 2800].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{'error' if result.returncode else 'notice'} title=Tests calcul distant {i // 2800 + 1}::{text}", flush=True)
sys.exit(result.returncode)
