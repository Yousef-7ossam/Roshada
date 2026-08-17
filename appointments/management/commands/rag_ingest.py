"""Ingest documents into the retrieval corpus.

    python manage.py rag_ingest <path> [--metadata k=v ...] [--force]
    python manage.py rag_ingest --stats
    python manage.py rag_ingest --search "how is hypertension treated?"

A command rather than an endpoint: ingestion is an operator action over files on
disk, it is slow and billable, and exposing it over HTTP would need an
authorisation story that belongs with the corpus itself.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from appointments.services import rag


class Command(BaseCommand):
    help = "Ingest documents into the RAG corpus, or inspect/query it."

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?",
                            help="A supported file, or a directory of them.")
        parser.add_argument("--metadata", nargs="*", default=[], metavar="KEY=VALUE",
                            help="Facets attached to every ingested document.")
        parser.add_argument("--force", action="store_true",
                            help="Re-embed even if the content is unchanged.")
        parser.add_argument("--no-recursive", action="store_true",
                            help="Do not descend into subdirectories.")
        parser.add_argument("--remove", metavar="SOURCE",
                            help="Delete a document and its chunks by source.")
        parser.add_argument("--stats", action="store_true",
                            help="Show what is indexed, and in which spaces.")
        parser.add_argument("--search", metavar="QUERY",
                            help="Run a retrieval query (no LLM involved).")
        parser.add_argument("--top-k", type=int, default=5)

    # -- helpers ---------------------------------------------------------
    def _parse_metadata(self, pairs):
        metadata = {}
        for pair in pairs:
            if "=" not in pair:
                raise CommandError(f"--metadata expects KEY=VALUE, got '{pair}'")
            key, _, value = pair.partition("=")
            metadata[key.strip()] = value.strip()
        return metadata

    def _show_embedder(self):
        info = rag.embedder_info()
        self.stdout.write(
            f"embedder: {info['embedder']}:{info['model']} "
            f"[{info['dimension']}d] semantic={info['semantic']}")
        if not info["semantic"]:
            self.stdout.write(self.style.WARNING(
                "  offline lexical embeddings — matches shared words, not "
                "meaning. Set RAG_EMBEDDING_API_KEY for semantic retrieval."))

    # -- entry point -----------------------------------------------------
    def handle(self, *args, **options):
        if options["stats"]:
            return self._handle_stats()
        if options["remove"]:
            return self._handle_remove(options["remove"])
        if options["search"]:
            return self._handle_search(options["search"], options["top_k"])
        if not options["path"]:
            raise CommandError(
                "Give a path to ingest, or use --stats / --search / --remove.")
        return self._handle_ingest(options)

    def _handle_stats(self):
        self._show_embedder()
        summary = rag.stats()
        self.stdout.write(f"documents: {summary['documents']}  "
                          f"chunks: {summary['chunks']}")
        for space in summary["spaces"]:
            self.stdout.write(
                f"  {space['embedder']}:{space['embedding_model']} "
                f"[{space['dimension']}d] -> {space['chunks']} chunks")
        if not summary["spaces"]:
            self.stdout.write("  (nothing indexed yet)")

    def _handle_remove(self, source):
        if rag.remove(source):
            self.stdout.write(self.style.SUCCESS(f"Removed '{source}'."))
        else:
            self.stdout.write(self.style.WARNING(f"No document '{source}'."))

    def _handle_search(self, query, top_k):
        self._show_embedder()
        try:
            result = rag.retrieve_context(query, top_k=top_k)
        except rag.IndexSpaceMismatch as exc:
            raise CommandError(str(exc))

        if not result.found:
            self.stdout.write(self.style.WARNING("No matching passages."))
            return

        self.stdout.write(f"\n{len(result.sources)} passage(s):\n")
        for source in result.sources:
            self.stdout.write(f"  [{source['n']}] {source['reference']}  "
                              f"(score {source['score']})")
        self.stdout.write("\n--- context handed to the LLM ---")
        self.stdout.write(result.text)

    def _handle_ingest(self, options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"No such path: {path}")

        self._show_embedder()
        metadata = self._parse_metadata(options["metadata"])

        try:
            if path.is_dir():
                results, failures = rag.ingest_directory(
                    path, metadata=metadata, force=options["force"],
                    recursive=not options["no_recursive"])
            else:
                results, failures = [rag.ingest_file(
                    path, metadata=metadata, force=options["force"])], []
        except (rag.UnsupportedDocument, rag.EmbeddingError) as exc:
            raise CommandError(str(exc))

        indexed = sum(r.chunks for r in results if not r.skipped)
        skipped = sum(1 for r in results if r.skipped)
        for result in results:
            state = "skipped (unchanged)" if result.skipped else f"{result.chunks} chunks"
            self.stdout.write(f"  {result.document.title}: {state}")

        for source, error in failures:
            self.stdout.write(self.style.ERROR(f"  {source}: {error}"))

        self.stdout.write(self.style.SUCCESS(
            f"\n{len(results) - skipped} document(s) indexed "
            f"({indexed} chunks), {skipped} unchanged, {len(failures)} failed."))
