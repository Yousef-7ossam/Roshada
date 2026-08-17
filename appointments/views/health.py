"""Health / readiness endpoint for load balancers and ops.

Unauthenticated and un-throttled so probes can poll freely.

Two distinct questions are answered on one endpoint, because conflating them was
ambiguous: is the process alive, and is every dependency actually usable?

* ``GET /api/health/``           -> liveness. Always 200 while the process serves
  requests.
* ``GET /api/health/?ready=1``   -> readiness. 200 only when the database is
  reachable, else 503. Point readiness probes and deployment gates here.
"""
from django.db import connection
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthCheck(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []          # never rate-limit a health probe

    def _database_ok(self):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return True
        except Exception:
            return False

    def get(self, request):
        payload = {"status": "ok"}

        # Liveness (default): the process is up, regardless of dependencies.
        if request.query_params.get("ready") not in ("1", "true", "yes"):
            return Response(payload, status=status.HTTP_200_OK)

        # Readiness: everything the app needs must actually be usable.
        database_ok = self._database_ok()
        payload.update({
            "status": "ready" if database_ok else "degraded",
            "database": "ok" if database_ok else "unavailable",
        })
        return Response(
            payload,
            status=(status.HTTP_200_OK if database_ok
                else status.HTTP_503_SERVICE_UNAVAILABLE),
        )
