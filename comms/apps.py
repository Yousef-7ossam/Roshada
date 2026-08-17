import importlib

from django.apps import AppConfig


class CommsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'comms'
    verbose_name = 'Communication & Notifications'

    def ready(self):
        """Subscribe to the events the scheduling engine owns.

        Appointments live in ``appointments``, which sits below
        every clinical module and must not import one — so this module
        registers callbacks with its registries instead. The clinical modules
        (radiology, pharmacy) call ``comms.notifications`` directly from their
        own services.
        """
        # Imported for its side effect: importing the module registers its
        # callbacks with the engine.
        importlib.import_module("comms.hooks")
