import json
import os
import aiofiles
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class MemoryService:
    @staticmethod
    async def load_memory(file_path: str) -> Dict[str, Any]:
        """Asynchronously load JSON memory file"""
        if not os.path.exists(file_path):
            logger.info(f"Memory file {file_path} does not exist, initializing new memory.")
            return {"past_inferences": []}
            
        try:
            async with aiofiles.open(file_path, mode='r', encoding='utf-8') as f:
                content = await f.read()
                return json.loads(content)
        except Exception as e:
            logger.error(f"Failed to load memory file: {e}")
            return {"past_inferences": []}

    @staticmethod
    async def save_memory(file_path: str, data: Dict[str, Any]):
        """Asynchronously save memory to local"""
        try:
            # Ensure that the directory exists
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            async with aiofiles.open(file_path, mode='w', encoding='utf-8') as f:
                content = json.dumps(data, ensure_ascii=False, indent=2)
                await f.write(content)
            logger.info(f"The memory has been successfully saved to {file_path}")
        except Exception as e:
            logger.error(f"Failed to save memory: {e}")