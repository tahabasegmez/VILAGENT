# VILAGENT

A vision-language agent system that combines computer vision and language models to interact with graphical user interfaces.

## Features

- **Vision Server**: Captures and analyzes screen content
- **Control Server**: Handles mouse and keyboard interactions
- **Model Handlers**: Supports OmniParser and YOLO models for UI element detection
- **MCP Integration**: Model Context Protocol server implementation

## Installation

### Prerequisites

- Python 3.8+
- Windows 10/11 (for control features)
- Conda or virtual environment

### Setup

1. Clone the repository:
```bash
git clone <repository-url>
cd VILAGENT
```

2. Create and activate a conda environment:
```bash
conda create -n vilagent python=3.10
conda activate vilagent
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up environment variables:
```bash
copy .env.example .env
# Edit .env and add your API keys
```

5. Download models (if needed):
```bash
python scripts/download_models.py
```

## Usage

### Starting the Vision Server

```bash
python -m src.servers.vision.vision_server
```

### Starting the Control Server

```bash
python -m src.servers.control.control_server
```

### Running a Client

```bash
python -m src.clients.client_llama
```

## Project Structure

See `CODING_STANDARTS.md` for detailed project structure and coding standards.

## Documentation

- [Architecture](docs/architecture.md)
- [Setup Guide](docs/setup.md)
- [Coding Standards](CODING_STANDARTS.md)

## License


