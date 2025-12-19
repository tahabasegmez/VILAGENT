# VILAGENT Setup Guide

## Initial Setup

### 1. Environment Setup

```bash
cd VILAGENT
conda env create -f conda_vilagent_env.yml
```
### 2. Environment Variables

Edit `.env` and add your API keys:
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

## Troubleshooting

### Common Issues



