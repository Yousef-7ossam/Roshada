"""Rebuild the Knowledge Base index.

Roshada has no task queue — no Celery, no Redis, no scheduler — and the brief
is explicit that one should not be introduced for this. So bulk processing is a
management command an operator runs, and everything else is a synchronous
service call.

    python manage.py kb_reindex --all
    python manage.py kb_reindex --document 12
    python manage.py kb_reindex --failed --dry-run

Re-indexing is the supported way to change what is stored: the engine replaces
a document's chunks wholesale, so a partial rebuild cannot leave old passages
retrievable beside new ones.
"""
from django.core.management.base import BaseCommand, CommandError

from appointments.models import Document
from knowledge import services


class Command(BaseCommand):
    help = "Re-run the ingestion pipeline for Knowledge Base documents."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--all", action="store_true",
                           help="Every non-archived document.")
        group.add_argument("--failed", action="store_true",
                           help="Only documents whose last run failed.")
        group.add_argument("--document", type=int,
                           help="One document, by id.")
        parser.add_argument("--dry-run", action="store_true",
                            help="List what would be processed.")

    def handle(self, *args, **options):
        # The command runs as an operator on the host, which is already the
        # highest privilege there is; it borrows an administrator identity so
        # the service layer's own authorization is exercised rather than
        # bypassed.
        admin = _an_administrator()
        if admin is None:
            raise CommandError(
                "No administrator account exists. Create one with "
                "`manage.py createsuperuser` before re-indexing.")

        if options["document"]:
            queryset = Document.objects.filter(pk=options["document"])
            if not queryset.exists():
                raise CommandError(f"No document {options['document']}.")
        elif options["failed"]:
            queryset = Document.objects.filter(status=Document.FAILED)
        else:
            queryset = Document.objects.exclude(status=Document.ARCHIVED)

        documents = list(queryset.select_related("knowledge_source"))
        if options["dry_run"]:
            for document in documents:
                self.stdout.write(
                    f"  would reindex #{document.id} {document.title} "
                    f"v{document.version} [{document.status}]")
            self.stdout.write(f"{len(documents)} document(s).")
            return

        succeeded, failed = 0, []
        for document in documents:
            try:
                services.reindex_document(admin, document.id)
                succeeded += 1
                self.stdout.write(f"  indexed #{document.id} {document.title}")
            except Exception as exc:                        # noqa: BLE001
                # One unparseable document must not abandon the corpus.
                failed.append((document.id, services.safe_error(exc)))
                self.stderr.write(f"  FAILED #{document.id}: {failed[-1][1]}")

        self.stdout.write(self.style.SUCCESS(
            f"Re-indexed {succeeded} document(s); {len(failed)} failed."))


def _an_administrator():
    from accounts import roles
    from django.contrib.auth.models import User
    return (User.objects.filter(account__role=roles.ADMIN).first()
            or User.objects.filter(is_superuser=True).first())
