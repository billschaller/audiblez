"""
Automatic GPU device detection for PyTorch.
"""

import torch


def init_and_get_gpu_device():
    """
    Detect available PyTorch GPU devices and set the default device to the first available.
    If no GPU is available, falls back to CPU.

    Returns:
        torch.device: The selected device (either GPU or CPU)
    """
    import torch

    if torch.cuda.is_available():
        print("CUDA GPU available")
        torch.set_default_device("cuda")
        return "cuda"
    elif torch.xpu.is_available():
        print("XPU GPU available")
        torch.set_default_device("xpu")
        return "xpu"
    elif torch.backends.mps.is_available():
        print("MPS GPU available")
        torch.set_default_device("mps")
        return "mps"
    else:
        print("GPU not available. Defaulting to CPU")
        return "cpu"
