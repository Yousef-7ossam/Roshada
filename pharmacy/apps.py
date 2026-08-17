import importlib

from django.apps import AppConfig


class PharmacyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pharmacy'
    verbose_name = 'Pharmacy'

    def ready(self):
        """Attach the pharmacy workflow to the platform's shared services.

        Same direction as radiology: the dependency runs pharmacy -> engine,
        never the reverse. The dashboard module knows nothing about this app;
        this app registers a contributor with it at startup.
        """
        # Imported for its side effect: importing the module registers its
        # callbacks.
        importlib.import_module("pharmacy.hooks")
