"""Give every pre-existing user a role record.

Before ``UserAccount`` a role was inferred from which profile row existed. That
inference is applied once, here, so the new authoritative column agrees with how
every existing account already behaved — nobody's role changes as a result of
this migration.

The one addition is ADMIN: superusers and staff previously resolved to "patient"
through the fail-open default, which is why the admin portal could not exist.

Depends on ``appointments`` so ``Doctor`` and ``PatientProfile`` are guaranteed
to be present when this runs.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model("auth", "User")
    UserAccount = apps.get_model("accounts", "UserAccount")
    Doctor = apps.get_model("appointments", "Doctor")
    PatientProfile = apps.get_model("appointments", "PatientProfile")

    # Doctor.user is nullable (seeded doctors exist with no login), so filter
    # the NULLs out rather than putting None in the id set.
    doctor_ids = set(
        Doctor.objects.exclude(user__isnull=True).values_list("user_id", flat=True))
    patient_ids = set(PatientProfile.objects.values_list("user_id", flat=True))
    already = set(UserAccount.objects.values_list("user_id", flat=True))

    rows = []
    for user in User.objects.exclude(id__in=already).iterator():
        if user.is_superuser or user.is_staff:
            role = "admin"
        elif user.id in doctor_ids:
            role = "doctor"
        elif user.id in patient_ids:
            role = "patient"
        else:
            # Matches the previous fallback exactly: a user with no profile
            # behaved as a patient, and every patient capability is scoped to
            # their own data.
            role = "patient"
        rows.append(UserAccount(user_id=user.id, role=role, status="active"))

    UserAccount.objects.bulk_create(rows, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
        ("appointments", "0005_doctor_clinic_doctor_license_number_doctor_phone_and_more"),
    ]

    operations = [
        # Reversing drops the table anyway (0001), so there is nothing useful to
        # undo here and a destructive reverse would only be a foot-gun.
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
