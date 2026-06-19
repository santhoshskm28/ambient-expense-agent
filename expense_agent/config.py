import os

# Configs for the ambient-expense-agent
AUTO_APPROVE_THRESHOLD = 100.0
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-3.1-flash-lite")
