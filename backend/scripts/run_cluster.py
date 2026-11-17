"""Start several independent backend instances on consecutive ports (development).

    python -m scripts.run_cluster 4 8001

Used to check horizontal scaling (NFR-08) on a machine without a load
balancer: scripts.loadtest spreads its requests over the instances. Each
instance gets a small connection pool so that together they stay under
PostgreSQL's default limit of 100 connections. In production the same thing is
done by running several containers behind the reverse proxy.
"""
import os
import subprocess
import sys

count = int(sys.argv[1]) if len(sys.argv) > 1 else 4
first_port = int(sys.argv[2]) if len(sys.argv) > 2 else 8001
env = {**os.environ, "BT_DB_POOL_SIZE": "10", "BT_DB_MAX_OVERFLOW": "10", "BT_WORKER_THREADS": "15"}
processes = [
    subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(first_port + i),
                      "--log-level", "warning"], env=env)
    for i in range(count)
]
print("Instances:", ",".join(f"http://127.0.0.1:{first_port + i}" for i in range(count)), flush=True)
try:
    for process in processes:
        process.wait()
finally:
    for process in processes:
        process.terminate()
