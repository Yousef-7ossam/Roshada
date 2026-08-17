import importlib

from django.apps import AppConfig


class RadiologyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'radiology'
    verbose_name = 'Radiology'

    def ready(self):
        """Attach the radiology workflow to the appointment engine.

        The engine must not import this module — the dependency runs one way,
        radiology -> appointments — so radiology registers itself here instead.
        Explicit callbacks rather than Django signals: the registry is a plain
        list that a test can inspect, and the wiring is greppable.
        """
        # Imported for its side effect: importing the module registers its
        # callbacks with the engine.
        importlib.import_module("radiology.hooks")
