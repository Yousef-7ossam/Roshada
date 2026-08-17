"""Move existing appointments onto the unified provider/time-range schema.

Every existing appointment was a doctor consultation stored as ``doctor`` +
``date`` + ``time``. The unified engine addresses the provider by *user* and
stores the occupied period, so each row is translated once, here:

* ``provider``  <- the doctor's user account
* ``start_at``  <- date + time, interpreted in the project timezone
* ``end_at``    <- start + the legacy slot length

Nothing is dropped and nothing is invented: the same appointment, at the same
wall-clock moment, with the provider named the way the new engine names them.
"""
import datetime

from django.db import migrations
from django.utils import timezone

#: The old schema stored only a start. Thirty minutes is the slot length the
#: legacy booking form and the doctor dashboard both assumed, so it is the
#: duration these appointments have always effectively had.
LEGACY_SLOT_MINUTES = 30


def forwards(apps, schema_editor):
    Appointment = apps.get_model("appointments", "Appointment")

    migrated = 0
    orphaned = []
    for appointment in Appointment.objects.select_related("doctor").iterator():
        # Doctor.user is nullable — a seeded doctor with no login cannot be a
        # provider. Report rather than silently dropping the appointment.
        if appointment.doctor.user_id is None:
            orphaned.append(appointment.pk)
            continue

        start = timezone.make_aware(
            datetime.datetime.combine(appointment.date, appointment.time),
            timezone.get_default_timezone())
        appointment.provider_id = appointment.doctor.user_id
        appointment.start_at = start
        appointment.end_at = start + datetime.timedelta(minutes=LEGACY_SLOT_MINUTES)
        appointment.save(update_fields=["provider", "start_at", "end_at"])
        migrated += 1

    if orphaned:
        raise RuntimeError(
            f"Cannot migrate appointments {orphaned}: their doctor has no user "
            f"account, so there is no provider to point at. Attach a user to "
            f"those Doctor rows (or cancel the appointments) and re-run.")
    print(f"  migrated {migrated} appointment(s) to the unified schema")


def backwards(apps, schema_editor):
    """Rebuild the legacy columns from the range, so 0006 can be unapplied."""
    Appointment = apps.get_model("appointments", "Appointment")
    for appointment in Appointment.objects.iterator():
        if appointment.start_at is None:
            continue
        local = timezone.localtime(appointment.start_at)
        appointment.date = local.date()
        appointment.time = local.time()
        appointment.save(update_fields=["date", "time"])


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0006_scheduling_models"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
