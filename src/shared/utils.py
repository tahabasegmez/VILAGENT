"""
Utility functions for VILAGENT.

This module contains common utility functions for logging, image conversion, etc.
"""

import logging
import sys
import yaml
from pathlib import Path
from typing import Optional, Union, Dict, Any
from PIL import Image
import io

from src.shared.vilagent_config import LOGS_PATH, LOG_LEVEL, LOG_FILE, PROMPTS_DIR


def setup_logger(name: str = "vilagent", log_file: Optional[Path] = None) -> logging.Logger:
    """
    Set up a logger with file and console handlers.
    
    Args:
        name: Logger name.
        log_file: Optional path to log file. If None, uses default from config.
    
    Returns:
        logging.Logger: Configured logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    
    # Avoid adding handlers multiple times
    if logger.handlers:
        return logger
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler
    if log_file is None:
        log_file = LOG_FILE
    
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger


def image_to_bytes(image: Image.Image, format: str = "JPEG", quality: int = 80) -> bytes:
    """
    Convert PIL Image to bytes.
    
    Args:
        image: PIL Image object.
        format: Image format (JPEG, PNG, etc.).
        quality: Image quality (for JPEG, 1-100).
    
    Returns:
        bytes: Image as bytes.
    """
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format=format, quality=quality)
    img_byte_arr.seek(0)
    return img_byte_arr.read()


def bytes_to_image(image_bytes: bytes) -> Image.Image:
    """
    Convert bytes to PIL Image.
    
    Args:
        image_bytes: Image data as bytes.
    
    Returns:
        Image.Image: PIL Image object.
    """
    return Image.open(io.BytesIO(image_bytes))


def format_coordinates(x1: int, y1: int, x2: int, y2: int) -> str:
    """
    Format bounding box coordinates as string.
    
    Args:
        x1, y1: Top-left coordinates.
        x2, y2: Bottom-right coordinates.
    
    Returns:
        str: Formatted coordinate string.
    """
    return f"({x1}, {y1}) to ({x2}, {y2})"


def validate_path(path: Union[str, Path], must_exist: bool = False) -> Path:
    """
    Validate and convert path to Path object.
    
    Args:
        path: Path as string or Path object.
        must_exist: If True, raises error if path doesn't exist.
    
    Returns:
        Path: Path object.
    
    Raises:
        FileNotFoundError: If must_exist is True and path doesn't exist.
    """
    path_obj = Path(path)
    
    if must_exist and not path_obj.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    
    return path_obj


def load_system_prompt(prompt_file: Optional[Path] = None) -> str:
    """
    Load system prompt from YAML file and convert to string format.
    
    Args:
        prompt_file: Optional path to system.yaml. If None, uses default from config.
    
    Returns:
        str: Formatted system prompt string.
    
    Raises:
        FileNotFoundError: If prompt file doesn't exist.
    """
    if prompt_file is None:
        prompt_file = PROMPTS_DIR / "system.yaml"
    
    prompt_file = validate_path(prompt_file, must_exist=True)
    
    with open(prompt_file, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    
    # Build prompt string from YAML structure
    prompt_parts = []
    
    # Role
    if 'role' in data:
        prompt_parts.append(data['role'])
    
    # Capabilities
    if 'capabilities' in data and data['capabilities']:
        prompt_parts.append("\n\nCapabilities:")
        for cap in data['capabilities']:
            prompt_parts.append(f"- {cap}")
    
    # Critical Rules
    if 'critical_rules' in data and data['critical_rules']:
        prompt_parts.append("\n\nCRITICAL RULES:")
        for i, rule in enumerate(data['critical_rules'], 1):
            prompt_parts.append(f"{i}. {rule}")
    
    # Error Handling
    if 'error_handling' in data and data['error_handling']:
        prompt_parts.append("\n\nError Handling:")
        for rule in data['error_handling']:
            prompt_parts.append(f"- {rule}")
    
    # Preferences
    if 'preferences' in data and data['preferences']:
        prompt_parts.append("\n\nPreferences:")
        for pref in data['preferences']:
            prompt_parts.append(f"- {pref}")
    
    return "\n".join(prompt_parts)


def load_tasks_prompt(tasks_file: Optional[Path] = None) -> Dict[str, Any]:
    """
    Load tasks prompt from YAML file.
    
    Args:
        tasks_file: Optional path to tasks.yaml. If None, uses default from config.
    
    Returns:
        dict: Tasks data structure.
    
    Raises:
        FileNotFoundError: If tasks file doesn't exist.
    """
    if tasks_file is None:
        tasks_file = PROMPTS_DIR / "tasks.yaml"
    
    tasks_file = validate_path(tasks_file, must_exist=True)
    
    with open(tasks_file, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    
    return data

