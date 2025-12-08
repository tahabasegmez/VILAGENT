# VILAGENT Architecture

## Overview

VILAGENT is a vision-language agent system that enables AI agents to interact with graphical user interfaces through computer vision and natural language understanding.

## System Components

### 1. Clients (`src/clients/`)

The clients are the orchestration layer that starts and coordinates the agent:

- **client_groq.py**: Groq-based agent client
- **client_vision.py**: Vision-based agent client
- **client_llama.py**: Llama-based agent client

### 2. Model Handlers (`src/model_handlers/`)

Model handlers act as a bridge between the application and ML models:

- **omni_handler.py**: Manages OmniParser model operations
- **yolo_handler.py**: Manages YOLO model operations
- **model_configs/**: Configuration files for different models

### 3. Servers (`src/servers/`)

MCP (Model Context Protocol) servers that provide tools to the agent:

#### Control Server (`control/`)
- **control_server.py**: Main control server
- **tools/**: Mouse and keyboard interaction tools

#### Vision Server (`vision/`)
- **vision_server.py**: Main vision server
- **tools/**: Image processing and UI element detection tools

### 4. Shared Components (`src/shared/`)

Common utilities and configurations:

- **vilagent_config.py**: Project constants and path configurations
- **dataclasses.py**: Data models and structures
- **utils.py**: Logging, image conversion, and other utilities

### 5. Prompts (`src/prompts/`)

LLM system instructions and task definitions:

- **system.yaml**: Main system prompt ("You are a computer agent...")
- **tasks.yaml**: Specific task definitions

## Data Flow

1. Client receives user request
2. Vision server captures screen and analyzes UI elements
3. Model handlers process images using ML models
4. Control server executes actions (mouse/keyboard)
5. Results are returned to the client

## Model Data

Large model files (>100MB) are stored in `model_data/`:
- `omniparser/`: OmniParser model files (.pt, .onnx)
- `yolo/`: YOLO model files (.pt)

## Database

Vector database for embeddings stored in `data/chroma_db/`.

## Logs

Application logs stored in `data/logs/`.

