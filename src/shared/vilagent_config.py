"""
VILAGENT project constants and path configurations.

This module contains all project-wide constants and path settings.
All path configurations should be done here.
"""

import os
from pathlib import Path

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Data directories
DATA_DIR = PROJECT_ROOT / "data"
CHROMA_DB_PATH = DATA_DIR / "chroma_db"
LOGS_PATH = DATA_DIR / "logs"

# Model data directories
MODEL_DATA_PATH = PROJECT_ROOT / "model_data"
OMNI_MODEL_PATH = MODEL_DATA_PATH / "omniparser"
YOLO_MODEL_PATH = MODEL_DATA_PATH / "yolo"

# Source directories
SRC_DIR = PROJECT_ROOT / "src"
PROMPTS_DIR = SRC_DIR / "prompts"

# Environment variables with defaults
def get_env_var(key: str, default: str = "") -> str:
    """
    Get environment variable with optional default.
    
    Args:
        key: Environment variable name.
        default: Default value if not found.
    
    Returns:
        str: Environment variable value or default.
    """
    return os.getenv(key, default)

# API Keys
GROQ_API_KEY = get_env_var("GROQ_API_KEY")
OPENAI_API_KEY = get_env_var("OPENAI_API_KEY")

# Server Configuration
CONTROL_SERVER_PORT = int(get_env_var("CONTROL_SERVER_PORT", "8000"))
VISION_SERVER_PORT = int(get_env_var("VISION_SERVER_PORT", "8001"))

# Logging Configuration
LOG_LEVEL = get_env_var("LOG_LEVEL", "INFO")
LOG_FILE = LOGS_PATH / "vilagent.log"

# Ensure directories exist
def ensure_directories():
    """Create necessary directories if they don't exist."""
    directories = [
        DATA_DIR,
        CHROMA_DB_PATH,
        LOGS_PATH,
        MODEL_DATA_PATH,
        OMNI_MODEL_PATH,
        YOLO_MODEL_PATH,
    ]
    
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

# Initialize directories on import
ensure_directories()

