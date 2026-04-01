import sys
from loguru import logger
import uvicorn

# Remove default loguru handler and replace with a clean one
logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}", level="DEBUG")

from maxmanager.server import app


def run():
    uvicorn.run(app, host="0.0.0.0", port=8765, log_config=None)


if __name__ == "__main__":
    run()
