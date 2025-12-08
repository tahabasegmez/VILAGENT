"""
Data models and dataclasses for VILAGENT.

This module contains all data structures used throughout the project.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any


@dataclass
class UIElement:
    """Represents a UI element detected on screen."""
    
    element_type: str  # e.g., "button", "icon", "text"
    label: str
    coordinates: Tuple[int, int, int, int]  # (x1, y1, x2, y2)
    confidence: float
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class ScreenCapture:
    """Represents a screen capture with metadata."""
    
    image_path: str
    timestamp: float
    resolution: Tuple[int, int]  # (width, height)
    detected_elements: List[UIElement]


@dataclass
class ToolResult:
    """Result from a tool execution."""
    
    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class WindowInfo:
    """Information about a window."""
    
    title: str
    process_name: str
    hwnd: Optional[int] = None
    coordinates: Optional[Tuple[int, int, int, int]] = None  # (left, top, right, bottom)
    is_visible: bool = True


@dataclass
class ModelConfig:
    """Base configuration for ML models."""
    
    model_path: str
    confidence_threshold: float = 0.5
    device: str = "cpu"  # "cpu" or "cuda"
    batch_size: int = 1


@dataclass
class OmniResult:
    """Result from OmniParser analysis."""
    
    icons: List[UIElement]
    text_elements: List[UIElement]
    buttons: List[UIElement]
    raw_output: Optional[str] = None

