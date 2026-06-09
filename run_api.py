"""Run the FastAPI server.

Default port is 8888 (browser-safe). Port 6000 was avoided because browsers block it
as an "unsafe port" (ERR_UNSAFE_PORT, the X11 port). Override with `python run_api.py
<port>` or the PORT env var.
"""

import os
import sys

import uvicorn

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8888))
    uvicorn.run("meta_ad_library.api:app", host="127.0.0.1", port=port, reload=False)
