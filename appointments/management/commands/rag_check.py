"""Answer a question through the full RAG → LLM path, and report how it went.

    python manage.py rag_check "What is hypertension?"
    python manage.py rag_check "ما هو ارتفاع ضغط الدم؟"
    python manage.py rag_check --retrieval-only "What is HbA1c?"
    python manage.py rag_check --provider mock "What is hypertension?"

The companion to ``ai_check``: that one proves the provider answers, this one
proves the answer was *grounded* — how many passages were retrieved, which of
them the answer actually cited, whether any citation was fabricated, and where
the time went.

It prints no credential. Output is ASCII-safed for consoles that are not UTF-8,
which is why an Arabic question can be checked from a Windows terminal at all;
what is *sent* is always the original string.
"""
import os
import time

from django.core.management.base import BaseCommand


def _safe(text):
    """Printable on a non-UTF-8 console. Display only."""
    return str(text).encode("ascii", "replace").decode("ascii")


class Command(BaseCommand):
    help = "Answer one question through RAG and report the grounding."

    def add_arguments(self, parser):
        parser.add_argument("question")
        parser.add_argument(
            "--provider",
            help="Override AI_PROVIDER for this run (groq, mock, ...).")
        parser.add_argument(
            "--retrieval-only", action="store_true",
            help="Retrieve and stop. Spends no model request.")
        parser.add_argument("--top-k", type=int)
        parser.add_argument("--min-score", type=float)

    def handle(self, *args, **options):
        from knowledge import retrieval as knowledge_retrieval
        from knowledge.rag import service as rag

        if options["provider"]:
            os.environ["AI_PROVIDER"] = options["provider"]

        question = options["question"]
        coverage = knowledge_retrieval.coverage()
        self.stdout.write(
            f"Corpus: {coverage['documents']} document(s), "
            f"{coverage['sources']} source(s), "
            f"languages {coverage['languages'] or '(none)'}")
        self.stdout.write(f"Question: {_safe(question)}")

        if options["retrieval_only"]:
            started = time.monotonic()
            try:
                hits = knowledge_retrieval.search(
                    question,
                    top_k=options["top_k"] or 5,
                    min_score=options["min_score"])
            except knowledge_retrieval.RetrievalUnavailable as exc:
                self.stdout.write(self.style.ERROR(
                    f"Retrieval unavailable: {_safe(exc)}"))
                return
            elapsed = int((time.monotonic() - started) * 1000)
            self.stdout.write(f"\n{len(hits)} passage(s) in {elapsed} ms")
            for index, hit in enumerate(hits, start=1):
                self.stdout.write(
                    f"  [{index}] score {hit['score']:.4f}  "
                    f"{_safe(hit['reference'])[:90]}")
            return

        result = rag.answer(question, top_k=options["top_k"],
                            min_score=options["min_score"])

        self.stdout.write("")
        style = self.style.WARNING if result.degraded else self.style.SUCCESS
        self.stdout.write(style(
            f"{'DEGRADED (' + result.reason + ')' if result.degraded else 'GROUNDED'}"
            f" · provider {result.provider or '(none)'}"
            f" · model {result.model or '(none)'}"
            f" · prompt {result.prompt_version or '(none)'}"))
        self.stdout.write(
            f"Latency: {result.latency_ms} ms total "
            f"(retrieval {result.retrieval_ms} ms, generation {result.llm_ms} ms)")
        signals = result.retrieval or {}
        self.stdout.write(
            f"Retrieval: {signals.get('matches', 0)} match(es), "
            f"{signals.get('sources_used', 0)} sent, "
            f"top score {signals.get('top_score', 0)}, "
            f"strength {signals.get('match_strength', 'none')}")
        self.stdout.write(f"Language detected: {result.language}")
        self.stdout.write(
            f"Fabricated citations: {result.fabricated_citations or 'none'}")

        for source in result.sources:
            mark = "cited" if source.get("cited") else "     "
            self.stdout.write(
                f"  [{source['n']}] {mark}  {_safe(source['reference'])[:88]}")
        for warning in result.warnings:
            self.stdout.write(self.style.WARNING(f"  ! {_safe(warning)}"))

        self.stdout.write("\n--- answer ---")
        self.stdout.write(_safe(result.answer)[:2000])
