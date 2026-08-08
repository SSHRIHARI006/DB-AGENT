from db_agent.providers.base import ProviderAuthError
from db_agent.providers.openai_compatible import OpenAICompatibleAdapter


class OllamaCloudAdapter(OpenAICompatibleAdapter):
    name = "ollama_cloud"
    include_all_models = True

    def __init__(self, api_key: str | None, base_url: str | None = None, *, client=None):
        super().__init__(api_key, base_url or "https://ollama.com/v1", client=client)

    async def validate_key(self) -> bool:
        await self.list_models()
        return True
