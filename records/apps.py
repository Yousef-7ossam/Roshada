import importlib

from django.apps import AppConfig


class RecordsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'records'
    verbose_name = 'Medical Records'

    def ready(self):
        """Register the timeline sources this package owns.

        Appointments and consultations live in the scheduling
        engine, which must not import a clinical module — so their contributors
        are registered from here. Radiology and pharmacy register their own, so
        the aggregation layer never has to be edited when a domain is added.
        """
        # Imported for its side effect: importing the module registers its
        # contributors with the timeline registry.
        importlib.import_module("records.sources")
