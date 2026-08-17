"""Authentication routes.

Included under the same ``/api/`` prefix as the rest of the API, so every path
here is byte-identical to what it was before the roles work — only the module
serving it moved.
"""
from django.urls import path

from .views import (
    AdminUserList, Login, Logout, ProfileView, SignupDoctor, SignupLaboratory,
    SignupPatient, SignupPharmacy, SignupRadiology,
)

app_name = "accounts"

urlpatterns = [
    # ---- Registration (one view class, one per public role) ----
    path("signup/patient/", SignupPatient.as_view(), name="signup-patient"),
    path("signup/doctor/", SignupDoctor.as_view(), name="signup-doctor"),
    path("signup/laboratory/", SignupLaboratory.as_view(), name="signup-laboratory"),
    path("signup/radiology/", SignupRadiology.as_view(), name="signup-radiology"),
    path("signup/pharmacy/", SignupPharmacy.as_view(), name="signup-pharmacy"),
    # There is deliberately no signup/admin/ — see roles.SELF_SERVICE_ROLES.

    # ---- Session ----
    path("login/", Login.as_view(), name="login"),
    path("logout/", Logout.as_view(), name="logout"),

    # ---- Profile (always the caller's own; never takes an id) ----
    path("profile/", ProfileView.as_view(), name="profile"),

    # ---- Administration (admin role only; enforced by the view) ----
    path("admin/users/", AdminUserList.as_view(), name="admin-users"),
]
