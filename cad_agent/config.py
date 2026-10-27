from dataclasses import dataclass, replace
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent


def local_env() -> dict[str, str]:
    path = ROOT / ".env"
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values | dict(os.environ)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    protocol: str = "anthropic"
    provider: str = "custom"
    model_profile: str = "default"
    reasoning_effort: str = "low"
    context_length: int | None = None
    agent_max_rounds: int = 8
    agent_max_tool_calls: int = 16
    agent_timeout_seconds: int = 180
    execution_policy: str = "v2"
    subscription_budget_policy: str = "adaptive_v1"
    stable_subscription_prefix: bool = True
    model_context_codec: bool = True
    read_context_optimization: bool = True
    llm_query_intent: bool = True
    source_record_workflow: bool = True
    source_record_max_records: int = 64
    source_record_max_pages: int = 128
    source_record_packet_tokens: int = 12000
    siwc_max_concurrency: int = 1
    siwc_queue_timeout_seconds: float = 600
    max_upload_bytes: int = 32 * 1024 * 1024
    oda_base_url: str = "http://192.168.1.146:18080"
    oda_api_key: str = ""
    oda_timeout_seconds: float = 600
    oda_poll_seconds: float = 1
    oda_max_result_bytes: int = 256 * 1024 * 1024
    embedding_model_dir: Path = ROOT / ".data/models/bge-small-zh-v1.5"

    def model_catalog(self):
        """Server-owned endpoints only; credentials never go to the browser."""
        env = local_env()
        profiles = {"default": self}
        profiles["siwc_sol"] = replace(self, api_base="", api_key="", model="gpt-6.1-sol",
            provider="responses_subscription", protocol="responses", model_profile="siwc_sol", context_length=None, reasoning_effort="low")
        profiles["siwc_sol_high"] = replace(profiles["siwc_sol"], model_profile="siwc_sol_high", reasoning_effort="high")
        profiles["siwc_luna_high"] = replace(profiles["siwc_sol"], model_profile="siwc_luna_high", model="gpt-6-luna", reasoning_effort="high")
        from cad_agent.runtimes.budget_policy import policy_for
        for key in ("siwc_sol", "siwc_sol_high", "siwc_luna_high"):
            policy = policy_for(profiles[key])
            if policy:
                profiles[key] = replace(profiles[key], agent_max_rounds=policy.rounds_ceiling,
                    agent_max_tool_calls=policy.tool_calls_ceiling,
                    agent_timeout_seconds=policy.elapsed_ceiling_seconds)
        for profile, model in (("codex_sol_56", "gpt-5.6-sol"), ("codex_sol", "gpt-6.1-sol"), ("codex_astra", "gpt-6-astra")):
            profiles[profile] = replace(self, api_base="", api_key="", model=model,
                provider="codex_subscription", protocol="codex", model_profile=profile,
                context_length=None)
        for profile, model in (("api_sol", "gpt-6.1-sol"), ("api_astra", "gpt-6-astra")):
            profiles[profile] = replace(
                self,
                api_base=env.get("CAD_GPT_API_BASE_URL", "").rstrip("/"),
                api_key=env.get("CAD_GPT_API_KEY", ""),
                model=model,
                protocol="openai",
                provider="custom",
                model_profile=profile,
                context_length=None,
            )
        for tier in ("large", "medium", "small"):
            prefix = "CAD_QWEN_" + tier.upper() + "_"
            profiles["qwen_" + tier] = replace(
                self,
                api_base=env.get(prefix + "BASE_URL", "").rstrip("/"),
                api_key=env.get(prefix + "API_KEY", ""),
                model=env.get(prefix + "MODEL", ""),
                protocol="openai",
                provider="qwen",
                model_profile="qwen_" + tier,
                context_length=(
                    int(env[prefix + "CONTEXT_LENGTH"])
                    if env.get(prefix + "CONTEXT_LENGTH")
                    else None
                ),
            )
        return profiles

    @classmethod
    def load(cls):
        e = local_env()
        deepseek = e.get("CAD_AGENT_PROVIDER", "deepseek") == "deepseek"
        return cls(
            data_dir=Path(e.get("CAD_AGENT_DATA_DIR", str(ROOT / ".data"))).resolve(),
            llm_query_intent=e.get("CAD_LLM_QUERY_INTENT", "1") == "1",
            execution_policy=e.get("CAD_EXECUTION_POLICY", "v2"),
            subscription_budget_policy=e.get("CAD_SUBSCRIPTION_BUDGET_POLICY", "adaptive_v1"),
            stable_subscription_prefix=e.get("CAD_STABLE_SUBSCRIPTION_PREFIX", "1") == "1",
            model_context_codec=e.get("CAD_MODEL_CONTEXT_CODEC", "1") == "1",
            read_context_optimization=e.get("CAD_READ_CONTEXT_OPTIMIZATION", "1") == "1",
            source_record_workflow=e.get("CAD_SOURCE_RECORD_WORKFLOW", "1") == "1",
            siwc_max_concurrency=int(e.get("CAD_SIWC_MAX_CONCURRENCY", "1")),
            siwc_queue_timeout_seconds=float(e.get("CAD_SIWC_QUEUE_TIMEOUT_SECONDS", "600")),
            agent_timeout_seconds=int(e.get("CAD_AGENT_TIMEOUT_SECONDS","180")),
            agent_max_tool_calls=int(e.get("CAD_AGENT_MAX_TOOL_CALLS","16")),
            agent_max_rounds=int(e.get("CAD_AGENT_MAX_ROUNDS","8")),
            oda_base_url=e.get("CAD_ODA_BASE_URL", "http://192.168.1.146:18080").rstrip("/"),
            oda_api_key=e.get("CAD_ODA_API_KEY", ""),
            oda_timeout_seconds=float(e.get("CAD_ODA_TIMEOUT_SECONDS", "600")),
            api_base=(
                "https://api.deepseek.com"
                if deepseek
                else e.get("CAD_AGENT_API_BASE", "")
            ).rstrip("/"),
            # Never send an old gateway credential to a newly selected provider.
            api_key=(
                e.get("DEEPSEEK_API_KEY", "")
                if deepseek
                else e.get("CAD_AGENT_API_KEY", "")
            ),
            model="deepseek-flash" if deepseek else e.get("CAD_AGENT_MODEL", ""),
            protocol=(
                "openai" if deepseek else e.get("CAD_AGENT_API_PROTOCOL", "anthropic")
            ),
            provider="deepseek" if deepseek else "custom",
            embedding_model_dir=Path(
                e.get(
                    "CAD_EMBEDDING_MODEL_DIR",
                    str(ROOT / ".data/models/bge-small-zh-v1.5"),
                )
            ).resolve(),
        )
