"""Dashboard summary endpoint.

Returns real, role-scoped aggregates. Metrics the product has no data source for
are reported as null and listed in ``unsupported_metrics`` so the UI shows an
honest empty state instead of inventing a clinical-looking number.
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..services import dashboard


class DashboardSummary(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(dashboard.summary_for(request.user),
                        status=status.HTTP_200_OK)
