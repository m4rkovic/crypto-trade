# src/logger.py
import asyncio
import aiofiles
from aiocsv import AsyncWriter
import logging
import sys
import os  # <--- Added to handle folder creation
from typing import List, Any

class AsyncAuditLogger:
    """
    High-performance, non-blocking logger for trade auditing.
    Decouples disk I/O from the trading loop using an asyncio Queue.
    """
    def __init__(self, filepath: str):
        self.filepath = filepath
        self._queue = asyncio.Queue()
        self._worker_task = None

    async def start(self):
        """
        Initializes the log file (creates header if missing) and starts the background writer.
        """
        # --- FIX: Create the directory if it doesn't exist ---
        directory = os.path.dirname(self.filepath)
        if directory and not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
        # -----------------------------------------------------

        # Create file if it doesn't exist
        async with aiofiles.open(self.filepath, mode='a') as f:
            pass 
        self._worker_task = asyncio.create_task(self._writer_worker())

    async def log_trade(self, data: List[Any]):
        """
        Non-blocking call to add a trade record to the queue.
        """
        await self._queue.put(data)

    async def _writer_worker(self):
        """
        Background consumer that writes to disk.
        """
        while True:
            row = await self._queue.get()
            try:
                # 'a' mode appends to the file
                async with aiofiles.open(self.filepath, mode='a', newline='') as f:
                    writer = AsyncWriter(f, dialect='unix')
                    await writer.writerow(row)
            except Exception as e:
                # Fallback to stderr if disk I/O fails, don't crash the bot
                print(f"LOGGING FAILURE: {e}", file=sys.stderr)
            finally:
                self._queue.task_done()

def setup_console_logger(name: str, level: str):
    """
    Sets up the standard Python logger for console output.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(module)s | %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
    return logger