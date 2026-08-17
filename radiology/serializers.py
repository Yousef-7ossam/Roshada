"""Radiology serializers.

Output shapes are role-independent — the *filtering* is what differs, and that
happens in ``services`` querysets, not here. A serializer that decided what to
hide would be a second authorization layer, and the weaker of the two.
"""
from rest_framework import serializers

from appointments.serializers import ServiceSerializer, UserSerializer

from . import modalities
from .models import Examination, ImagingFile, ImagingOrder, RadiologyReport


class ImagingOrderSerializer(serializers.ModelSerializer):
    patient = UserSerializer(read_only=True)
    doctor = UserSerializer(read_only=True)
    modality_label = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    is_bookable = serializers.BooleanField(read_only=True)
    is_self_requested = serializers.BooleanField(read_only=True)

    class Meta:
        model = ImagingOrder
        fields = ["id", "patient", "doctor", "modality", "modality_label",
                  "study_name", "clinical_indication", "notes", "status",
                  "status_display", "is_bookable", "is_self_requested",
                  "cancellation_reason", "created_at", "updated_at"]


class ImagingOrderCreateSerializer(serializers.Serializer):
    patient_id = serializers.IntegerField(min_value=1)
    modality = serializers.ChoiceField(choices=modalities.ALL)
    study_name = serializers.CharField(max_length=200)
    clinical_indication = serializers.CharField(
        max_length=4000, required=False, allow_blank=True, default="")
    notes = serializers.CharField(max_length=2000, required=False,
                                  allow_blank=True, default="")


class ImagingFileSerializer(serializers.ModelSerializer):
    """File *metadata* only.

    Deliberately no URL: the bytes are reachable solely through the download
    endpoint, which checks the caller's relationship to the study first. Handing
    out ``file.url`` here would route around that.
    """
    uploaded_by = UserSerializer(read_only=True)

    class Meta:
        model = ImagingFile
        fields = ["id", "kind", "original_name", "content_type", "size_bytes",
                  "description", "modality_code", "study_uid", "series_uid",
                  "instance_uid", "uploaded_by", "uploaded_at"]


class RadiologyReportSerializer(serializers.ModelSerializer):
    author = UserSerializer(read_only=True)
    verified_by = UserSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)

    class Meta:
        model = RadiologyReport
        fields = ["id", "examination", "findings", "impression", "notes",
                  "status", "status_display", "report_date", "author",
                  "verified_by", "verified_at", "released_at",
                  "created_at", "updated_at"]


class ExaminationSerializer(serializers.ModelSerializer):
    order = ImagingOrderSerializer(read_only=True)
    service = ServiceSerializer(read_only=True)
    patient = UserSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display",
                                           read_only=True)
    center = serializers.SerializerMethodField()
    appointment = serializers.SerializerMethodField()
    files = ImagingFileSerializer(many=True, read_only=True)
    report = serializers.SerializerMethodField()

    class Meta:
        model = Examination
        fields = ["id", "status", "status_display", "order", "patient",
                  "center", "service", "appointment", "files", "report",
                  "checked_in_at", "started_at", "completed_at", "notes",
                  "created_at", "updated_at"]

    def get_center(self, obj):
        from appointments.serializers import provider_brief
        return provider_brief(obj.center)

    def get_appointment(self, obj):
        appointment = obj.appointment
        return {
            "id": appointment.id,
            "date": appointment.date.isoformat(),
            "time": appointment.time.strftime("%H:%M"),
            "end_time": appointment.end_time.strftime("%H:%M"),
            "status": appointment.status,
        }

    def get_report(self, obj):
        """The report, when the caller may see it.

        ``visible_reports`` is put in the context by the view from the same
        role-scoped queryset used everywhere else, so a patient never receives a
        draft through the examination payload.
        """
        report = getattr(obj, "report", None)
        if report is None:
            return None
        visible = self.context.get("visible_report_ids")
        if visible is not None and report.id not in visible:
            # Say that a report exists and is not ready — silence would read as
            # "nothing was written".
            return {"id": None, "status": "pending",
                    "status_display": "Being prepared"}
        return RadiologyReportSerializer(report).data


class ReportDraftSerializer(serializers.Serializer):
    findings = serializers.CharField(max_length=20000, required=False,
                                     allow_blank=True, default="")
    impression = serializers.CharField(max_length=8000, required=False,
                                       allow_blank=True, default="")
    notes = serializers.CharField(max_length=4000, required=False,
                                  allow_blank=True, default="")


class StatusChangeSerializer(serializers.Serializer):
    """A status transition request.

    The value is validated against the model's own choices, and the *legality*
    of the move is decided by the service layer — never by the caller.
    """
    status = serializers.CharField(max_length=30)


class BookOrderSerializer(serializers.Serializer):
    service_id = serializers.IntegerField(min_value=1)
    date = serializers.DateField()
    time = serializers.TimeField()
    reason = serializers.CharField(max_length=1000, required=False,
                                   allow_blank=True, default="")


class SelfBookSerializer(BookOrderSerializer):
    study_name = serializers.CharField(max_length=200, required=False,
                                       allow_blank=True, default="")
