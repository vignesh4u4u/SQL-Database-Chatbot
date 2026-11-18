import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import json_repair
from openai import AsyncOpenAI

import config


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    api_key_env: str
    base_url_env: Optional[str] = None
    default_base_url: Optional[str] = None
    model_env: Optional[str] = None
    default_model: Optional[str] = None
    supports_json_schema: bool = True
    timeout_seconds: float = 30.0
    api_key_fallback: Optional[str] = None


PROVIDER_REGISTRY: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="groq",
        api_key_env="GROQ_API_KEY",
        default_base_url="https://api.groq.com/openai/v1",
        model_env="GROQ_MODEL",
        default_model="openai/gpt-oss-120b",
        supports_json_schema=True,
        timeout_seconds=25.0,
    ),
    ProviderSpec(
        name="kovaion_gemma",
        api_key_env="KOVAION_AI_API_KEY",
        base_url_env="KOVAION_AI_GEMMA",
        model_env="KOVAION_AI_GEMMA_MODEL",
        supports_json_schema=True,
        timeout_seconds=45.0,
        api_key_fallback="not-needed",
    ),
    ProviderSpec(
        name="kovaion_qwen3",
        api_key_env="KOVAION_AI_API_KEY",
        base_url_env="KOVAION_AI_QWEN3",
        model_env="KOVAION_AI_QWEN3_MODEL",
        supports_json_schema=False,
        timeout_seconds=45.0,
        api_key_fallback="not-needed",
    ),
    ProviderSpec(
        name="openai",
        api_key_env="OPENAI_API_KEY",
        base_url_env="OPENAI_BASE_URL",
        default_base_url="https://api.openai.com/v1",
        model_env="OPENAI_MODEL",
        default_model="gpt-4o-mini",
        supports_json_schema=True,
        timeout_seconds=30.0,
    ),
    ProviderSpec(
        name="gemini",
        api_key_env="GEMINI_API_KEY",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model_env="GEMINI_MODEL",
        default_model="gemini-2.5-flash",
        supports_json_schema=True,
        timeout_seconds=30.0,
    ),
)


def parse_json_content(raw: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I).strip()
    fm = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.S | re.I)
    if fm:
        cleaned = fm.group(1).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        obj = json_repair.loads(cleaned)
    if not isinstance(obj, dict):
        raise ValueError("LLM output is not a JSON object.")
    return obj


