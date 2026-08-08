from db_agent.providers.base import ModelInfo


STATIC_MODELS: dict[str, list[ModelInfo]] = {
    "gemini": [
        ModelInfo("gemini-2.5-flash", "Gemini 2.5 Flash", 1_000_000, True, True),
        ModelInfo("gemini-2.5-pro", "Gemini 2.5 Pro", 1_000_000, True, True),
        ModelInfo("gemini-2.0-flash", "Gemini 2.0 Flash", 1_000_000, True, False),
    ],
    "openrouter": [
        ModelInfo("openai/gpt-oss-20b:free", "GPT OSS 20B Free", None, True, None),
        ModelInfo("meta-llama/llama-3.3-70b-instruct", "Llama 3.3 70B", 131072, True, None),
        ModelInfo("google/gemini-2.5-flash", "Gemini 2.5 Flash", 1_000_000, True, True),
    ],
    "ollama_cloud": [
        ModelInfo("gpt-oss:20b", "GPT OSS 20B", 131072, True, None),
        ModelInfo("qwen3-coder:30b", "Qwen3 Coder 30B", 131072, True, True),
        ModelInfo("llama3.3", "Llama 3.3", 131072, True, None),
    ],
    "nvidia_nim": [
        ModelInfo("meta/llama-3.1-8b-instruct", "Llama 3.1 8B Instruct", 131072, True, None),
        ModelInfo("meta/llama-3.3-70b-instruct", "Llama 3.3 70B Instruct", 131072, True, None),
        ModelInfo("mistralai/mixtral-8x22b-instruct-v0.1", "Mixtral 8x22B", 65536, True, None),
    ],
    "groq": [
        ModelInfo("llama-3.3-70b-versatile", "Llama 3.3 70B Versatile", 131072, True, None),
        ModelInfo("llama-3.1-8b-instant", "Llama 3.1 8B Instant", 131072, True, None),
        ModelInfo("qwen/qwen3-32b", "Qwen3 32B", 131072, True, True),
    ],
}
