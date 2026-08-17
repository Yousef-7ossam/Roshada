from django.apps import AppConfig


class KnowledgeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'knowledge'
    verbose_name = 'Medical Knowledge Base'

    # No ``ready()`` hook, deliberately. This module registers no timeline
    # source, no dashboard contributor and no notification producer: general
    # medical reference material is not a patient's record and must not appear
    # in one. Kept true by having nothing to disable.