def _parse_string_list(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
    if "," in text:
        return [item.strip().strip("\"'") for item in text.strip("[]").split(",") if item.strip().strip("\"'")]
    single = text.strip("\"'")
    return [single] if single else []


class OpenAISDKBackend:
    def __init__(self, spec: ProviderSpec, api_keys: list[str], base_url: str, preferred_model: Optional[str]) -> None:
        self.name = spec.name
        self.supports_json_schema = spec.supports_json_schema
        cleaned_url = base_url.rstrip("/")
        self.clients: list[AsyncOpenAI] = [
            AsyncOpenAI(
                api_key=key,
                base_url=cleaned_url,
                timeout=spec.timeout_seconds,
                max_retries=0,
            )
            for key in api_keys
        ]
        self._model = preferred_model

    async def resolve_model(self, client: AsyncOpenAI, logger: logging.Logger) -> str:
        if self._model:
            return self._model
        models = await client.models.list()
        if not models.data:
            raise RuntimeError(f"No models available on {self.name}")
        self._model = models.data[0].id
        logger.info("Resolved %s model: %s", self.name, self._model)
        return self._model


class LLMRouter:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.backends = self._load_backends()
        if not self.backends:
            raise ValueError("No OpenAI SDK LLM providers configured in environment.")
        self.logger.info(
            "LLM provider order: %s",
            [f"{b.name} ({len(b.clients)} key{'s' if len(b.clients) > 1 else ''})" for b in self.backends],
        )

    def _load_backends(self) -> list[OpenAISDKBackend]:
        config.load_settings()
        settings = config.get_all_settings()
        spec_map = {spec.name: spec for spec in PROVIDER_REGISTRY}

        main_name = str(settings.get("MAIN_LLM_PROVIDER", "")).strip()
        fallback_raw = settings.get("FALLBACK_LLM_PROVIDERS", [])
        order_raw = settings.get("LLM_PROVIDER_ORDER", [])

        requested_names: list[str] = []
        if main_name or fallback_raw:
            if main_name:
                requested_names.append(main_name)
            if isinstance(fallback_raw, list):
                requested_names.extend([str(x).strip() for x in fallback_raw if str(x).strip()])
            else:
                requested_names.extend(_parse_string_list(str(fallback_raw)))
        elif order_raw:
            if isinstance(order_raw, list):
                requested_names.extend([str(x).strip() for x in order_raw if str(x).strip()])
            else:
                requested_names.extend(_parse_string_list(str(order_raw)))

        ordered_specs: list[ProviderSpec] = []
        seen_names: set[str] = set()
        for name in requested_names:
            if name in spec_map and name not in seen_names:
                ordered_specs.append(spec_map[name])
                seen_names.add(name)
        for spec in PROVIDER_REGISTRY:
            if spec.name not in seen_names:
                ordered_specs.append(spec)
                seen_names.add(spec.name)

        active: list[OpenAISDKBackend] = []
        for spec in ordered_specs:
            base_url = (
                str(settings.get(spec.base_url_env, "")).strip() if spec.base_url_env else ""
            ) or (spec.default_base_url or "")

            raw_keys = settings.get(spec.api_key_env, "")
            if isinstance(raw_keys, list):
                api_keys = [str(k).strip() for k in raw_keys if str(k).strip()]
            else:
                api_keys = _parse_string_list(str(raw_keys))

            if not api_keys and spec.api_key_fallback:
                api_keys = [spec.api_key_fallback]

            if not base_url or not api_keys:
                continue

            model = (
                str(settings.get(spec.model_env, "")).strip() if spec.model_env else ""
            ) or spec.default_model
            active.append(OpenAISDKBackend(spec, api_keys, base_url, model))
        return active

    async def close(self) -> None:
        for backend in self.backends:
            for client in backend.clients:
                await client.close()

    async def json_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        json_schema: dict[str, Any],
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        errors: list[str] = []
        for b_idx, backend in enumerate(self.backends):
            for k_idx, client in enumerate(backend.clients):
                key_label = f"{backend.name}[key#{k_idx + 1}]"
                try:
                    model_id = await backend.resolve_model(client, self.logger)
                    sys_msg = (
                        system_prompt
                        if backend.supports_json_schema
                        else system_prompt + "\nReturn ONLY valid JSON matching schema:\n" + json.dumps(json_schema)
                    )
                    resp_fmt: dict[str, Any] = (
                        {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": json_schema}}
                        if backend.supports_json_schema
                        else {"type": "json_object"}
                    )
                    comp = await client.chat.completions.create(
                        model=model_id,
                        messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_prompt}],
                        temperature=0.0,
                        max_tokens=max_tokens,
                        response_format=resp_fmt,
                    )
                    return parse_json_content(comp.choices[0].message.content or "")
                except Exception as exc:
                    errors.append(f"{key_label}: {exc}")
                    if k_idx < len(backend.clients) - 1:
                        self.logger.warning(
                            "LLM %s failed (%s); switching to next API key (%s[key#%d])",
                            key_label,
                            exc,
                            backend.name,
                            k_idx + 2,
                        )
                    elif b_idx < len(self.backends) - 1:
                        self.logger.warning(
                            "LLM %s failed (%s); falling back to next provider %s",
                            key_label,
                            exc,
                            self.backends[b_idx + 1].name,
                        )
                    else:
                        self.logger.error("LLM %s failed (%s); exhausted all keys and backends", key_label, exc)

        raise RuntimeError("All LLM backends and keys failed: " + " | ".join(errors))