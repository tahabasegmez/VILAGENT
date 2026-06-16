# VILAGENT Colab Runtimes

These notebooks expose Colab-hosted models to VILAGENT through pyngrok.

- `vilagent_text_vllm_pyngrok.ipynb`: starts an OpenAI-compatible vLLM server for the text planner.
- `vilagent_uitars_pyngrok.ipynb`: starts a FastAPI target-resolution service for UI-TARS-style vision inference.

## UI-TARS Drive Cache

`vilagent_uitars_pyngrok.ipynb` caches the vision model in Google Drive:

```text
My Drive/models/UI-TARS-1.5-7B
```

In Colab this path is mounted as:

```text
/content/drive/MyDrive/models/UI-TARS-1.5-7B
```

First run:

- mounts Google Drive;
- downloads `ByteDance-Seed/UI-TARS-1.5-7B` into the Drive cache;
- copies the Drive cache to `/content/models/UI-TARS-1.5-7B` for faster local runtime access.

Later runs:

- skips the download when the Drive cache already has files;
- copies the cached model from Drive to local runtime disk again.

Override the Hugging Face repo if needed:

```python
%env VILAGENT_UITARS_HF_MODEL_ID=your-org/your-model
```

After a notebook prints its public URL, copy the suggested values into `.env` and `config.yaml`, then open the VILAGENT Operator and run:

- `Validate config`
- `Text model health`
- `UI-TARS health`

Keep API keys short-lived. Do not paste private keys into shared notebooks.
