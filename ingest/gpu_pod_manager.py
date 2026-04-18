"""
GPU pod lifecycle stub.
Real RunPod API integration comes in a later brief.
All methods log intent only.
"""
import logging

logger = logging.getLogger(__name__)


def request_gpu_pod(task_name: str):
    """Log intent to spin up a GPU pod for the given task."""
    logger.info("[GPU_POD] Would request GPU pod for task: %s", task_name)


def release_gpu_pod(task_name: str = ""):
    """Log intent to terminate GPU pod after task completion."""
    logger.info("[GPU_POD] Would terminate GPU pod (after task: %s)", task_name or "unknown")


def gpu_available() -> bool:
    """Check whether a CUDA GPU is present in this process."""
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False
