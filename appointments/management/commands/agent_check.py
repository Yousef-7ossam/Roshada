"""Ask the assistant something as a real user, and report what it did.

    python manage.py agent_check demo_patient "مين الدكاترة المتاحين؟"
    python manage.py agent_check demo_doctor "who is booked with me tomorrow?"
    python manage.py agent_check demo_patient "hi" --tools-only

The third check command, and the one that needs a *user*: ``ai_check`` proves the
provider answers and ``rag_check`` proves the answer was grounded, but a tool
call only means anything on behalf of somebody. This runs the real pipeline as
the named account, so what it exercises is exactly what the API serves.

It reports which tools ran. That is the whole point: an assistant that says "I
don't have access to your appointments" while ``get_patient_appointments`` never
appears here is not a model problem, it is a wiring problem.

Nothing is written without a confirmation, here as anywhere else — the gate is
in the service, not in the caller.

Output is ASCII-safed for consoles that are not UTF-8; what is *sent* is always
the original string.
"""
import os
import time

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


def _safe(text):
    """Printable on a non-UTF-8 console. Display only."""
    return str(text).encode("ascii", "replace").decode("ascii")


class Command(BaseCommand):
    help = "Ask the assistant a question as a given user and show the tools used."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("message", nargs="?", default="")
        parser.add_argument("--provider",
                            help="Override AI_PROVIDER for this run.")
        parser.add_argument(
            "--tools-only", action="store_true",
            help="List the tools this user's role holds and stop. Costs "
                 "nothing and calls no model.")

    def handle(self, *args, **options):
        from appointments.services.ai import agent, pipeline, tools

        if options["provider"]:
            os.environ["AI_PROVIDER"] = options["provider"]

        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError(
                f"No user called '{options['username']}'.") from None

        role = tools.role_of(user)
        held = tools.names_for(role)
        self.stdout.write(f"User    : {user.username}")
        self.stdout.write(f"Role    : {role}   (read from the account record)")
        self.stdout.write(f"Tools   : {', '.join(held) or '(none for this role)'}")
        self.stdout.write(f"Enabled : {agent.is_available(role)}")

        if options["tools_only"]:
            return
        if not options["message"]:
            raise CommandError("Give a message, or pass --tools-only.")

        message = options["message"]
        self.stdout.write(f"\nAsking  : {_safe(message)}")

        started = time.monotonic()
        result = pipeline.ask(user, message)
        elapsed = int((time.monotonic() - started) * 1000)

        style = self.style.WARNING if result.degraded else self.style.SUCCESS
        self.stdout.write(style(
            f"\n{'DEGRADED' if result.degraded else 'ANSWERED'}"
            f" · provider {result.provider or '(none)'}"
            f" · model {result.model or '(none)'}"
            f" · {elapsed} ms"))
        self.stdout.write(
            f"Tools used : {', '.join(result.tools_used) or '(none)'}")
        self.stdout.write(f"Grounded   : {result.grounded}")
        self.stdout.write(f"Citations  : {len(result.citations)}")
        self.stdout.write(
            f"Record used: {', '.join(s['kind'] for s in result.sources) or '(none)'}")
        for warning in result.warnings:
            self.stdout.write(self.style.WARNING(f"  ! {_safe(warning)}"))

        self.stdout.write("\n--- reply ---")
        self.stdout.write(_safe(result.reply)[:2000])
