"""Verify the configured LLM provider, end to end, through the abstraction.

    python manage.py ai_check
    python manage.py ai_check --provider mock
    python manage.py ai_check --provider groq --prompt "ما هو نظام RAG؟"

This is the live connection test. It deliberately goes through
``services.ai.llm`` rather than talking to a provider directly, because the
thing worth verifying is not that Groq answers — it is that *Roshada's
abstraction reaches Groq and normalises what comes back*. A standalone script
against the vendor SDK would prove nothing about this application.

It prints the provider, model, latency and token usage. It never prints a
credential.
"""
import time

from django.core.management.base import BaseCommand

from appointments.services.ai import llm, providers

DEFAULT_PROMPT = "Explain what RAG is in one short paragraph."


def _console_safe(text):
    """Printable on a non-UTF-8 console.

    Only for display: what is *sent* to the provider is always the original
    string. Losing a character here must never mean sending a different
    question.
    """
    return str(text).encode("ascii", "replace").decode("ascii")


class Command(BaseCommand):
    help = "Send one prompt through the configured LLM provider."

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            help="Override AI_PROVIDER for this run (groq, mock, openai, "
                 "gemini, local).")
        parser.add_argument("--prompt", default=DEFAULT_PROMPT)
        parser.add_argument(
            "--status-only", action="store_true",
            help="Report configuration without spending a request.")

    def handle(self, *args, **options):
        import os

        if options["provider"]:
            os.environ["AI_PROVIDER"] = options["provider"]

        described = llm.describe()
        self.stdout.write("Provider configuration")
        self.stdout.write(f"  selected  : {described['provider'] or '(none)'}")
        self.stdout.write(f"  model     : {described['model'] or '(none)'}")
        self.stdout.write(f"  label     : {described['label']}")
        self.stdout.write(
            f"  configured: {', '.join(described['configured_providers']) or '(none)'}")
        self.stdout.write(f"  registered: {', '.join(sorted(providers.PROVIDERS))}")

        if not described["enabled"]:
            # A configuration problem, reported as one — not a stack trace.
            self.stdout.write(self.style.WARNING(
                "\nNo provider is configured. Set AI_PROVIDER and the matching "
                "API key in .env (see .env.example), or use --provider mock to "
                "test offline."))
            return

        if options["status_only"]:
            return

        prompt = options["prompt"]
        # Everything written to stdout is ASCII-safed: this command exists to
        # test Arabic prompts, and a Windows console is cp1252 — echoing the
        # prompt verbatim crashed the command before it ever reached Groq.
        self.stdout.write(f"\nSending: {_console_safe(prompt)[:80]}")
        started = time.monotonic()
        try:
            response = llm.complete(prompt, temperature=0.1)
        except llm.LLMUnavailable as exc:
            self.stdout.write(self.style.ERROR(f"\nNot configured: {exc}"))
            return
        except llm.LLMFailed as exc:
            # The provider name only. The cause is logged with its taxonomy
            # class; it is not repeated here because provider text carries
            # endpoints and model ids.
            self.stdout.write(self.style.ERROR(
                f"\nThe provider '{exc}' did not answer. See the application "
                f"log for the failure category."))
            return

        elapsed = int((time.monotonic() - started) * 1000)
        # ASCII-safe: this runs on consoles that are not UTF-8.
        text = _console_safe(response.text)

        self.stdout.write(self.style.SUCCESS(
            f"\nAnswered by {response.provider} · {response.model} "
            f"in {elapsed} ms"))
        if response.usage.known:
            self.stdout.write(f"Tokens: {response.usage.total_tokens}")
        self.stdout.write(f"Characters: {len(response.text)}")
        self.stdout.write("\n--- answer ---")
        self.stdout.write(text[:1500])
