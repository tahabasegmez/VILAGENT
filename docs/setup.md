# VILAGENT Setup Guide

## Initial Setup

### 1. Environment Setup

#### Using Conda (Recommended)

```powershell
# Create conda environment
conda create -n vilagent python=3.10
conda activate vilagent

# Install dependencies
pip install -r requirements.txt
```

#### Using Virtual Environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment Variables

1. Copy the example environment file:
```bash
copy .env.example .env
```

2. Edit `.env` and add your API keys:
```
GROQ_API_KEY=your_actual_key_here
OPENAI_API_KEY=your_actual_key_here
```

### 3. Model Download

Download required models using the setup script:

```bash
python scripts/download_models.py
```

Or manually download and place models in:
- `model_data/omniparser/` for OmniParser models
- `model_data/yolo/` for YOLO models

### 4. Directory Structure

Ensure the following directories exist:
- `data/chroma_db/` - Vector database
- `data/logs/` - Application logs
- `model_data/` - Model files (gitignored)

## Running the Application

### Start Vision Server

```bash
python -m src.servers.vision.vision_server
```

### Start Control Server

```bash
python -m src.servers.control.control_server
```

### Run Client

```bash
python -m src.clients.client_llama
```

## Troubleshooting

### Common Issues



