"""Retire the doctor-only columns and enforce the new invariants.

Runs after 0007 has given every row a provider and a time range, so the
not-null alterations below have nothing left to fail on.

The final operation is the one that matters: an ``EXCLUDE`` index that makes
overlapping scheduled appointments for one provider *impossible to store*,
rather than merely unlikely. It needs ``btree_gist`` for the equality operator
on the provider column, which is why the extension is created first.
"""
import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
from django.conf import settings
from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0007_backfill_appointment_schedule"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Must precede the ExclusionConstraint: without btree_gist there is no
        # gist operator class for `provider_id =`, and the index cannot be built.
        BtreeGistExtension(),
        migrations.AlterModelOptions(
            name="appointment",
            options={"ordering": ["-start_at"]},
        ),
        migrations.RemoveConstraint(
            model_name="appointment",
            name="unique_active_doctor_timeslot",
        ),
        migrations.RemoveIndex(
            model_name="appointment",
            name="appointment_patient_83aa7a_idx",
        ),
        migrations.RemoveIndex(
            model_name="appointment",
            name="appointment_doctor__e7d63f_idx",
        ),
        migrations.RemoveField(
            model_name="appointment",
            name="date",
        ),
        migrations.RemoveField(
            model_name="appointment",
            name="doctor",
        ),
        migrations.RemoveField(
            model_name="appointment",
            name="time",
        ),
        migrations.AlterField(
            model_name="appointment",
            name="end_at",
            field=models.DateTimeField(),
        ),
        migrations.AlterField(
            model_name="appointment",
            name="provider",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="provider_appointments",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="appointment",
            name="start_at",
            field=models.DateTimeField(),
        ),
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(
                fields=["patient", "start_at"], name="appointment_patient_4b5ed5_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(
                fields=["provider", "start_at"], name="appointment_provide_486882_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="appointment",
            index=models.Index(
                fields=["service"], name="appointment_service_9ce97f_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=models.CheckConstraint(
                condition=models.Q(("end_at__gt", models.F("start_at"))),
                name="appointment_ends_after_it_starts",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(("status", "scheduled")),
                expressions=[
                    ("provider", "="),
                    (
                        models.Func(
                            models.F("start_at"),
                            models.F("end_at"),
                            models.Value("[)"),
                            function="tstzrange",
                            output_field=django.contrib.postgres.fields.ranges.DateTimeRangeField(),
                        ),
                        "&&",
                    ),
                ],
                name="no_overlapping_provider_appointments",
            ),
        ),
    ]
