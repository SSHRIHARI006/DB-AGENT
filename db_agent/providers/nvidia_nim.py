from db_agent.providers.openai_compatible import OpenAICompatibleAdapter


class NvidiaNimAdapter(OpenAICompatibleAdapter):
    name = "nvidia_nim"

    def __init__(self, api_key: str | None, base_url: str | None = None, *, client=None):
        super().__init__(
            api_key,
            base_url or "https://integrate.api.nvidia.com/v1",
            client=client,
        )
