"""Optional Apache Tika fallback, always executed through the sandbox port."""

from __future__ import annotations

from pathlib import Path

from proofcounsel_ingest.extractors.base import ExtractionContext, ExtractionResult
from proofcounsel_ingest.extractors.text import TextExtractor
from proofcounsel_ingest.models import SourceIdentity
from proofcounsel_ingest.security.sandbox import run_sandboxed


class TikaFallbackExtractor:
    def extract(
        self,
        source: Path,
        identity: SourceIdentity,
        context: ExtractionContext,
    ) -> ExtractionResult:
        if context.work_dir is None:
            raise RuntimeError("Apache Tika fallback requires an extraction work directory")
        tika = context.settings.resolved_binary("tika_binary", "tika")
        if tika:
            command = [tika, "--text", str(source.resolve())]
        elif context.settings.tika_jar is not None and context.settings.tika_jar.is_file():
            java = context.settings.resolved_binary("java_binary", "java")
            if not java:
                raise RuntimeError("Apache Tika JAR fallback requires Java")
            command = [java, "-jar", str(context.settings.tika_jar.resolve()), "--text", str(source.resolve())]
        else:
            raise RuntimeError("Apache Tika fallback requires PROOFCOUNSEL_TIKA_BINARY or PROOFCOUNSEL_TIKA_JAR")
        result = run_sandboxed(
            command,
            cwd=context.work_dir,
            profile=context.sandbox_profile,
        )
        text_path = context.work_dir / "tika-output.txt"
        text_path.write_text(result.stdout, encoding="utf-8")
        extraction = TextExtractor().extract(text_path, identity, context)
        extraction.metadata.update(
            {
                "extraction_method": "apache_tika_fallback",
                "original_mime": identity.detected_mime,
            }
        )
        extraction.blocks = [
            block.model_copy(
                update={
                    "confidence": 0.5,
                    "metadata": {
                        **block.metadata,
                        "queryable_evidence": False,
                        "locator_warning": "Tika text lines are not exact original-format locators",
                    },
                }
            )
            for block in extraction.blocks
        ]
        extraction.warnings.append("Tika fallback evidence requires source-locator review")
        return extraction
