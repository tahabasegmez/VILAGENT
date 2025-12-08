"""
OmniParser model configuration.

Configuration settings for OmniParser model operations.
"""

import os
from pathlib import Path
from typing import Dict, Any

from src.shared.vilagent_config import OMNI_MODEL_PATH


class OmniConfig:
    """Configuration for OmniParser model."""

    def __init__(self):
        """Initialize OmniParser configuration."""
        self.model_path = OMNI_MODEL_PATH
        self.icon_detect_model = os.path.join(self.model_path, "icon_detect", "model.pt")
        self.icon_caption_model = os.path.join(
            self.model_path, "icon_caption_florence", "model.safetensors"
        )
        
        # Model parameters
        self.confidence_threshold = 0.5
        self.max_detections = 100
        
        # Image processing
        self.image_size = (1280, 1280)
        self.image_quality = 80

    def get_config(self) -> Dict[str, Any]:
        """
        Get configuration as dictionary.
        
        Returns:
            dict: Configuration parameters.
        """
        return {
            "model_path": self.model_path,
            "icon_detect_model": self.icon_detect_model,
            "icon_caption_model": self.icon_caption_model,
            "confidence_threshold": self.confidence_threshold,
            "max_detections": self.max_detections,
            "image_size": self.image_size,
            "image_quality": self.image_quality,
        }

    def validate_paths(self) -> bool:
        """
        Validate that model paths exist.
        
        Returns:
            bool: True if all required paths exist, False otherwise.
        """
        paths_to_check = [
            self.model_path,
            self.icon_detect_model,
            self.icon_caption_model,
        ]
        
        for path in paths_to_check:
            if not Path(path).exists():
                return False
        return True

