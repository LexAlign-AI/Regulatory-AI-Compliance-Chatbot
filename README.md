# SLM Powered Regulatory (ISO 27001)

A 100% local RAG system: reads an organization's PDF/Word files, compares them with ISO 27001 requirements, and produces a compliance report with evidence and recommended fixes.

## Run
```bash
# 1) Ollama + models
ollama pull qwen2.5:14b      # or qwen2.5:7b on a weaker machine (change LLM_MODEL in regulatory_core.py)
ollama pull bge-m3

# 2) Dependencies
pip install -r requirements.txt

# 3) Create a test document
python make_sample.py

# 4) Dashboard (drag & drop)
streamlit run app.py

# or the step-by-step notebook
jupyter notebook SLM_Powered_Regulatory.ipynb
```

## Files
| File | Purpose |
|---|---|
| `regulatory_core.py` | All the logic (single source of truth) |
| `app.py` | Streamlit dashboard |
| `iso27001_requirements.json` | ISO 27001 requirements as short paraphrases (editable and extendable) |
| `build_notebook.py` | Generates the notebook from `regulatory_core.py` (after any edit: `python build_notebook.py`) |
| `make_sample.py` | Creates a sample company document |

## Notes
- **ISO copyright:** the JSON holds short paraphrased summaries, not the official text. If you have a licensed copy, upload it from the dashboard to ask questions about it.
- **Scanned PDFs:** need OCR first, e.g. `ocrmypdf input.pdf output.pdf`.
- **Future improvements:** a reranker (bge-reranker), the remaining Annex A controls (93 in total), accuracy evaluation on 30-50 human-labelled requirements, LoRA fine-tuning.
