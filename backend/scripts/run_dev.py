"""
WorkGrid Local Development Server Launcher.
Configures WindowsSelectorEventLoopPolicy on Windows before uvicorn initializes,
enabling native asynchronous PostgreSQL LISTEN/NOTIFY without ProactorEventLoop warnings.
"""

import asyncio
import os
from pathlib import Path
import sys
import uvicorn

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Ensure backend directory is in sys.path and set as current working directory
backend_dir = Path(__file__).resolve().parent.parent
os.chdir(backend_dir)
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

if __name__ == "__main__":
    os.environ.setdefault("APP_ENV", "development")
    os.environ.setdefault("ENABLE_DEMO_LOGIN", "true")
    os.environ.setdefault("PYTHONPATH", str(backend_dir))
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_level="info", loop="asyncio")
