"""Raise reminder notifications for appointments starting soon.

Deliberately a command rather than a background job. Roshada has no scheduler,
no Celery and no task queue, and this is not the place to invent one — so this is
something an operator runs (from cron, Task Scheduler, or by hand) if they want
reminders, and nothing runs automatically otherwise.

Safe to run as often as you like: a partial unique constraint means the same
patient cannot be reminded twice about the same appointment.

    python manage.py send_appointment_reminders --within-hours 24
"""
from django.core.management.base import BaseCommand

from comms import notifications


class Command(BaseCommand):
    help = "Notify patients about appointments starting within N hours."

    def add_arguments(self, parser):
        parser.add_argument(
            "--within-hours", type=int, default=24,
            help="How far ahead to look. Default 24.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what would be sent without writing anything.")

    def handle(self, *args, **options):
        hours = options["within_hours"]
        if options["dry_run"]:
            import datetime

            from django.utils import timezone

            from appointments.models import Appointment
            from comms.models import Notification
            from comms import types

            now = timezone.now()
            due = Appointment.objects.filter(
                status=Appointment.SCHEDULED, start_at__gte=now,
                start_at__lte=now + datetime.timedelta(hours=hours))
            already = set(Notification.objects.filter(
                type=types.APPOINTMENT_REMINDER,
                source="appointments.Appointment"
            ).values_list("reference", flat=True))
            pending = [a for a in due if a.id not in already]
            self.stdout.write(
                f"{due.count()} appointment(s) in the next {hours}h; "
                f"{len(pending)} would be reminded "
                f"({len(already & {a.id for a in due})} already were).")
            return

        raised = notifications.create_due_reminders(within_hours=hours)
        self.stdout.write(self.style.SUCCESS(
            f"Raised {len(raised)} reminder(s) for the next {hours}h."))
