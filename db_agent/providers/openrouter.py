from db_agent.providers.base import ProviderAuthError
from db_agent.providers.openai_compatible import OpenAICompatibleAdapter


class OpenRouterAdapter(OpenAICompatibleAdapter):
    name = "openrouter"

    def __init__(self, api_key: str | None, base_url: str | None = None, *, client=None):
        super().__init__(api_key, base_url or "https://openrouter.ai/api/v1", client=client)

    async def validate_key(self) -> bool:
        if not self.api_key:
            raise ProviderAuthError(self.name, "an API key is required")
        models = await self.list_models()
        if not models:
            raise ProviderAuthError(self.name, "the public model catalog returned no usable models")
        candidate = next((m.id for m in models if ":free" in m.id), models[0].id)
        result = await self.generate(
            prompt="ping",
            model=candidate,
            temperature=0,
            max_tokens=1,
        )
        return result is not None
