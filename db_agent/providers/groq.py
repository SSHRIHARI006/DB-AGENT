from db_agent.providers.openai_compatible import OpenAICompatibleAdapter


class GroqAdapter(OpenAICompatibleAdapter):
    name = "groq"

    def __init__(self, api_key: str | None, base_url: str | None = None, *, client=None):
        super().__init__(api_key, base_url or "https://api.groq.com/openai/v1", client=client)
