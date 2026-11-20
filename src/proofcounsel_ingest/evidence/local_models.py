"""Firm-operated Ollama inference over IPv4 loopback with the existing review contracts."""

import json
import os
import re
import time

import httpx

from .providers import ProviderFailure, StructuredDecisionProviders, meter, trace_packet
from .store import dump


class LocalModels(StructuredDecisionProviders):
    runs_locally = True

    def __init__(self):
        super().__init__()
        self.model = os.getenv("EVIDENCE_OLLAMA_MODEL", "")
        self.digest = os.getenv("EVIDENCE_OLLAMA_DIGEST", "")
        port = int(os.getenv("EVIDENCE_OLLAMA_PORT", "11434"))
        if os.getenv("EVIDENCE_LOCAL_ONLY", "").lower() not in {"true", "1"}:
            raise ValueError("Set EVIDENCE_LOCAL_ONLY=true to use the local model.")
        if not re.fullmatch(r"[a-zA-Z0-9._/-]+:[a-zA-Z0-9._-]+", self.model) or not re.fullmatch(
            r"[0-9a-f]{64}", self.digest
        ):
            raise ValueError("Configure the installed Ollama model with an explicit tag and its SHA-256 digest.")
        if not 1 <= port <= 65535:
            raise ValueError("Invalid EVIDENCE_OLLAMA_PORT.")
        self.origin = f"http://127.0.0.1:{port}"

    @property
    def version(self):
        return ["ollama-local-v1", self.model, self.digest]

    def available(self, reason):
        # The daemon and model are checked immediately before each evidence dispatch.
        return True

    def structured(self, name, schema, instructions, data, deadline, max_tokens=1400):
        if name in {"notice_finding", "notice_exception_reading"}:
            instructions += (
                " Use gaps only for missing facts that prevent determination of the contractual minimum. "
                "If the minimum is established, return an empty gaps array. Put prerequisites for giving "
                "notice in conditions. Unknown actual service is handled separately."
            )
            ids = [p["id"] for p in data["passages"]]
            allowed_id = {"type": "string", "enum": ids}
            schema["properties"]["evidence_ids"]["items"] = allowed_id
            if name == "notice_finding":
                relation = schema["$defs"]["NoticeRelationship"]["properties"]
                for key in ("from_evidence_id", "to_evidence_id"):
                    relation[key] = allowed_id
                relation["execution_evidence_ids"]["items"] = allowed_id
                if len(ids) == 1:
                    # A relationship links distinct original passages.
                    schema["properties"]["relationships"] = {"const": []}
                    instructions += " There is only one passage. Return an empty relationships array."
                else:
                    instructions += " Only link two distinct supplied passage IDs. Use an empty array when no relationship is established."
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": dump(data)}],
            "format": schema,
            "stream": False,
            "think": False,
            "truncate": False,
            "shift": False,
            "options": {"temperature": 0, "seed": 7, "num_ctx": 8192, "num_predict": max_tokens},
        }
        body = dump(payload).encode()
        if len(body) > 48000:
            raise ProviderFailure("Complete evidence exceeds local model request budget.")
        with httpx.Client(base_url=self.origin, trust_env=False, follow_redirects=False) as client:

            def request(method, path, content=None, trace=None):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderFailure("Local model deadline exceeded.")
                try:
                    with client.stream(
                        method, path, content=content, headers={"Content-Type": "application/json"}, timeout=remaining
                    ) as response:
                        if trace is not None:
                            trace["http_status"] = response.status_code
                        if response.status_code != 200:
                            if trace is not None:
                                trace["outcome"] = "http_error"
                            raise ProviderFailure(f"Local model {path} returned HTTP {response.status_code}.")
                        raw = bytearray()
                        for chunk in response.iter_bytes(16384):
                            raw.extend(chunk)
                            if len(raw) > 512000 or time.monotonic() > deadline:
                                raise ProviderFailure("Local model response exceeded its budget.")
                        if trace is not None:
                            trace["outcome"] = "response_read"
                        return json.loads(raw)
                except httpx.HTTPError as exc:
                    raise ProviderFailure("Local model unavailable or timed out.") from exc

            if request("GET", "/api/status").get("cloud", {}).get("disabled") is not True:
                raise ProviderFailure("The local daemon must report cloud processing disabled.")
            models = request("GET", "/api/tags").get("models", [])
            if not any(
                m.get("name") == self.model
                and m.get("digest") == self.digest
                and m.get("details", {}).get("format") == "gguf"
                for m in models
            ):
                raise ProviderFailure("Approved local model weights unavailable or changed.")
            wire = json.loads(json.loads(body)["messages"][-1]["content"])
            trace = trace_packet(self.model, name, wire.get("state", wire))
            start = time.monotonic()
            try:
                response = request("POST", "/api/chat", body, trace)
            finally:
                if trace is not None:
                    trace["elapsed_ms"] = round((time.monotonic() - start) * 1000, 2)
        usage = meter.get()
        if usage is not None:
            usage["provider_usage"].append(
                {
                    "model": self.model,
                    "input_tokens": response.get("prompt_eval_count"),
                    "output_tokens": response.get("eval_count"),
                }
            )
        if (
            response.get("model") != self.model
            or response.get("done") is not True
            or response.get("done_reason") != "stop"
        ):
            raise ProviderFailure("Local model response incomplete or from an unexpected model.")
        message = response["message"]
        if message.get("role") != "assistant" or not isinstance(message.get("content"), str):
            raise ProviderFailure("Local model returned no structured answer.")
        return message["content"]
