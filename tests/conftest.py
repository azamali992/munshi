import os

os.environ.setdefault("MUNSHI_NO_AUTOAPP", "1")
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
os.environ.setdefault("LLM_PROVIDER", "stub")
