"""Docker executor stub — reserved for future implementation."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class DockerExecutor:
    """Execute a task inside a Docker container.

    MVP: logs a warning and returns a placeholder so the pipeline isn't broken.
    """

    async def run(
        self,
        task: dict[str, Any],
        execution: dict[str, Any],
        work_dir: str,
        log_dir: str,
    ) -> dict[str, Any]:
        image = execution.get("image", "python:3.11")
        command = execution.get("command", "")

        logger.warning(
            "Docker executor is a stub (task %s). "
            "Would run image=%r command=%r",
            task["task_id"], image, command,
        )

        return {
            "success": False,
            "error": "Docker executor not yet implemented (MVP stub)",
            "traceback": "",
        }
