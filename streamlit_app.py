import datetime
import html

from dotenv import load_dotenv
load_dotenv()  # load .env BEFORE importing modules that read env at import time

import streamlit as st
import plotly.graph_objects as go

from shared import ai
from shared import history as chat_history
from shared.reminders import ensure_reminders
from shared.api import (
    api_request, handle_login, handle_logout, handle_registration,
)
from shared.ui import (
    show_api_error, DISCLAIMER, init_session_state,
    clean_html,
)
from shared import theme
from shared.theme import (
    inject_theme, page_header, stat_card, badge,
    doctor_card, appointment_card, empty_state, avatar_initials,
)
# The role vocabulary, shared with the backend rather than restated here — a
# second copy of the role names is a second copy that can disagree. This module
# is deliberately pure Python (no Django imports), so importing it does not pull
# the backend into the Streamlit process.
from accounts import roles as role_defs

st.set_page_config(page_title="Roshada", page_icon=theme.FAVICON_PATH, layout="wide",
                   initial_sidebar_state="expanded")

# The AI assistant provider (Groq, OpenAI-compatible or Gemini) is resolved lazily by
# shared.ai on first use; each page asks it directly rather than caching a flag
# here, so a key added to .env takes effect without an app restart.

inject_theme()
init_session_state()
ensure_reminders()
theme.loading_splash()


# ===========================================================================
# Small helpers
# ===========================================================================
def _get_json(endpoint):
    res = api_request("GET", endpoint)
    if res is not None and res.status_code == 200:
        return res.json()
    return None


def _upload_part(uploaded):
    """Build the requests `files` value for a Streamlit upload.

    Sending bare bytes makes requests label the part with the field name and no
    Content-Type, so the filename and MIME type never reach the backend and its
    extension/content-type checks had nothing to inspect. Send the real triple.
    """
    return (uploaded.name, uploaded.getvalue(), uploaded.type or "application/octet-stream")


def _consume_search():
    """Take the query submitted from the top bar, if any (single use)."""
    return st.session_state.pop("search_query", "") or ""


def _dashboard_summary():
    """Real, role-scoped dashboard aggregates from the API."""
    return _get_json("dashboard/summary/") or {}


def _metric(stats, key, fallback="—"):
    """Render a stat, or a neutral placeholder when it isn't tracked yet.

    The dashboards used to show invented figures (a fabricated medication
    adherence percentage, 237 appointments). Anything the product cannot
    actually measure now reads as "not tracked" rather than as a clinical fact.
    """
    value = stats.get(key)
    return fallback if value is None else f"{value:,}"


def _plotly_theme(fig, height=300):
    fig.update_layout(
        template="plotly_white",
        font=dict(family="Inter, sans-serif", color=theme.TOKENS["ink"], size=13),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=30, b=10), height=height,
        colorway=theme.CHART_COLORS,
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor=theme.TOKENS["border"], zeroline=False)
    return fig


def _card_open():
    st.markdown('<div class="rs-card">', unsafe_allow_html=True)


def _card_close():
    st.markdown('</div>', unsafe_allow_html=True)


# ===========================================================================
# Interactivity — router, header chrome, notifications, avatar menu
# ===========================================================================
def goto(label):
    """Programmatically navigate to a sidebar page (any button can call this)."""
    st.session_state["_goto"] = label
    st.rerun()


def _nav_for(role):
    """The flat nav for a role, falling back to the patient portal.

    The fallback is not a permission decision — the backend refuses whatever the
    caller may not do regardless of what the sidebar shows. It only guarantees
    the shell can always render something rather than raising a KeyError on an
    unrecognised role string.
    """
    return ROLE_NAV.get(role, PATIENT_NAV)


def _nav_labels():
    return [n[0] for n in _nav_for(st.session_state.get("role"))]


def _first_valid(*candidates):
    """Return the first candidate that is a real page in the current nav."""
    labels = _nav_labels()
    for c in candidates:
        if c in labels:
            return c
    return labels[0]


# Role-tailored top-bar content ------------------------------------------------
_SEARCH_PLACEHOLDERS = {
    role_defs.PATIENT: "Search doctors, appointments, medications…",
    role_defs.DOCTOR: "Search your patients, records…",
    role_defs.LABORATORY: "Search orders, samples, results…",
    role_defs.RADIOLOGY: "Search orders, studies, reports…",
    role_defs.PHARMACY: "Search prescriptions, orders, inventory…",
    role_defs.ADMIN: "Search users, providers, appointments…",
}


def _search_placeholder(role):
    return _SEARCH_PLACEHOLDERS.get(role, _SEARCH_PLACEHOLDERS[role_defs.PATIENT])


def _display_name(role):
    """The signed-in name as it should appear in the chrome.

    Doctors are titled; a laboratory or pharmacy is an organisation and takes
    its facility name, which is what the profile endpoint returns as ``name``.
    """
    display = (st.session_state.get("user_name") or "User").strip()
    if role == role_defs.DOCTOR:
        return "Dr. " + display
    return display


# ---------------------------------------------------------------------------
# Notifications
#
# These used to be *derived*: the top bar re-read /api/dashboard/summary/ and
# invented items from whatever the counts happened to be. That is gone. There
# is now one stored notification per real event, raised by the module that
# caused it, and this reads them.
# ---------------------------------------------------------------------------
NOTIFICATION_ICONS = {
    "appointment_created": "📅",
    "appointment_confirmed": "✅",
    "appointment_cancelled": "🚫",
    "appointment_rescheduled": "🔄",
    "appointment_reminder": "⏰",
    "appointment_completed": "✅",
    "prescription_created": "💊",
    "prescription_updated": "💊",
    "lab_result_released": "🧪",
    "radiology_report_released": "🩻",
    "imaging_order_created": "🩻",
    "pharmacy_request_created": "🧾",
    "pharmacy_request_confirmed": "✅",
    "pharmacy_request_rejected": "🚫",
    "pharmacy_order_preparing": "⏳",
    "pharmacy_order_ready": "📦",
    "pharmacy_order_completed": "✅",
    "pharmacy_request_cancelled": "🚫",
    "message_received": "💬",
    "system_notification": "🔔",
}

#: Notification type -> the page that shows the record it points at. Stored
#: nowhere in the database on purpose: a nav label is a frontend concern, and
#: persisting one would strand every old notification the day a page is
#: renamed. The candidates are tried in order against the caller's own nav, so
#: one map serves every portal.
NOTIFICATION_DESTINATIONS = {
    "appointment_created": ("My Appointments", "Appointments"),
    "appointment_confirmed": ("My Appointments", "Appointments"),
    "appointment_cancelled": ("My Appointments", "Appointments"),
    "appointment_rescheduled": ("My Appointments", "Appointments"),
    "appointment_reminder": ("My Appointments", "Appointments"),
    "appointment_completed": ("My Appointments", "Appointments"),
    "prescription_created": ("Prescriptions",),
    "prescription_updated": ("Prescriptions",),
    "lab_result_released": ("Laboratory", "Medical Records"),
    "radiology_report_released": ("Radiology", "Reports", "Medical Records"),
    "imaging_order_created": ("Radiology", "Imaging Orders"),
    "pharmacy_request_created": ("Medication Requests", "Pharmacy"),
    "pharmacy_request_confirmed": ("Pharmacy", "Medication Requests"),
    "pharmacy_request_rejected": ("Pharmacy", "Medication Requests"),
    "pharmacy_order_preparing": ("Pharmacy", "Medication Requests"),
    "pharmacy_order_ready": ("Pharmacy", "Medication Requests"),
    "pharmacy_order_completed": ("Pharmacy", "Medication Requests"),
    "pharmacy_request_cancelled": ("Pharmacy", "Medication Requests"),
    "message_received": ("Messages",),
    "system_notification": ("Notifications",),
}


def _notification_icon(entry):
    return NOTIFICATION_ICONS.get(entry.get("type"), "🔔")


def _notification_destination(entry):
    """The page this notification opens, if the caller's portal has one."""
    labels = _nav_labels()
    for candidate in NOTIFICATION_DESTINATIONS.get(entry.get("type"), ()):
        if candidate in labels:
            return candidate
    return None


def _unread_badge():
    """The bell's count, cached for the rerun.

    The top bar renders on every rerun, so this must not become a request per
    page view. It is one indexed COUNT on the backend, cached here for the
    duration of a script run and invalidated whenever anything marks a
    notification read.
    """
    if "_unread" not in st.session_state:
        payload = _get_json("notifications/unread/") or {}
        st.session_state["_unread"] = payload.get("unread", 0)
        st.session_state["_unread_messages"] = payload.get("unread_messages", 0)
    return st.session_state["_unread"]


def _invalidate_unread():
    st.session_state.pop("_unread", None)
    st.session_state.pop("_unread_messages", None)


def _mark_notification_read(notification_id):
    api_request("POST", f"notifications/{notification_id}/read/", {})
    _invalidate_unread()


def _notifications_popover(role):
    """The bell. Shows the most recent few; the page shows everything."""
    unread = _unread_badge()
    with st.popover(f"🔔 {unread}" if unread else "🔔"):
        st.markdown("##### Notifications")
        payload = _get_json("notifications/?limit=6")
        if payload is None:
            st.caption("Unable to load notifications.")
            return
        entries = payload["results"]
        if not entries:
            st.caption("You're all caught up. 🎉")
        for entry in entries:
            dot = "" if entry["is_read"] else "🔵 "
            body = entry["body"] or entry["type_label"]
            if st.button(
                    f"{_notification_icon(entry)} {dot}**{entry['title']}** — {body}",
                    key=f"notif_{entry['id']}", use_container_width=True):
                _mark_notification_read(entry["id"])
                destination = _notification_destination(entry)
                if destination:
                    goto(destination)
                st.rerun()
        if payload["unread"]:
            if st.button("Mark all read", use_container_width=True,
                         key="notif_readall"):
                api_request("POST", "notifications/read-all/", {})
                _invalidate_unread()
                st.rerun()
        if st.button("Open notification center", use_container_width=True,
                     key="notif_open_all"):
            goto(_first_valid("Notifications"))


@st.dialog("Help & Support")
def _help_dialog():
    st.markdown(
        "**Roshada Health — Help Center**\n\n"
        "- 📖 Browse the in-app pages from the sidebar.\n"
        "- 💬 The AI Assistant answers general medical questions.\n\n"
        "Need a human? Email **support@roshada.health**.")
    if st.button("Got it", type="primary"):
        st.rerun()


def _avatar_menu(display, role):
    with st.popover(f"{avatar_initials(display)}  ▾"):
        st.markdown(f"**{html.escape(display)}**  \n_{role_defs.label(role)}_")
        st.divider()
        if st.button("👤  Profile", use_container_width=True, key="am_profile"):
            goto(_first_valid("Profile"))
        if st.button("⚙️  Settings", use_container_width=True, key="am_settings"):
            goto(_first_valid("Settings", "Profile"))
        if st.button("🔔  Notifications", use_container_width=True, key="am_notif"):
            goto(_first_valid("Notifications", "Medical Records"))
        if st.button("❓  Help", use_container_width=True, key="am_help"):
            _help_dialog()
        if st.button("🚪  Logout", use_container_width=True, key="am_logout"):
            handle_logout()


def render_topbar(title, role):
    """Single reusable top bar shown on EVERY page (matches Roshada_UI/1.png):
    title · role-tailored search · notifications · Need Help?/Support · avatar.
    Only the content varies by role — the layout is identical everywhere.
    """
    display = _display_name(role)

    left, mid, right = st.columns([3, 5, 3.4], gap="small")
    with left:
        st.markdown(f'<div class="rs-hdr-title"><div class="big">{html.escape(title)}</div></div>',
                    unsafe_allow_html=True)
    with mid:
        q = st.text_input("search", key="hdr_search", label_visibility="collapsed",
                          placeholder="🔍  " + _search_placeholder(role))
        if q and q.strip() and q != st.session_state.get("_search_done"):
            st.session_state["_search_done"] = q
            st.session_state["search_query"] = q.strip()
            # Each role searches whatever it works from: patients the doctor
            # directory, an admin the account directory, everyone else their own
            # queue. _first_valid falls through to the role's own first page
            # when none of the candidates exist in its nav.
            if role == role_defs.PATIENT:
                goto(_first_valid("Find Doctors", "My Appointments"))
            elif role == role_defs.ADMIN:
                goto(_first_valid("Users", "Dashboard"))
            else:
                goto(_first_valid("Appointments", "Orders", "Dashboard"))
    with right:
        st.markdown('<div class="rs-hdr-actions">', unsafe_allow_html=True)
        b, h, a = st.columns([1, 1.4, 1])
        with b:
            _notifications_popover(role)
        with h:
            if st.button("Need Help?  \nSupport", key="hdr_help", use_container_width=True):
                _help_dialog()
        with a:
            _avatar_menu(display, role)
        st.markdown('</div>', unsafe_allow_html=True)


# ===========================================================================
# Authentication
# ===========================================================================
def render_auth():
    """Split-screen login matching the approved mockup (Roshada_UI/Login.png)."""
    theme.inject_login_css()
    left, right = st.columns(2, gap="small")
    with left:
        st.markdown(
            f'<div class="rs-login-left"><div style="text-align:center">'
            f'<div class="logo-badge">{theme.logo_mark_html(76)}</div>'
            f'<div style="margin-top:.6rem">{theme.login_illustration_svg(300)}</div>'
            f'<div class="brand-word">Roshada</div>'
            f'<div class="tag">Your Health, Our Priority.</div></div>'
            f'<div class="foot">© 2026 Roshada Health. All Rights Reserved.</div></div>',
            unsafe_allow_html=True)
    with right:
        st.markdown('<div style="height:11vh"></div>', unsafe_allow_html=True)
        _, mid, _ = st.columns([1, 10, 1])
        with mid:
            if st.session_state.get("auth_mode") == "signup":
                _signup_panel()
            else:
                _login_panel()


def _login_panel():
    st.markdown('<div style="font-size:1.7rem;font-weight:800;color:#12294A;'
                'margin:0 0 1.1rem">Welcome Back to Roshada</div>', unsafe_allow_html=True)
    with st.form("login_form"):
        email = st.text_input("Email", placeholder="name@email.com")
        password = st.text_input("Password", type="password", placeholder="••••••••")
        c1, c2 = st.columns(2)
        c1.checkbox("Remember Me", value=True)
        c2.markdown('<div style="text-align:right;padding-top:.5rem">'
                    '<a href="#" style="color:#2563EB;font-weight:600;text-decoration:none">'
                    'Forgot Password?</a></div>', unsafe_allow_html=True)
        if st.form_submit_button("Login", use_container_width=True):
            with st.spinner("Signing you in…"):
                handle_login(email, password)   # email accepts username too
    st.markdown('<div class="rs-divider">or continue with</div>', unsafe_allow_html=True)
    st.markdown(theme.social_button_html("Google"), unsafe_allow_html=True)
    st.markdown(theme.social_button_html("Microsoft"), unsafe_allow_html=True)
    s1, s2 = st.columns([2, 1])
    s1.markdown('<div style="color:var(--muted);padding-top:.5rem">New to Roshada?</div>',
                unsafe_allow_html=True)
    if s2.button("Sign Up", use_container_width=True):
        st.session_state.auth_mode = "signup"
        st.rerun()
    st.markdown('<div class="rs-login-foot">Need Help? Contact Support &nbsp;|&nbsp; '
                'Privacy Policy &nbsp;|&nbsp; Terms of Service</div>', unsafe_allow_html=True)


def _signup_panel():
    st.markdown('<div style="font-size:1.7rem;font-weight:800;color:#12294A;'
                'margin:0 0 1.1rem">Create your Roshada account</div>', unsafe_allow_html=True)
    _render_signup()
    if st.button("← Back to Sign in", use_container_width=True):
        st.session_state.auth_mode = "login"
        st.rerun()


# Registration is a two-step flow — choose the account type, then fill that
# type's form — so the role selector sits OUTSIDE st.form: a widget inside a
# form does not trigger a rerun until submit, which would make the fields below
# it impossible to switch.
_SIGNUP_NAME_LABEL = {
    role_defs.PATIENT: "Full name",
    role_defs.DOCTOR: "Full name",
    role_defs.LABORATORY: "Laboratory name",
    role_defs.RADIOLOGY: "Center name",
    role_defs.PHARMACY: "Pharmacy name",
}

_SIGNUP_SERVICES_LABEL = {
    role_defs.LABORATORY: ("Tests offered", "e.g. CBC, Lipid profile, HbA1c"),
    role_defs.RADIOLOGY: ("Imaging services", "e.g. X-ray, CT, MRI, Ultrasound"),
    role_defs.PHARMACY: ("Services offered", "e.g. Prescription dispensing, delivery"),
}


def _id_autofill_panel():
    """National-ID upload — patients only; it auto-fills personal details."""
    uploaded_id = st.file_uploader("Upload National ID (optional — auto-fills details)",
                                   type=["jpg", "jpeg", "png"])
    if uploaded_id is None:
        return
    st.image(uploaded_id, caption="ID preview", use_container_width=True)
    if st.button("📷 Extract details from ID", use_container_width=True):
        with st.spinner("Reading your ID…"):
            res = api_request("POST", "ocr/extract-id/",
                              files={"file": _upload_part(uploaded_id)})
        if res is not None and res.status_code == 200:
            d = res.json()
            st.session_state.auto_name = d.get("name", "")
            st.session_state.auto_gender = d.get("gender", "")
            st.session_state.auto_address = d.get("address", "")
            st.session_state.auto_age = d.get("age") or 0
            st.success("Details extracted — review the form below.")
        else:
            show_api_error(res, "Couldn't read the ID. Try a clearer photo.")


def _signup_role_fields(role):
    """The fields specific to one account type. Returns the extra payload."""
    if role == role_defs.PATIENT:
        c1, c2 = st.columns(2)
        gender = c1.text_input("Gender", value=st.session_state.auto_gender)
        age = c2.number_input("Age", 0, 120, int(st.session_state.auto_age or 0))
        phone = st.text_input("Phone", placeholder="Optional")
        address = st.text_area("Address", value=st.session_state.auto_address)
        return {"gender": gender, "age": age, "phone": phone, "address": address}

    if role == role_defs.DOCTOR:
        c1, c2 = st.columns(2)
        spec = c1.text_input("Specialization", placeholder="e.g. Cardiology")
        licence = c2.text_input("Medical license number", placeholder="Optional")
        phone = c1.text_input("Phone", placeholder="Optional")
        clinic = c2.text_input("Clinic / hospital", placeholder="Optional")
        return {"specialization": spec, "license_number": licence,
                "phone": phone, "clinic": clinic}

    # The three facility types register identically — only the wording differs.
    services_label, services_hint = _SIGNUP_SERVICES_LABEL[role]
    c1, c2 = st.columns(2)
    licence = c1.text_input("License / registration number", placeholder="Optional")
    phone = c2.text_input("Phone", placeholder="Optional")
    email = c1.text_input("Contact email", placeholder="Optional")
    hours = c2.text_input("Operating hours", placeholder="e.g. Sun–Thu 9:00–17:00")
    address = st.text_area("Address", placeholder="Street, city")
    services = st.text_area(services_label, placeholder=services_hint)
    return {"license_number": licence, "phone": phone, "email": email,
            "operating_hours": hours, "address": address, "services": services}


def _render_signup():
    st.caption("Step 1 — choose your account type")
    role = st.selectbox(
        "Account type", list(role_defs.SELF_SERVICE_ROLES),
        format_func=role_defs.label, key="signup_role",
        help="Administrator accounts are created by the Roshada team, not here.")

    if role == role_defs.PATIENT:
        _id_autofill_panel()

    st.caption(f"Step 2 — {role_defs.label(role)} details")
    # Keyed by role so switching type gives a clean form rather than reusing the
    # previous type's widget state.
    with st.form(f"signup_form_{role}"):
        c1, c2 = st.columns(2)
        new_user = c1.text_input("Username", help="3–30 chars: letters, digits, . _ -")
        new_pass = c2.text_input("Password", type="password", help="Use a strong password.")
        full_name = st.text_input(_SIGNUP_NAME_LABEL[role],
                                  value=st.session_state.auto_name
                                  if role == role_defs.PATIENT else "")
        extra = _signup_role_fields(role)

        if st.form_submit_button("Create account", use_container_width=True):
            payload = {"username": new_user, "password": new_pass,
                       "name": full_name, **extra}
            with st.spinner("Creating your account…"):
                res = api_request("POST", f"signup/{role}/", payload)
            if res is not None and res.status_code == 201:
                # Straight into the portal for the role just created.
                handle_registration(res.json(), new_user)
            else:
                show_api_error(res, "Registration failed. Please check your details.")


# ===========================================================================
# Pages
# ===========================================================================
def _spline(x, y, color, fill_rgba, name="", hover=True):
    return go.Scatter(x=x, y=y, mode="lines", name=name,
                      line=dict(color=color, width=3, shape="spline", smoothing=1.1),
                      fill="tozeroy", fillcolor=fill_rgba,
                      hoverinfo="x+y" if hover else "skip",
                      hovertemplate=(f"<b>{name or 'value'}</b>: %{{y}}<extra></extra>" if hover else None))


def _weekly_appointments_chart(weekly, height=190):
    """Real appointments-per-day for the last 7 days."""
    import datetime as _dt
    labels = [_dt.date.fromisoformat(d["date"]).strftime("%a") for d in weekly]
    values = [d["count"] for d in weekly]
    fig = go.Figure(_spline(labels, values, "#2563EB",
                            "rgba(37,99,235,.12)", name="Appointments"))
    fig = _plotly_theme(fig, height=height)
    fig.update_yaxes(range=[0, max(max(values, default=0), 4) + 1], dtick=1)
    fig.update_layout(hovermode="x unified")
    return fig


def _hero(title, subtitle, cta, target, key, variant="doctor"):
    """Welcome banner with a REAL (functional) CTA button pulled into the banner."""
    with st.container():
        if variant == "doctor":
            body = (f'<div class="rs-heromark"></div><div class="rs-welcome">'
                    f'<div class="illus">{theme.dashboard_illustration_svg(340)}</div>'
                    f'<h2>{html.escape(title)}</h2><p>{html.escape(subtitle)}</p></div>')
        else:
            body = (f'<div class="rs-heromark"></div><div class="rs-pt-banner">{theme._pt_wave()}'
                    f'<h2>{html.escape(title)}</h2><p>{html.escape(subtitle)}</p></div>')
        st.markdown(body, unsafe_allow_html=True)
        if st.button(cta, key=key, type="primary"):
            goto(target)


def _calendar_functional():
    """Calendar widget with working month navigation and today highlighting."""
    import calendar as _calmod
    import datetime as _dt
    if "cal_offset" not in st.session_state:
        st.session_state["cal_offset"] = 0
    today = _dt.date.today()
    m = today.month - 1 + st.session_state["cal_offset"]
    year, month = today.year + m // 12, m % 12 + 1
    events = [3, 9, 12, 18, 24]  # demo appointment markers
    with st.container(border=True):
        st.markdown('<h4 style="margin:0 0 .3rem">Calendar Widget</h4>', unsafe_allow_html=True)
        st.markdown('<div class="rs-cal-nav">', unsafe_allow_html=True)
        n1, n2, n3 = st.columns([1, 5, 1])
        with n1:
            if st.button("‹", key="cal_prev"):
                st.session_state["cal_offset"] -= 1
                st.rerun()
        with n2:
            st.markdown(f'<div style="text-align:center;font-weight:700;padding-top:.35rem">'
                        f'{_calmod.month_name[month]} {year}</div>', unsafe_allow_html=True)
        with n3:
            if st.button("›", key="cal_next"):
                st.session_state["cal_offset"] += 1
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        st.markdown(theme.calendar_grid_html(year, month, today, events), unsafe_allow_html=True)


def page_doctor_dashboard():
    """Doctor Dashboard — real figures from /api/dashboard/summary/.

    Every tile is derived from this doctor's own appointments. Metrics with no
    data source (medication adherence) are shown as "not tracked" rather than as
    an invented percentage.
    """
    name = (st.session_state.get("user_name") or "Roshada").strip()

    # ---- Hero (the shared top bar is rendered by the shell) ----
    _hero(f"Welcome back, Dr. {name}!",
          "Here's your schedule and your patients' latest results.",
          "View My Day", _first_valid("Appointments"), "hero_doc", variant="doctor")

    summary = _dashboard_summary()
    if not summary:
        show_api_error(None, "Couldn't load your dashboard.")
        return
    stats = summary.get("stats", {})
    today = summary.get("today", [])
    upcoming = summary.get("upcoming_appointments", [])

    cols = [3.25, 3.15, 5.7, 2.85]

    # ---- Row 1 ----
    a, b, mid, e = theme.row(cols)
    with a:
        with st.container(border=True):
            st.markdown('<div style="font-weight:700;font-size:1.08rem;margin:-.2rem 0 -.4rem">'
                        'Appointments (last 7 days)</div>', unsafe_allow_html=True)
            st.plotly_chart(
                _weekly_appointments_chart(summary.get("weekly_appointments", [])),
                use_container_width=True, config={"displayModeBar": False})
    with b:
        theme.metric_card("Appointments Today", _metric(stats, "appointments_today"), "")
        st.markdown('<div style="height:.9rem"></div>', unsafe_allow_html=True)
        theme.metric_card("Total Patients", _metric(stats, "total_patients"), "")
    with mid:
        m1, m2 = st.columns([1.12, 1], gap="medium")
        with m1:
            with st.container(border=True):
                st.markdown('<h4 style="margin:0 0 .5rem">Today\'s Schedule</h4>',
                            unsafe_allow_html=True)
                if today:
                    st.markdown(theme.appt_rows_html([
                        (a["patient"], a["reason"] or "—", a["time"]) for a in today
                    ]), unsafe_allow_html=True)
                else:
                    st.caption("No appointments scheduled for today.")
        with m2:
            _calendar_functional()
    with e:
        with st.container(border=True):
            st.markdown('<h4 style="margin:0 0 .7rem">Quick Actions</h4>', unsafe_allow_html=True)
            if st.button("View Schedule", key="qa_new", type="primary", use_container_width=True):
                goto(_first_valid("Appointments"))
            if st.button("Patient Records", key="qa_presc", use_container_width=True):
                goto(_first_valid("Medical Records"))
            if st.button("Access AI Assistant", key="qa_ai", use_container_width=True):
                goto("AI Assistant")

    st.markdown('<div style="height:1.1rem"></div>', unsafe_allow_html=True)

    # ---- Row 2 ----
    a2, b2, mid2, e2 = theme.row(cols)
    with a2:
        with st.container(border=True):
            st.markdown('<h4 style="margin:0 0 .5rem">Upcoming Appointments</h4>',
                        unsafe_allow_html=True)
            if upcoming:
                st.markdown(theme.appt_rows_html([
                    (a["patient"], a["date"], a["time"]) for a in upcoming
                ]), unsafe_allow_html=True)
            else:
                st.caption("Nothing booked yet.")
            if st.button("View Full Schedule", key="doc_sched", type="primary",
                         use_container_width=True):
                goto(_first_valid("Appointments"))
    with b2:
        with st.container(border=True):
            st.markdown('<h4 style="margin:0 0 .9rem">Recent Activity</h4>',
                        unsafe_allow_html=True)
            activity = summary.get("recent_activity", [])
            if activity:
                tone = {"cancelled": "#F04438", "completed": "#12B76A",
                        "no_show": "#F79009"}
                st.markdown(theme.activity_rows_html([
                    (item["text"], tone.get(item["status"], "#2563EB"))
                    for item in activity
                ]), unsafe_allow_html=True)
            else:
                st.caption("No activity yet.")
    with mid2:
        with st.container(border=True):
            st.markdown('<div style="font-weight:700;font-size:1.08rem;margin-top:.3rem">'
                        'Practice Overview</div>', unsafe_allow_html=True)
            o1, o2, o3 = st.columns(3)
            o1.metric("Completed", _metric(stats, "completed_appointments"))
            o2.metric("Cancelled", _metric(stats, "cancelled_appointments"))
            o3.metric("Upcoming", _metric(stats, "upcoming_appointments"))
            st.caption("Medication adherence isn't tracked yet — prescriptions "
                       "are not part of the product.")
    with e2:
        with st.container(border=True):
            st.markdown('<h4 style="margin:0 0 .8rem">AI Assistant Shortcut</h4>',
                        unsafe_allow_html=True)
            if st.button("💬  Ask Roshada AI", key="doc_askai", type="primary",
                         use_container_width=True):
                goto("AI Assistant")

    st.markdown('<div style="text-align:right;color:var(--muted);font-size:.8rem;margin-top:1.4rem">'
                '© 2026 Roshada Health. All Rights Reserved.</div>', unsafe_allow_html=True)


def page_patient_dashboard():
    """Patient Dashboard — real figures from /api/dashboard/summary/.

    Every tile here is derived from this patient's own records. Metrics the
    product does not yet track are shown as "—" with a caption, never as an
    invented number: a fabricated figure on a medical dashboard is unsafe.
    """
    # ---- Hero (the shared top bar is rendered by the shell) ----
    _hero("Smart Care. Better Life.",
          "Take care of your health today to tomorrow.",
          "Find Doctors", _first_valid("Find Doctors"), "hero_pt", variant="patient")

    summary = _dashboard_summary()
    if not summary:
        show_api_error(None, "Couldn't load your dashboard.")
        return
    stats = summary.get("stats", {})
    upcoming = summary.get("upcoming_appointments", [])

    # ---- Statistics cards ----
    s1, s2, s3 = st.columns(3)
    with s1:
        theme.patient_stat_card("calendar", "rgba(37,99,235,.10)", "#2563EB",
                                "Upcoming Appointments",
                                _metric(stats, "upcoming_appointments"))
    with s2:
        theme.patient_stat_card("doc", "rgba(18,183,106,.12)", "#12B76A",
                                "Total Appointments",
                                _metric(stats, "total_appointments"))
    with s3:
        theme.patient_stat_card("bell", "rgba(79,110,247,.12)", "#4F6EF7",
                                "Completed Visits",
                                _metric(stats, "completed_appointments"))

    st.markdown('<div style="height:1rem"></div>', unsafe_allow_html=True)

    # ---- Main 3-column grid ----
    c1, c2, c3 = st.columns(3)
    with c1:
        with st.container(border=True):
            st.markdown(
                '<div class="rs-pai"><div class="logo">' + theme.logo_mark_html(30) + '</div>'
                '<div><div class="t">AI Assistant</div>'
                '<div class="s">Smart assistant ready. Start chat.</div></div></div>',
                unsafe_allow_html=True)
            if st.button("Ask AI", key="pt_askai", type="primary", use_container_width=True):
                goto("AI Assistant")
    with c2:
        with st.container(border=True):
            st.markdown('<h4 style="margin:0 0 .8rem">Upcoming Appointments</h4>',
                        unsafe_allow_html=True)
            if upcoming:
                # The counterparty may be a doctor, a laboratory or an imaging
                # centre now, so only doctors get the title.
                st.markdown(theme.patient_appt_rows_html([
                    (("Dr. " if a.get("provider_role") == role_defs.DOCTOR else "")
                     + (a.get("provider") or "Provider"),
                     a.get("service") or a.get("specialization") or "",
                     f"{a['date']} · {a['time']}")
                    for a in upcoming
                ]), unsafe_allow_html=True)
            else:
                st.caption("No upcoming appointments.")
            if st.button("View Details", key="pt_viewdet", use_container_width=True):
                goto(_first_valid("My Appointments", "Book Appointment"))
    with c3:
        with st.container(border=True):
            st.markdown('<h4 style="margin:0 0 .4rem">Medications</h4>',
                        unsafe_allow_html=True)
            # Real counts now that the Pharmacy module exists. Zero is a
            # counted answer here, not a placeholder for one.
            pharmacy = summary.get("pharmacy") or {}
            active = pharmacy.get("active_prescriptions") or 0
            ready = pharmacy.get("requests_ready") or 0
            waiting = pharmacy.get("open_requests") or 0
            if not active and not waiting:
                st.caption("No active prescriptions. Ones your doctor writes "
                           "appear here.")
            else:
                st.markdown(
                    f"**{active}** active prescription(s) · "
                    f"**{pharmacy.get('prescribed_medications') or 0}** "
                    f"medication(s)")
                if ready:
                    st.markdown(badge(f"{ready} ready for pickup", "success"),
                                unsafe_allow_html=True)
                elif waiting:
                    st.markdown(badge(f"{waiting} request(s) with a pharmacy",
                                      "info"), unsafe_allow_html=True)
            if st.button("Open Pharmacy", key="dash_pharmacy",
                         use_container_width=True):
                goto(_first_valid("Pharmacy", "Prescriptions"))


def page_find_doctors():
    page_header("Find doctors", "Search specialists and book in seconds.", "🔎")
    doctors = _get_json("doctors/")
    if doctors is None:
        show_api_error(None, "Couldn't load doctors.")
        return
    # A query typed in the top bar navigates here; adopt it as the initial value
    # so the search actually filters instead of only switching pages.
    st.session_state.setdefault("doctor_search", "")
    pending = _consume_search()
    if pending:
        st.session_state["doctor_search"] = pending
    query = st.text_input("Search by name or specialization",
                          placeholder="e.g. cardiology", key="doctor_search")
    q = (query or "").strip().lower()
    filtered = [d for d in doctors
                if q in d["name"].lower() or q in d["specialization"].lower()] if q else doctors
    if not filtered:
        empty_state("No doctors found", "Try a different search term.", "🔍")
        return
    cols = st.columns(2)
    for i, d in enumerate(filtered):
        with cols[i % 2]:
            doctor_card(d["name"], d["specialization"], d.get("available", True))
            # Booking goes through the slot flow rather than a free-form time
            # box, so a patient can only pick times the doctor actually offers.
            if st.button(f"Book with Dr. {d['name']}", key=f"find_book_{d['id']}",
                         use_container_width=True):
                st.session_state["book_type"] = role_defs.DOCTOR
                st.session_state["_preselect_provider"] = d.get("user", {}).get("id")
                goto(_first_valid("Book Appointment"))


# ---------------------------------------------------------------------------
# Booking
#
# One flow books every kind of provider: choose type -> provider -> service ->
# date -> slot -> confirm. The Book Appointment, Laboratory and Radiology pages
# are all this component with the provider type pinned differently, because the
# steps genuinely are identical — only the vocabulary changes.
# ---------------------------------------------------------------------------
PROVIDER_TYPE_LABELS = {
    role_defs.DOCTOR: "Doctor",
    role_defs.LABORATORY: "Laboratory",
    role_defs.RADIOLOGY: "Radiology Center",
}

_SLOTS_PER_ROW = 4


def _provider_label(provider):
    prefix = "Dr. " if provider["role"] == role_defs.DOCTOR else ""
    detail = provider.get("detail") or ""
    suffix = f" — {detail[:60]}" if detail else ""
    return f"{prefix}{provider['name']}{suffix}"


def _slot_grid(key, slots):
    """The slot buttons. Returns the chosen ISO start, or None.

    Unavailable slots are rendered disabled rather than hidden: "10:00 is
    booked" is information, and a grid with holes in it reads as a real
    schedule rather than an arbitrary list of times.
    """
    chosen = None
    for row_start in range(0, len(slots), _SLOTS_PER_ROW):
        row = slots[row_start:row_start + _SLOTS_PER_ROW]
        columns = st.columns(_SLOTS_PER_ROW)
        for column, slot in zip(columns, row):
            with column:
                available = slot["available"]
                caption = {"booked": "Booked", "unavailable": "Unavailable",
                           "past": "Passed"}.get(slot["state"], "")
                if st.button(slot["start_time"], key=f"{key}_slot_{slot['start']}",
                             use_container_width=True, disabled=not available,
                             type="secondary",
                             help=caption or f"Book {slot['start_time']}–{slot['end_time']}"):
                    chosen = slot["start"]
    return chosen


def booking_flow(key, provider_role=None, intro=None):
    """The whole patient booking journey, as one reusable component."""
    # ---- Step 1: provider type ----
    role = provider_role
    if role is None:
        picked = st.radio("Provider type", list(PROVIDER_TYPE_LABELS),
                          format_func=lambda r: PROVIDER_TYPE_LABELS[r],
                          horizontal=True, key=f"{key}_type")
        role = picked
    if intro:
        st.caption(intro)

    providers = _get_json(f"providers/?type={role}")
    if providers is None:
        show_api_error(None, "Couldn't load providers.")
        return
    if not providers:
        empty_state(f"No {PROVIDER_TYPE_LABELS[role].lower()} available yet",
                    "Please check back later.", "🏥")
        return

    # ---- Step 2: provider ----
    options = {_provider_label(p): p for p in providers}
    label = st.selectbox(f"Choose a {PROVIDER_TYPE_LABELS[role].lower()}",
                         list(options), key=f"{key}_provider")
    provider = options[label]
    if provider.get("location"):
        st.caption(f"📍 {provider['location']}")

    # ---- Step 3: service ----
    services = _get_json(f"providers/{provider['id']}/services/") or []
    service = None
    if services:
        service_options = {"— No specific service —": None}
        for s in services:
            service_options[f"{s['name']} ({s['duration_minutes']} min)"] = s
        service = service_options[st.selectbox(
            "Service", list(service_options), key=f"{key}_service")]
        if service and service.get("preparation"):
            st.info(f"**Preparation:** {service['preparation']}")
    elif role != role_defs.DOCTOR:
        st.caption("This provider has not published a service list yet.")

    # ---- Step 4: date ----
    today = datetime.date.today()
    chosen_date = st.date_input("Date", value=today, min_value=today,
                                max_value=today + datetime.timedelta(days=365),
                                key=f"{key}_date")

    # ---- Step 5: slots ----
    query = f"slots/?provider={provider['id']}&date={chosen_date.isoformat()}"
    if service:
        query += f"&service={service['id']}"
    with st.spinner("Loading available times…"):
        payload = _get_json(query)
    if payload is None:
        show_api_error(None, "Couldn't load available times.")
        return

    if not payload["slots"]:
        if not payload.get("publishes_availability"):
            empty_state(
                "This provider has not published opening hours",
                "They can still be booked directly — ask them for a time, or "
                "pick another provider.", "🕐")
        else:
            empty_state("No appointments available for this date",
                        "Try another date.", "📅")
        return

    st.markdown(f"**Available times on {chosen_date.strftime('%A, %d %B %Y')}**")
    picked = _slot_grid(key, payload["slots"])
    if picked:
        st.session_state[f"{key}_chosen"] = {
            "provider": provider, "service": service,
            "date": chosen_date.isoformat(),
            "slot": next(s for s in payload["slots"] if s["start"] == picked),
        }
        st.rerun()

    free = [s for s in payload["slots"] if s["available"]]
    if not free:
        st.caption("Every time on this date is taken. Try another date.")

    # ---- Step 6: review and confirm ----
    chosen = st.session_state.get(f"{key}_chosen")
    if chosen and chosen["provider"]["id"] == provider["id"]:
        _confirm_booking(key, chosen)


def _confirm_booking(key, chosen):
    """The review step — nothing is sent until this is confirmed."""
    provider, service, slot = chosen["provider"], chosen["service"], chosen["slot"]
    st.markdown('<div style="height:.6rem"></div>', unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("##### Review your booking")
        c1, c2 = st.columns(2)
        c1.markdown(f"**Provider**  \n{_provider_label(provider)}")
        c2.markdown(f"**Service**  \n{service['name'] if service else 'General visit'}")
        c1.markdown(f"**Date**  \n{chosen['date']}")
        c2.markdown(f"**Time**  \n{slot['start_time']} – {slot['end_time']}")

        reason = st.text_area("Reason for visit (optional)", key=f"{key}_reason")
        confirm, cancel = st.columns([2, 1])
        if confirm.button("Confirm booking", type="primary",
                          use_container_width=True, key=f"{key}_confirm"):
            payload = {"provider_id": provider["id"], "date": chosen["date"],
                       "time": slot["start_time"] + ":00", "reason": reason}
            if service:
                payload["service_id"] = service["id"]
            with st.spinner("Booking…"):
                res = api_request("POST", "appointment/create/", payload)
            if res is not None and res.status_code == 201:
                st.session_state.pop(f"{key}_chosen", None)
                st.success("✅ Appointment booked successfully.")
                st.balloons()
            elif res is not None and res.status_code == 409:
                st.session_state.pop(f"{key}_chosen", None)
                st.error("That time was taken while you were deciding. "
                         "Please choose another slot.")
            else:
                show_api_error(res, "Unable to book this appointment. "
                                    "Please try again.")
        if cancel.button("Choose another time", use_container_width=True,
                         key=f"{key}_clear"):
            st.session_state.pop(f"{key}_chosen", None)
            st.rerun()


def page_book_appointment():
    page_header("Book appointment",
                "Choose a provider, a service and a time that works for you.", "📅")
    booking_flow("book")


def page_patient_laboratory():
    page_header("Laboratory", "Book a lab test and track your visits.", "🧪")
    booking_flow("lab_book", provider_role=role_defs.LABORATORY,
                 intro="Choose a laboratory, then the test you need.")


# ===========================================================================
# Radiology
#
# All of this sits on components the app already has: page_header, badge,
# empty_state, the bordered container, tabs and the booking flow. Nothing here
# introduces a colour, a font or a layout the rest of Roshada does not use.
# ===========================================================================
ORDER_TONE = {
    "ordered": "warning",
    "scheduled": "brand",
    "in_progress": "info",
    "report_pending": "info",
    "reported": "success",
    "cancelled": "danger",
}

EXAM_TONE = {
    "scheduled": "brand",
    "checked_in": "info",
    "in_progress": "info",
    "completed": "success",
    "cancelled": "danger",
}

REPORT_TONE = {
    "draft": "muted",
    "pending_review": "warning",
    "verified": "info",
    "released": "success",
    "cancelled": "danger",
    "pending": "muted",
}


def _order_card(order, show_patient=False):
    """One imaging order, rendered the same way everywhere it appears."""
    with st.container(border=True):
        head, chip = st.columns([4, 1])
        with head:
            who = ""
            if show_patient:
                patient = order.get("patient") or {}
                who = f" · {patient.get('first_name') or patient.get('username', '')}"
            st.markdown(f"**{html.escape(order['study_name'])}**"
                        f" · {order['modality_label']}{html.escape(who)}")
            if order.get("clinical_indication"):
                st.caption(f"Indication: {order['clinical_indication']}")
            doctor = order.get("doctor")
            if doctor:
                st.caption(f"Ordered by Dr. {doctor.get('first_name') or doctor.get('username')}"
                           f" · {order['created_at'][:10]}")
            else:
                st.caption(f"Requested by you · {order['created_at'][:10]}")
        with chip:
            st.markdown(badge(order["status_display"],
                              ORDER_TONE.get(order["status"], "muted")),
                        unsafe_allow_html=True)
    return order


def _download_file_button(stored, key):
    """Fetch through the authorizing endpoint, then hand the bytes over.

    The API never returns a URL for imaging, so the only way to get the bytes is
    this request — which the backend answers only for someone related to the
    study.
    """
    if st.button(f"⬇️ {stored['original_name'] or 'file'}", key=f"dl_{key}",
                 use_container_width=True):
        res = api_request("GET", f"radiology/files/{stored['id']}/download/")
        if res is not None and res.status_code == 200:
            st.session_state[f"_file_{key}"] = res.content
        else:
            show_api_error(res, "You are not authorized to view this file.")
    blob = st.session_state.get(f"_file_{key}")
    if blob:
        st.download_button("Save to your device", blob,
                           file_name=stored["original_name"] or "image.png",
                           mime=stored.get("content_type") or "application/octet-stream",
                           key=f"save_{key}", use_container_width=True)
        if (stored.get("content_type") or "").startswith("image/"):
            st.image(blob, use_container_width=True)


def _report_block(report, key):
    """A radiology report, or an honest statement that it is not ready."""
    if not report:
        st.caption("No report yet.")
        return
    if not report.get("id"):
        st.markdown(badge("Report is being prepared",
                          REPORT_TONE.get(report.get("status"), "muted")),
                    unsafe_allow_html=True)
        st.caption("Your radiology centre is still working on this report. "
                   "It appears here once it has been verified and released.")
        return

    st.markdown(badge(report["status_display"],
                      REPORT_TONE.get(report["status"], "muted")),
                unsafe_allow_html=True)
    if report.get("impression"):
        st.markdown("**Impression**")
        st.write(report["impression"])
    if report.get("findings"):
        st.markdown("**Findings**")
        st.write(report["findings"])
    if report.get("released_at"):
        st.caption(f"Released {report['released_at'][:16].replace('T', ' ')}")
    st.caption("This report was written by a radiologist. Discuss it with your "
               "doctor — Roshada does not interpret it for you.")


def _patient_book_imaging_tab():
    """Book from a doctor's order when there is one; otherwise self-book."""
    orders = _get_json("radiology/orders/?status=ordered") or []
    if orders:
        st.markdown("##### Awaiting booking")
        st.caption("Your doctor has requested these studies. Booking one "
                   "attaches it to that order — it does not create a new one.")
        for order in orders:
            _order_card(order)
            if st.button(f"Book {order['study_name']}",
                         key=f"bk_{order['id']}", use_container_width=True,
                         type="primary"):
                st.session_state["_booking_order"] = order
                st.rerun()
        st.divider()

    active = st.session_state.get("_booking_order")
    if active:
        _book_against_order(active)
        return

    st.markdown("##### Book imaging yourself")
    st.caption("You can book without a referral. It will be recorded as your "
               "own request, not as a doctor's order.")
    booking_flow("rad_book", provider_role=role_defs.RADIOLOGY)


def _book_against_order(order):
    """Slot picking for a specific order, restricted to its modality."""
    st.markdown(f"##### Booking: {html.escape(order['study_name'])}")
    st.caption(f"{order['modality_label']} · requested by your doctor")
    if st.button("← Choose a different order", key="cancel_order_booking"):
        st.session_state.pop("_booking_order", None)
        st.rerun()

    centers = _get_json(f"radiology/centers/?modality={order['modality']}")
    if centers is None:
        show_api_error(None, "Couldn't load radiology centres.")
        return
    if not centers:
        empty_state("No radiology centers found",
                    f"No centre currently offers {order['modality_label']} "
                    f"imaging. Please check back later.", "🏥")
        return

    options = {c["name"]: c for c in centers}
    center = options[st.selectbox("Radiology centre", list(options),
                                  key="order_center")]
    if center.get("address"):
        st.caption(f"📍 {center['address']}")
    if not center.get("verified"):
        st.caption("⏳ This centre is awaiting verification by Roshada.")

    services = center["services"]
    if not services:
        empty_state("No imaging services available", "", "🩻")
        return
    service_options = {f"{s['name']} ({s['duration_minutes']} min)": s
                       for s in services}
    service = service_options[st.selectbox("Imaging service",
                                           list(service_options),
                                           key="order_service")]
    if service.get("preparation"):
        st.info(f"**How to prepare:** {service['preparation']}")

    today = datetime.date.today()
    chosen_date = st.date_input("Date", value=today, min_value=today,
                                key="order_date")
    payload = _get_json(f"slots/?provider={center['id']}"
                        f"&date={chosen_date.isoformat()}&service={service['id']}")
    if payload is None:
        show_api_error(None, "Couldn't load available times.")
        return
    if not payload["slots"]:
        empty_state("No available appointments for this date",
                    "Try another date or another centre.", "📅")
        return

    st.markdown(f"**Available times on {chosen_date.strftime('%A, %d %B %Y')}**")
    picked = _slot_grid("order_slot", payload["slots"])
    if picked:
        slot = next(s for s in payload["slots"] if s["start"] == picked)
        with st.spinner("Booking…"):
            res = api_request(
                "POST", f"radiology/orders/{order['id']}/book/",
                {"service_id": service["id"],
                 "date": chosen_date.isoformat(),
                 "time": slot["start_time"] + ":00"})
        if res is not None and res.status_code == 201:
            st.session_state.pop("_booking_order", None)
            st.success("✅ Imaging appointment booked.")
            st.balloons()
            st.rerun()
        elif res is not None and res.status_code == 409:
            st.error("That time was taken while you were deciding. "
                     "Please choose another slot.")
        else:
            show_api_error(res, "Unable to book this appointment.")


def _patient_orders_tab():
    orders = _get_json("radiology/orders/")
    if orders is None:
        show_api_error(None, "Couldn't load your imaging orders.")
        return
    if not orders:
        empty_state("No imaging orders yet",
                    "When a doctor requests a scan for you, it appears here.",
                    "🩻")
        return
    for order in orders:
        _order_card(order)
        if order["status"] == "cancelled":
            continue
        with st.expander("Details"):
            if order["is_bookable"]:
                st.caption("Not booked yet — use the Book tab to choose a "
                           "centre and time.")
            if order["status"] in ("ordered", "scheduled"):
                with st.form(f"cancel_order_{order['id']}"):
                    reason = st.text_input("Reason (optional)",
                                           key=f"cor_{order['id']}")
                    if st.form_submit_button("Cancel this order",
                                             use_container_width=True):
                        res = api_request(
                            "POST", f"radiology/orders/{order['id']}/cancel/",
                            {"reason": reason})
                        if res is not None and res.status_code == 200:
                            st.success("Order cancelled.")
                            st.rerun()
                        else:
                            show_api_error(res, "Couldn't cancel that order.")


def _patient_studies_tab():
    """Examinations, their files and their reports."""
    examinations = _get_json("radiology/examinations/")
    if examinations is None:
        show_api_error(None, "Couldn't load your studies.")
        return
    if not examinations:
        empty_state("No imaging studies yet",
                    "Booked scans and their reports appear here.", "🖼️")
        return

    for exam in examinations:
        appointment = exam["appointment"]
        report = exam.get("report") or {}
        # A released report is the thing the patient came for, so it is
        # announced on the card and its panel opens by default rather than
        # hiding behind a collapsed expander.
        ready = bool(report.get("id")) and report.get("status") == "released"
        with st.container(border=True):
            head, chip = st.columns([4, 1])
            with head:
                service = (exam.get("service") or {}).get("name") or "Imaging"
                st.markdown(f"**{html.escape(service)}** · "
                            f"{exam['center']['name']}")
                st.caption(f"{appointment['date']} at {appointment['time']}")
            with chip:
                chips = badge(exam["status_display"],
                              EXAM_TONE.get(exam["status"], "muted"))
                if ready:
                    chips += " " + badge("Report ready", "success")
                elif report:
                    chips += " " + badge("Report pending", "muted")
                st.markdown(chips, unsafe_allow_html=True)

            with st.expander("Report and images", expanded=ready):
                _report_block(exam.get("report"), key=f"pt_{exam['id']}")
                files = exam.get("files") or []
                if files:
                    st.markdown("**Images**")
                    for stored in files:
                        _download_file_button(stored, f"pt_{stored['id']}")
                else:
                    st.caption("No images have been shared yet.")


def page_patient_radiology():
    page_header("Radiology", "Imaging orders, appointments and reports.", "🩻")
    book, orders, studies = st.tabs(["Book", "My orders", "Studies & reports"])
    with book:
        _patient_book_imaging_tab()
    with orders:
        _patient_orders_tab()
    with studies:
        _patient_studies_tab()


STATUS_TONE = {
    "scheduled": "brand",
    "completed": "success",
    "cancelled": "danger",
    "no_show": "warning",
}


def _cancel_controls(appointment, key):
    """Cancel form for a scheduled appointment (either party may cancel)."""
    with st.form(f"cancel_{key}"):
        reason = st.text_input("Reason (optional)", key=f"cancel_reason_{key}",
                               placeholder="e.g. feeling better, schedule clash")
        if st.form_submit_button("Confirm cancellation", use_container_width=True):
            res = api_request("POST", f"appointments/{appointment['id']}/cancel/",
                              {"reason": reason})
            if res is not None and res.status_code == 200:
                st.success("Appointment cancelled.")
                st.rerun()
            else:
                show_api_error(res, "Couldn't cancel this appointment.")


def _reschedule_controls(appointment, key):
    """Move a scheduled appointment to a different slot with the same doctor."""
    with st.form(f"resched_{key}"):
        c1, c2 = st.columns(2)
        new_date = c1.date_input("New date", key=f"rd_{key}")
        new_time = c2.time_input("New time", key=f"rt_{key}")
        if st.form_submit_button("Confirm new time", use_container_width=True):
            res = api_request("POST", f"appointments/{appointment['id']}/reschedule/",
                              {"date": new_date.strftime("%Y-%m-%d"),
                               "time": new_time.strftime("%H:%M:%S")})
            if res is not None and res.status_code == 200:
                st.success("Appointment rescheduled.")
                st.rerun()
            else:
                show_api_error(res, "Couldn't move this appointment.")


def _outcome_controls(appointment, key):
    """Doctor closes out a visit."""
    c1, c2 = st.columns(2)
    if c1.button("✅ Mark completed", key=f"done_{key}", use_container_width=True):
        res = api_request("POST", f"appointments/{appointment['id']}/outcome/",
                          {"status": "completed"})
        if res is not None and res.status_code == 200:
            st.success("Marked completed.")
            st.rerun()
        else:
            show_api_error(res, "Couldn't update this appointment.")
    if c2.button("🚫 Mark no-show", key=f"noshow_{key}", use_container_width=True):
        res = api_request("POST", f"appointments/{appointment['id']}/outcome/",
                          {"status": "no_show"})
        if res is not None and res.status_code == 200:
            st.success("Marked as a no-show.")
            st.rerun()
        else:
            show_api_error(res, "Couldn't update this appointment.")


def _appointment_when(a):
    """Local-time label. The API returns date/time derived from the instant."""
    return f"{a['date']} · {a['time'][:5]}–{(a.get('end_time') or '')[:5]}".rstrip("–")


def _is_upcoming(a):
    try:
        start = datetime.datetime.fromisoformat(a["start_at"])
    except (KeyError, ValueError):
        return True
    return start >= datetime.datetime.now(start.tzinfo)


def _appointment_entry(a, as_provider, key_prefix=""):
    """One appointment card, with the actions the viewer is allowed."""
    status = a.get("status", "scheduled")
    provider = a.get("provider") or {}
    if as_provider:
        counterpart = (a["patient"].get("first_name")
                       or a["patient"].get("username") or "Patient")
        detail = a.get("service", {}).get("name") if a.get("service") else "Visit"
    else:
        prefix = "Dr. " if provider.get("role") == role_defs.DOCTOR else ""
        counterpart = f"{prefix}{provider.get('name', 'Provider')}"
        detail = (a.get("service", {}) or {}).get("name") or provider.get("detail") or ""

    appointment_card(_appointment_when(a), counterpart, detail or "",
                     a.get("reason", ""))
    chips = badge(a.get("status_display", status.title()),
                  STATUS_TONE.get(status, "muted"))
    if provider.get("role_label") and not as_provider:
        chips += " " + badge(provider["role_label"], "muted")
    if a.get("service"):
        chips += " " + badge(a["service"]["name"], "info")
    st.markdown(chips, unsafe_allow_html=True)
    if provider.get("location") and not as_provider:
        st.caption(f"📍 {provider['location']}")
    if a.get("cancellation_reason"):
        st.caption(f"Cancellation reason: {a['cancellation_reason']}")

    if status == "scheduled":
        with st.expander("Manage this appointment"):
            key = f"{key_prefix}{a['id']}"
            if as_provider:
                _outcome_controls(a, key)
                st.divider()
            else:
                _reschedule_controls(a, key)
                st.divider()
            _cancel_controls(a, key)
    st.markdown('<div style="height:.6rem"></div>', unsafe_allow_html=True)


def _render_appointments(endpoint, empty_msg, search_key, as_doctor=False):
    """Appointments grouped into Upcoming / Past / Cancelled.

    ``as_doctor`` is really "am I the provider" — the same view serves a doctor,
    a laboratory and a radiology centre, because all three are looking at their
    own queue.
    """
    with st.spinner("Loading appointments…"):
        appts = _get_json(endpoint)
    if appts is None:
        show_api_error(None, "Couldn't load appointments.")
        return
    if not appts:
        empty_state("No appointments", empty_msg, "🗓️")
        return

    # Honour a query submitted from the top bar (see render_topbar).
    st.session_state.setdefault(search_key, "")
    pending = _consume_search()
    if pending:
        st.session_state[search_key] = pending

    query = st.text_input("Filter appointments",
                          placeholder="provider, service, date or reason",
                          key=search_key)
    q = (query or "").strip().lower()
    if q:
        def matches(a):
            provider = a.get("provider") or {}
            service = (a.get("service") or {}).get("name") or ""
            haystack = " ".join([
                provider.get("name", ""), provider.get("detail", ""),
                service, str(a.get("date", "")), a.get("reason") or "",
                (a.get("patient") or {}).get("username") or "",
            ]).lower()
            return q in haystack
        appts = [a for a in appts if matches(a)]

    cancelled = [a for a in appts if a.get("status") == "cancelled"]
    active = [a for a in appts if a.get("status") != "cancelled"]
    upcoming = [a for a in active if a.get("status") == "scheduled" and _is_upcoming(a)]
    past = [a for a in active if a not in upcoming]

    if not appts:
        empty_state("No matching appointments",
                    "Try a different search term.", "🔍")
        return

    sections = [
        ("Upcoming", upcoming, "Nothing booked yet."),
        ("Past", past, "No past appointments."),
        ("Cancelled", cancelled, "No cancelled appointments."),
    ]
    tabs = st.tabs([f"{title} ({len(items)})" for title, items, _ in sections])
    for tab, (title, items, empty) in zip(tabs, sections):
        with tab:
            if not items:
                empty_state(empty, "", "🗓️")
                continue
            for a in items:
                _appointment_entry(a, as_doctor, key_prefix=f"{title.lower()}_")


def page_my_appointments():
    page_header("My appointments", "Your upcoming and past visits.", "🗓️")
    _render_appointments("appointments/mine/", "You have no appointments yet.",
                         "my_appt_search")


def page_doctor_schedule():
    page_header("My schedule", "Appointments booked with you.", "🗓️")
    # The unified provider queue — a doctor is one kind of provider, so the
    # doctor-only endpoint is no longer the one to read.
    _render_appointments("appointments/provider/",
                         "No patients have booked with you yet.",
                         "doc_appt_search", as_doctor=True)


def _render_assistant_meta(answer):
    """Attribution and safety caveats that came back with an answer.

    Two kinds of attribution, kept apart because they mean different things:

    * **sources** — which parts of the user's own record the assistant was
      allowed to look at.
    * **citations** — the approved knowledge-base passages a *grounded* medical
      answer rests on. A grounded answer reads no patient record, so the two
      lists are never both populated.
    """
    warnings = answer.get("warnings") or []
    for warning in warnings:
        st.warning(warning, icon="⚠️")

    sources = answer.get("sources") or []
    if sources:
        chips = " ".join(badge(html.escape(s["label"]), "muted") for s in sources)
        st.markdown(f"<div style='margin-top:.35rem'>{chips}</div>",
                    unsafe_allow_html=True)
        st.caption("Answered using your own records only.")

    citations = answer.get("citations") or []
    if citations:
        with st.expander(f"Sources ({len(citations)})"):
            for citation in citations:
                # Only fields the knowledge base actually holds are shown. A
                # blank line is better than an invented reference.
                label = citation.get("reference") or citation.get("document_title") or ""
                used = "" if citation.get("cited") else " · not cited in the answer"
                st.markdown(f"**[{citation['n']}]** {html.escape(label)}"
                            f"<span style='opacity:.6'>{used}</span>",
                            unsafe_allow_html=True)
                url = citation.get("url") or citation.get("source_url")
                if url:
                    st.caption(url)
        st.caption("Answered from Roshada's approved medical knowledge base. "
                   "Your personal records were not used.")


def page_ai_assistant():
    page_header("AI medical assistant", DISCLAIMER, "💬")
    if not ai.is_enabled():
        st.warning("The assistant is unavailable: no AI provider is configured "
                   "for this deployment. Everything else in Roshada still works.")
        return
    st.caption(f"Powered by **{ai.provider_label()}** · your conversation is private "
               "to your account")

    # Load this account's own history once per session, so the thread survives a
    # refresh or a different device instead of living only in session_state.
    if not st.session_state.get("_chat_loaded"):
        st.session_state.messages = [
            {"role": m["role"], "text": m["text"]}
            for m in chat_history.load_messages(limit=50)
        ]
        st.session_state["_chat_loaded"] = True

    for msg in st.session_state.messages:
        with st.chat_message("user" if msg["role"] == "user" else "assistant"):
            st.write(msg["text"])

    prompt = st.chat_input("Ask about symptoms, medication, or diet…")
    if prompt:
        st.session_state.messages.append({"role": "user", "text": prompt})
        with st.chat_message("user"):
            st.write(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                # One call: the server assembles context, prompts, calls the
                # provider, validates the answer and stores the turn pair. The
                # conversation window is the server's — the page no longer keeps
                # a second, divergent one in session_state.
                answer = ai.ask(clean_html(prompt))
            reply = answer.get("reply") or ""
            st.write(reply)
            _render_assistant_meta(answer)
        st.session_state.messages.append({"role": "assistant", "text": reply})

    if st.session_state.messages:
        with st.expander("🗂️ Conversation history"):
            st.caption("Only you can see this. It is stored with your account.")
            if st.button("Delete my chat history", type="secondary"):
                if chat_history.clear_history():
                    st.session_state.messages = []
                    st.session_state["_chat_loaded"] = False
                    st.success("Your chat history was deleted.")
                    st.rerun()
                else:
                    st.error("Couldn't delete your history. Please try again.")


# ---------------------------------------------------------------------------
# Unified Medical Record
#
# One set of renderers, used by the patient's own record and by a doctor
# reading one of their patients'. The API decides what each of them may see, so
# the UI never needs a second copy of that rule — it draws whatever the
# timeline returned.
# ---------------------------------------------------------------------------
RECORD_TYPE_ICONS = {
    "consultation": "🩺",
    "appointment": "📅",
    "lab_result": "🧪",
    "radiology_order": "🩻",
    "radiology_report": "📄",
    "prescription": "💊",
    "medication_order": "🧾",
}

RECORD_TYPE_TONE = {
    "consultation": "brand",
    "appointment": "info",
    "lab_result": "info",
    "radiology_order": "warning",
    "radiology_report": "success",
    "prescription": "brand",
    "medication_order": "info",
}

#: Statuses that should read as a warning wherever they appear on the timeline.
_ALERT_STATUSES = {"cancelled", "rejected", "no_show", "high_risk"}


def _record_endpoint(patient_id=None, suffix=""):
    """The caller's own record, or a patient's if a doctor is reading one."""
    if patient_id:
        return f"records/patients/{patient_id}/{suffix}"
    return f"records/me/{suffix}"


def _timeline_row(entry, key):
    """One event: when, what, who, status — and a way into the source module."""
    with st.container(border=True):
        head, chip = st.columns([4, 1])
        with head:
            icon = RECORD_TYPE_ICONS.get(entry["type"], "•")
            st.markdown(f"**{icon} {html.escape(entry['title'])}**")
            line = [entry["date"][:10], entry["type_label"]]
            if entry.get("provider"):
                line.append(html.escape(entry["provider"]))
            st.caption(" · ".join(line))
            if entry.get("detail"):
                st.caption(html.escape(entry["detail"]))
        with chip:
            if entry.get("status_label"):
                tone = ("danger" if entry.get("status") in _ALERT_STATUSES
                        else RECORD_TYPE_TONE.get(entry["type"], "muted"))
                st.markdown(badge(entry["status_label"], tone),
                            unsafe_allow_html=True)

        # "View details" hands off to the module that owns the record rather
        # than re-rendering it here — so opening one runs that module's own
        # permission check instead of a copy of it.
        destination = entry.get("destination")
        if destination and destination in _nav_labels():
            if st.button(f"View in {destination}", key=f"tl_{key}",
                         use_container_width=True):
                goto(destination)


def _timeline_panel(patient_id=None, key_prefix="own"):
    """The filterable, paginated timeline."""
    state_key = f"_tl_offset_{key_prefix}"
    st.session_state.setdefault(state_key, 0)

    kinds = _get_json("records/types/") or []
    # Kinds the platform cannot produce yet are offered but labelled, rather
    # than hidden — a filter that silently returns nothing is worse than one
    # that says why.
    options = {"All records": None}
    for kind in kinds:
        suffix = "" if kind["available"] else "  (not available yet)"
        options[f"{kind['label']}{suffix}"] = kind["value"]

    c1, c2 = st.columns([2, 3])
    with c1:
        chosen = st.selectbox("Show", list(options), key=f"{key_prefix}_type")
    with c2:
        term = st.text_input("Search", key=f"{key_prefix}_q",
                             placeholder="e.g. MRI, Amoxicillin, a doctor's name")

    with st.expander("Date range"):
        d1, d2 = st.columns(2)
        use_range = st.checkbox("Filter by date", key=f"{key_prefix}_use_range")
        start = d1.date_input("From", key=f"{key_prefix}_from",
                              value=datetime.date.today() - datetime.timedelta(days=365))
        end = d2.date_input("To", key=f"{key_prefix}_to",
                            value=datetime.date.today())

    selected = options[chosen]
    query = [f"limit=10&offset={st.session_state[state_key]}"]
    if selected:
        query.append(f"type={selected}")
    if term.strip():
        query.append(f"q={term.strip()}")
    if use_range:
        query.append(f"from={start.isoformat()}&to={end.isoformat()}")

    payload = _get_json(_record_endpoint(patient_id, "timeline/")
                        + "?" + "&".join(query))
    if payload is None:
        show_api_error(None, "Couldn't load the medical timeline.")
        return

    entries = payload["results"]
    if not entries:
        if selected and not any(k["value"] == selected and k["available"]
                                for k in kinds):
            empty_state(f"{chosen.split('  (')[0]} is not available yet",
                        "Roshada does not have a module producing these "
                        "records. Nothing is hidden — there is nothing to "
                        "show.", "🗂️")
        elif st.session_state[state_key]:
            empty_state("No more records", "You have reached the end.", "🗂️")
        else:
            empty_state("No medical records available",
                        "Consultations, imaging and prescriptions appear "
                        "here as they happen.", "🗂️")
    for index, entry in enumerate(entries):
        _timeline_row(entry, f"{key_prefix}_{st.session_state[state_key]}_{index}")

    back, forward = st.columns(2)
    with back:
        if st.session_state[state_key] and st.button(
                "← Newer", key=f"{key_prefix}_prev", use_container_width=True):
            st.session_state[state_key] = max(
                st.session_state[state_key] - 10, 0)
            st.rerun()
    with forward:
        if payload.get("has_more") and st.button(
                "Older →", key=f"{key_prefix}_next", use_container_width=True):
            st.session_state[state_key] += 10
            st.rerun()


def _record_overview(overview, patient_id=None):
    """The landing view: what happened lately, and what is coming."""
    counts = overview.get("counts", {})
    c1, c2, c3, c4 = st.columns(4)
    for column, (label_text, key, icon, tint, colour) in zip(
            (c1, c2, c3, c4),
            [("Consultations", "consultation", "🩺", "var(--brand-50)", "var(--brand)"),
             ("Imaging reports", "radiology_report", "📄", "rgba(79,110,247,.12)", "var(--accent)"),
             ("Prescriptions", "prescription", "💊", "rgba(18,183,106,.12)", "var(--success)"),
             ("Medication orders", "medication_order", "🧾",
              "rgba(247,144,9,.14)", "var(--warning)")]):
        with column:
            stat_card(label_text, counts.get(key, 0), icon, tint, colour)
    st.markdown('<div style="height:1rem"></div>', unsafe_allow_html=True)

    upcoming = overview.get("upcoming_appointments") or []
    if upcoming:
        st.markdown("##### Upcoming")
        for item in upcoming:
            with st.container(border=True):
                who = item["provider"]
                prefix = "Dr. " if item.get("provider_role") == "doctor" else ""
                service = f" · {item['service']}" if item.get("service") else ""
                st.markdown(f"**📅 {prefix}{html.escape(who)}**")
                st.caption(f"{item['date']} at {item['time']}{service}")

    st.markdown("##### Recent activity")
    recent = overview.get("recent_activity") or []
    if not recent:
        empty_state("No medical records available",
                    "Your consultations, imaging and prescriptions will "
                    "appear here.", "🗂️")
    for index, entry in enumerate(recent):
        _timeline_row(entry, f"ov_{patient_id or 'me'}_{index}")

    missing = overview.get("unavailable_types") or []
    if missing:
        # Named honestly rather than shown as an empty section: "we have no
        # laboratory module" and "you have no lab results" are different
        # statements, and only one of them is true.
        labels = ", ".join(
            RECORD_TYPE_ICONS.get(kind, "") + " " + kind.replace("_", " ")
            for kind in missing)
        st.caption(f"Not tracked by Roshada yet: {labels}.")


def page_medical_records():
    role = st.session_state.get("role")
    if role == role_defs.DOCTOR:
        return _page_doctor_records()

    page_header("Medical records",
                "Your consultations, imaging and prescriptions in one "
                "place.", "📋")
    overview = _get_json("records/me/")
    if overview is None:
        show_api_error(None, "Couldn't load your medical record.")
        return

    tab_overview, tab_timeline, tab_profile = st.tabs(
        ["Overview", "Timeline", "History"])
    with tab_overview:
        _record_overview(overview)
    with tab_timeline:
        _timeline_panel(key_prefix="own")
    with tab_profile:
        _patient_history_panel()

    st.caption("Roshada shows these records; it does not interpret them. "
               "Discuss anything here with your doctor.")


def _patient_history_panel():
    """The profile summary and medical history this page always had.

    Kept because they are real records the patient already relied on — the
    unified timeline adds to this page rather than replacing what was there.
    """
    data = _get_json("profile/") or {}

    c1, c2 = st.columns(2)
    with c1:
        stat_card("Age", data.get("age", "—"), "🎂", "var(--brand-50)",
                  "var(--brand)")
    with c2:
        stat_card("Gender", data.get("gender") or "—", "⚧",
                  "rgba(79,110,247,.12)", "var(--accent)")
    st.markdown('<div style="height:1rem"></div>', unsafe_allow_html=True)

    _card_open()
    st.markdown("##### Medical history")
    history = (data.get("medical_history") or "").strip()
    if history:
        st.write(history)
    else:
        st.caption("No medical history recorded. Add it from your Profile.")
    _card_close()


def _page_doctor_records():
    """Doctor view: pick one of your patients and read their unified record."""
    page_header("Patient records",
                "The medical history of patients you treat.", "📋")
    patients = _get_json("records/patients/")
    if patients is None:
        show_api_error(None, "Couldn't load your patients.")
        return
    if not patients:
        empty_state("No patients yet",
                    "Once a patient books with you, their records appear here.",
                    "📋")
        return

    labels = {f"{p['name']} (@{p['username']})": p["id"] for p in patients}
    choice = st.selectbox("Patient", list(labels), key="doc_patient_pick")
    patient_id = labels[choice]

    overview = _get_json(_record_endpoint(patient_id))
    if overview is None:
        # The backend refuses a patient this doctor does not treat, and it
        # answers 404 rather than 403 so an id cannot be probed.
        show_api_error(None, "You can only open records for patients you treat.")
        return

    tab_overview, tab_timeline = st.tabs(["Overview", "Timeline"])
    with tab_overview:
        _record_overview(overview, patient_id)
    with tab_timeline:
        _timeline_panel(patient_id, key_prefix=f"pat{patient_id}")

    st.caption("You see what this patient can see: released reports and issued "
               "prescriptions. Dispensing activity stays with the pharmacy.")


def _notification_row(entry, key):
    """One notification: what happened, when, and a way to the record."""
    with st.container(border=True):
        head, chip = st.columns([5, 1])
        with head:
            dot = "" if entry["is_read"] else "🔵 "
            st.markdown(f"**{dot}{_notification_icon(entry)} "
                        f"{html.escape(entry['title'])}**")
            if entry.get("body"):
                st.caption(html.escape(entry["body"]))
            st.caption(f"{entry['created_at'][:16].replace('T', ' ')} · "
                       f"{entry['type_label']}")
        with chip:
            if not entry["is_read"]:
                st.markdown(badge("Unread", "brand"), unsafe_allow_html=True)

        destination = _notification_destination(entry)
        actions = []
        if destination:
            actions.append(("open", f"Open {destination}", "primary"))
        actions.append(("read", "Mark unread" if entry["is_read"]
                        else "Mark read", "secondary"))
        columns = st.columns(len(actions))
        for column, (action, label_text, kind) in zip(columns, actions):
            with column:
                if st.button(label_text, key=f"n_{key}_{action}", type=kind,
                             use_container_width=True):
                    if action == "open":
                        _mark_notification_read(entry["id"])
                        goto(destination)
                    else:
                        api_request("POST",
                                    f"notifications/{entry['id']}/read/",
                                    {"read": not entry["is_read"]})
                        _invalidate_unread()
                        st.rerun()


def page_notifications():
    page_header("Notifications", "Everything the platform has told you.", "🔔")
    st.session_state.setdefault("_notif_offset", 0)

    vocabulary = _get_json("notifications/types/") or {}
    categories = vocabulary.get("categories", [])
    counts = (_get_json("notifications/unread/") or {}).get("by_category", {})

    options = {"All": None, "Unread": "__unread__"}
    for category in categories:
        pending = counts.get(category["value"], 0)
        suffix = f" ({pending})" if pending else ""
        options[f"{category['label']}{suffix}"] = category["value"]

    c1, c2 = st.columns([3, 1])
    with c1:
        chosen = st.radio("Show", list(options), horizontal=True,
                          key="notif_filter", label_visibility="collapsed")
    with c2:
        if st.button("Mark all read", use_container_width=True,
                     key="notif_page_readall"):
            api_request("POST", "notifications/read-all/", {})
            _invalidate_unread()
            st.rerun()

    selected = options[chosen]
    query = [f"limit=10&offset={st.session_state['_notif_offset']}"]
    if selected == "__unread__":
        query.append("unread=true")
    elif selected:
        query.append(f"category={selected}")

    payload = _get_json("notifications/?" + "&".join(query))
    if payload is None:
        show_api_error(None, "Unable to load notifications.")
        return

    entries = payload["results"]
    if not entries:
        if selected == "__unread__":
            empty_state("You're all caught up",
                        "Nothing is waiting for your attention.", "🎉")
        elif st.session_state["_notif_offset"]:
            empty_state("No more notifications", "You've reached the end.",
                        "🔔")
        else:
            empty_state("No new notifications",
                        "Appointments, prescriptions, imaging reports and "
                        "medication updates appear here as they happen.", "🔔")
    for index, entry in enumerate(entries):
        _notification_row(entry, f"{st.session_state['_notif_offset']}_{index}")

    back, forward = st.columns(2)
    with back:
        if st.session_state["_notif_offset"] and st.button(
                "← Newer", key="notif_prev", use_container_width=True):
            st.session_state["_notif_offset"] = max(
                st.session_state["_notif_offset"] - 10, 0)
            st.rerun()
    with forward:
        if payload.get("has_more") and st.button(
                "Older →", key="notif_next", use_container_width=True):
            st.session_state["_notif_offset"] += 10
            st.rerun()


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------
def _conversation_list(conversations):
    """The thread picker, newest activity first."""
    st.markdown("##### Conversations")
    for conversation in conversations:
        unread = conversation.get("unread") or 0
        label_text = conversation["counterparty"]
        if unread:
            label_text = f"🔵 {label_text} ({unread})"
        if st.button(label_text, key=f"conv_{conversation['id']}",
                     use_container_width=True):
            st.session_state["_conversation"] = conversation["id"]
            api_request("POST",
                        f"conversations/{conversation['id']}/read/", {})
            _invalidate_unread()
            st.rerun()
        if conversation.get("last_message"):
            st.caption(html.escape(conversation["last_message"]))


def _message_thread(conversation_id):
    """One conversation: history, then the composer."""
    payload = _get_json(
        f"conversations/{conversation_id}/messages/?limit=30")
    if payload is None:
        show_api_error(None, "You are not authorized to view this conversation.")
        return

    conversation = payload["conversation"]
    st.markdown(f"##### {html.escape(conversation['counterparty'])}")
    if payload.get("has_more"):
        st.caption(f"Showing the latest {payload['count']} of "
                   f"{payload['total']} messages.")

    if not payload["results"]:
        empty_state("No messages yet", "Start the conversation below.", "💬")

    for message in payload["results"]:
        with st.container(border=True):
            who = "You" if message["is_mine"] else message["sender_name"]
            st.markdown(f"**{html.escape(who)}**")
            st.write(message["body"])
            stamp = message["created_at"][:16].replace("T", " ")
            state = " · Read" if message["is_read"] else ""
            st.caption(f"{stamp}{state if message['is_mine'] else ''}")

    with st.form(f"composer_{conversation_id}", clear_on_submit=True):
        body = st.text_area("Type a message…", key=f"body_{conversation_id}",
                            label_visibility="collapsed",
                            placeholder="Type a message…")
        if st.form_submit_button("Send", type="primary",
                                 use_container_width=True):
            if not body.strip():
                st.error("Write something first.")
            else:
                res = api_request(
                    "POST", f"conversations/{conversation_id}/messages/",
                    {"body": body})
                if res is not None and res.status_code == 201:
                    _invalidate_unread()
                    st.rerun()
                else:
                    show_api_error(res, "Couldn't send that message.")


def _start_conversation_panel():
    """Open a thread with someone the care relationship already allows."""
    contacts = _get_json("conversations/contacts/") or []
    if not contacts:
        st.caption("You can message a doctor you have an appointment with. "
                   "Book a consultation first."
                   if st.session_state.get("role") == role_defs.PATIENT else
                   "You can message patients you have appointments with.")
        return
    options = {f"{c['name']} (@{c['username']})": c["id"] for c in contacts}
    chosen = st.selectbox("Start a conversation with", list(options),
                          key="new_conv_pick")
    if st.button("Start conversation", key="new_conv_go",
                 use_container_width=True):
        res = api_request("POST", "conversations/",
                          {"user_id": options[chosen]})
        if res is not None and res.status_code in (200, 201):
            st.session_state["_conversation"] = res.json()["id"]
            st.rerun()
        else:
            show_api_error(res, "Couldn't start that conversation.")


def page_messages():
    page_header("Messages", "Secure messaging with your care team.", "💬")
    role = st.session_state.get("role")
    if role not in (role_defs.PATIENT, role_defs.DOCTOR):
        # The backend refuses these roles anyway; saying so beats an empty list.
        empty_state("Messaging is for patients and doctors",
                    "Roshada's messaging is between a patient and a doctor "
                    "they have an appointment with.", "💬")
        return

    conversations = _get_json("conversations/")
    if conversations is None:
        show_api_error(None, "Unable to load your conversations.")
        return

    picker, thread = st.columns([1, 2], gap="medium")
    with picker:
        if conversations:
            _conversation_list(conversations)
            st.divider()
        with st.expander("➕ New conversation",
                         expanded=not conversations):
            _start_conversation_panel()

    with thread:
        current = st.session_state.get("_conversation")
        valid = {c["id"] for c in conversations}
        if current in valid:
            _message_thread(current)
        elif conversations:
            empty_state("Pick a conversation",
                        "Choose someone on the left to read the thread.", "💬")
        else:
            empty_state("No conversations yet",
                        "Start one with a doctor you have an appointment with."
                        if role == role_defs.PATIENT else
                        "Start one with a patient you treat.", "💬")

    st.caption("Messages are private to the two of you. They are not added to "
               "the medical record — ask your doctor to record anything "
               "clinically important.")


def _save_profile(payload):
    with st.spinner("Saving…"):
        put = api_request("PUT", "profile/", payload)
    if put is not None and put.status_code in (200, 201):
        st.success("✅ Profile updated.")
    else:
        show_api_error(put, "Couldn't update your profile.")


def _patient_profile_form(data):
    if data.get("gender") or data.get("address"):
        st.markdown(badge(f"Gender: {data.get('gender') or '—'}", "muted") + " " +
                    badge(f"Address: {data.get('address') or '—'}", "muted"),
                    unsafe_allow_html=True)
    with st.form("profile_update"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Full name", value=data.get("first_name", ""))
        email = c2.text_input("Email", value=data.get("email", ""))
        age = c1.number_input("Age", 0, 120, int(data.get("age") or 0))
        phone = c2.text_input("Phone", value=data.get("phone") or "")
        address = st.text_area("Address", value=data.get("address") or "")
        history = st.text_area("Medical history", value=data.get("medical_history", ""))
        if st.form_submit_button("Save changes", use_container_width=True):
            _save_profile({"first_name": name, "email": email, "age": age,
                           "phone": phone, "address": address,
                           "medical_history": history})


def _doctor_profile_form(data):
    with st.form("profile_update"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Full name", value=data.get("name") or data.get("first_name", ""))
        email = c2.text_input("Email", value=data.get("email", ""))
        spec = c1.text_input("Specialization", value=data.get("specialization") or "")
        licence = c2.text_input("License number", value=data.get("license_number") or "")
        phone = c1.text_input("Phone", value=data.get("phone") or "")
        clinic = c2.text_input("Clinic / hospital", value=data.get("clinic") or "")
        available = st.toggle("Accepting appointments",
                              value=bool(data.get("available", True)))
        if st.form_submit_button("Save changes", use_container_width=True):
            _save_profile({"first_name": name, "email": email, "name": name,
                           "specialization": spec, "license_number": licence,
                           "phone": phone, "clinic": clinic,
                           "available": available})


def _facility_profile_form(data):
    verified = data.get("verified")
    st.markdown(
        (badge("✅ Verified", "success") if verified
         else badge("⏳ Pending verification", "warning")) + " " +
        badge("Accepting work" if data.get("available") else "Not accepting work",
              "muted"),
        unsafe_allow_html=True)
    if not verified:
        st.caption("A Roshada administrator reviews new providers before they are "
                   "offered to patients. You can complete your profile meanwhile.")
    with st.form("profile_update"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Facility name", value=data.get("name") or "")
        email = c2.text_input("Contact email", value=data.get("email", ""))
        licence = c1.text_input("License / registration number",
                                value=data.get("license_number") or "")
        phone = c2.text_input("Phone", value=data.get("phone") or "")
        hours = st.text_input("Operating hours", value=data.get("operating_hours") or "")
        address = st.text_area("Address", value=data.get("address") or "")
        services = st.text_area("Services offered", value=data.get("services") or "")
        available = st.toggle("Accepting work", value=bool(data.get("available", True)))
        if st.form_submit_button("Save changes", use_container_width=True):
            _save_profile({"name": name, "email": email, "license_number": licence,
                           "phone": phone, "operating_hours": hours,
                           "address": address, "services": services,
                           "available": available})


def _admin_profile_form(data):
    _card_open()
    st.markdown("##### Administrator account")
    st.write(f"**{html.escape(data.get('username', ''))}** — "
             f"{html.escape(data.get('role_label', 'Administrator'))}")
    st.caption("Administrator accounts are managed through the Django admin site, "
               "not through this form.")
    _card_close()
    with st.form("profile_update"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Display name", value=data.get("first_name", ""))
        email = c2.text_input("Email", value=data.get("email", ""))
        if st.form_submit_button("Save changes", use_container_width=True):
            _save_profile({"first_name": name, "email": email})


_PROFILE_FORMS = {
    role_defs.PATIENT: _patient_profile_form,
    role_defs.DOCTOR: _doctor_profile_form,
    role_defs.LABORATORY: _facility_profile_form,
    role_defs.RADIOLOGY: _facility_profile_form,
    role_defs.PHARMACY: _facility_profile_form,
    role_defs.ADMIN: _admin_profile_form,
}

_PROFILE_SUBTITLE = {
    role_defs.PATIENT: "Manage your personal and medical details.",
    role_defs.DOCTOR: "Manage your professional details and availability.",
    role_defs.ADMIN: "Your administrator account.",
}


def page_profile():
    """One profile page for all six roles.

    The *server* decides which fields exist for the caller — the form is built
    from what ``/profile/`` returned, and the API only ever writes the columns
    belonging to the caller's own role. Rendering a field the role does not have
    would therefore be cosmetic, never a way in.
    """
    data = _get_json("profile/")
    if data is None:
        show_api_error(None, "Couldn't load your profile.")
        return

    role = data.get("role") or st.session_state.get("role")
    page_header("Profile",
                _PROFILE_SUBTITLE.get(role, "Manage your facility details."), "👤")
    _PROFILE_FORMS.get(role, _patient_profile_form)(data)


# ===========================================================================
# Provider portals — laboratory, radiology, pharmacy
#
# The portals are real: the operator signs in, sees their own facility, edits
# their profile and controls whether they are accepting work. The order, sample,
# imaging and inventory domains are a later task, so those pages carry empty
# states rather than invented rows — a fabricated lab result in a medical UI is
# worse than a blank page.
# ===========================================================================
def _placeholder_page(title, subtitle, icon, empty_title, empty_body):
    """Build a page function for a destination whose domain is not built yet.

    A factory rather than ~30 near-identical function bodies, and it reuses the
    same page_header + empty_state components as every other page, so these
    screens are visually part of the app rather than obvious stubs.
    """
    def page():
        page_header(title, subtitle, icon)
        empty_state(empty_title, empty_body, icon)
    page.__name__ = f"page_{title.lower().replace(' ', '_').replace('/', '_')}"
    return page


# Scheduling figures are real for laboratories and radiology centres; the order
# and result tiles have no data source yet and render as "—" via _metric.
_FACILITY_TILES = {
    role_defs.LABORATORY: [
        ("appointments_today", "Today's appointments", "📅"),
        ("upcoming", "Upcoming bookings", "🗓️"),
        ("pending_orders", "Pending orders", "🧾"),
        ("results_ready", "Results ready", "✅"),
    ],
    role_defs.RADIOLOGY: [
        ("appointments_today", "Today's appointments", "📅"),
        ("upcoming", "Upcoming bookings", "🗓️"),
        ("pending_orders", "Pending orders", "🧾"),
        ("results_ready", "Reports ready", "✅"),
    ],
    role_defs.PHARMACY: [
        ("pending_orders", "Prescriptions to fill", "💊"),
        ("in_progress", "Orders preparing", "📦"),
        ("completed_today", "Dispensed today", "✅"),
        ("results_ready", "Ready for pickup", "🛍️"),
    ],
}


def page_facility_dashboard():
    role = st.session_state.get("role")
    label = role_defs.label(role)
    summary = _dashboard_summary()
    facility = summary.get("facility", {})

    page_header(f"{label} Dashboard",
                facility.get("name") or "Your facility at a glance.", "🏥")

    status_html = (badge("✅ Verified", "success") if facility.get("verified")
                   else badge("⏳ Pending verification", "warning"))
    status_html += " " + (badge("Accepting work", "info") if facility.get("available")
                          else badge("Not accepting work", "muted"))
    if facility.get("operating_hours"):
        status_html += " " + badge(f"🕐 {facility['operating_hours']}", "muted")
    st.markdown(status_html, unsafe_allow_html=True)
    st.markdown('<div style="height:.8rem"></div>', unsafe_allow_html=True)

    # Every tile reads "—": the backend reports None because there is no order
    # or result table yet. _metric renders that honestly rather than as 0.
    stats = summary.get("stats", {})
    tiles = _FACILITY_TILES.get(role, _FACILITY_TILES[role_defs.LABORATORY])
    columns = st.columns(len(tiles))
    for column, (key, tile_label, icon) in zip(columns, tiles):
        with column:
            stat_card(tile_label, _metric(stats, key), icon)

    st.markdown('<div style="height:1rem"></div>', unsafe_allow_html=True)
    left, right = st.columns([2, 1])
    with left:
        _card_open()
        st.markdown("##### Today's schedule")
        today = summary.get("today", [])
        if not summary.get("bookable"):
            empty_state("Not part of the appointment engine",
                        "Medication is dispensed, not booked into a slot.", "💊")
        elif today:
            for a in today:
                with st.container(border=True):
                    st.markdown(
                        f"**{a['time']}–{a['end_time']}** · "
                        f"{html.escape(a['patient'])}"
                        + (f" · {html.escape(a['service'])}" if a.get("service") else ""))
                    if a.get("reason"):
                        st.caption(a["reason"])
        elif not summary.get("publishes_availability"):
            empty_state(
                "No opening hours published",
                "Patients cannot book you until you publish hours under "
                "Availability.", "🕐")
            if st.button("Publish opening hours", use_container_width=True,
                         key="fac_publish"):
                goto(_first_valid("Availability"))
        else:
            empty_state("Nothing booked today",
                        "Bookings for today will appear here.", "📋")
        _card_close()
    with right:
        _card_open()
        st.markdown("##### Your services")
        if facility.get("services"):
            st.write(facility["services"])
        else:
            st.caption("No services listed yet — add them from Profile so patients "
                       "and doctors know what you offer.")
        if st.button("Edit profile", use_container_width=True, key="fac_edit"):
            goto(_first_valid("Profile"))
        _card_close()


# ---------------------------------------------------------------------------
# Provider schedule management
#
# One set of components for doctors, laboratories and radiology centres. Every
# request here is scoped to the signed-in provider by the API — there is no
# provider id in any of these calls to tamper with.
# ---------------------------------------------------------------------------
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]

SERVICE_NOUNS = {
    role_defs.DOCTOR: ("service", "Consultation types you offer"),
    role_defs.LABORATORY: ("test", "Tests you offer"),
    role_defs.RADIOLOGY: ("study", "Imaging services you offer"),
}


def _my_services():
    return _get_json("me/services/") or []


def _services_manager():
    """Add, adjust and withdraw the caller's own services."""
    role = st.session_state.get("role")
    noun, heading = SERVICE_NOUNS.get(role, SERVICE_NOUNS[role_defs.DOCTOR])
    services = _my_services()

    st.markdown(f"##### {heading}")
    if not services:
        empty_state(f"No {noun}s listed yet",
                    f"Add a {noun} so patients can book it, and so its duration "
                    f"sets the length of the slot.", "🧾")
    for service in services:
        with st.container(border=True):
            head, action = st.columns([4, 1])
            with head:
                state = (badge("Active", "success") if service["is_active"]
                         else badge("Withdrawn", "muted"))
                st.markdown(f"**{html.escape(service['name'])}** "
                            f"· {service['duration_minutes']} min &nbsp; {state}",
                            unsafe_allow_html=True)
                if service.get("description"):
                    st.caption(service["description"])
                if service.get("preparation"):
                    st.caption(f"Preparation: {service['preparation']}")
            with action:
                if service["is_active"] and st.button(
                        "Withdraw", key=f"svc_del_{service['id']}",
                        use_container_width=True):
                    res = api_request("DELETE", f"me/services/{service['id']}/")
                    if res is not None and res.status_code == 200:
                        st.success(f"{service['name']} withdrawn.")
                        st.rerun()
                    else:
                        show_api_error(res, "Couldn't withdraw that.")

    with st.form("add_service"):
        st.markdown(f"**Add a {noun}**")
        c1, c2 = st.columns([3, 1])
        name = c1.text_input("Name", placeholder="e.g. CBC")
        minutes = c2.number_input("Minutes", 5, 480, 30, step=5)
        description = st.text_input("Description (optional)")
        preparation = st.text_input("Preparation (optional)",
                                    placeholder="e.g. Fast for 8 hours beforehand")
        if st.form_submit_button(f"Add {noun}", use_container_width=True):
            if not name.strip():
                st.error("Give it a name.")
            else:
                res = api_request("POST", "me/services/",
                                  {"name": name.strip(),
                                   "duration_minutes": int(minutes),
                                   "description": description,
                                   "preparation": preparation})
                if res is not None and res.status_code == 201:
                    st.success(f"{name} added.")
                    st.rerun()
                else:
                    show_api_error(res, f"Couldn't add that {noun}.")


def _opening_hours_manager():
    rules = _get_json("me/availability/")
    if rules is None:
        show_api_error(None, "Couldn't load your opening hours.")
        return
    services = _my_services()

    st.markdown("##### Published hours")
    if not rules:
        empty_state("No opening hours published",
                    "Until you publish hours, patients cannot see bookable "
                    "times for you.", "🕐")
    for rule in rules:
        with st.container(border=True):
            head, action = st.columns([4, 1])
            when = (rule["date"] if rule["date"]
                    else f"Every {rule['weekday_display']}")
            scope = rule.get("service_name") or "All services"
            window = f"{rule['start_time'][:5]}–{rule['end_time'][:5]}"
            chips = (badge(f"{rule['slot_minutes']} min slots", "brand")
                     + " " + badge(scope, "muted"))
            with head:
                st.markdown(f"**{when}** · {window} &nbsp; {chips}",
                            unsafe_allow_html=True)
            with action:
                if st.button("Remove", key=f"rule_del_{rule['id']}",
                             use_container_width=True):
                    res = api_request("DELETE", f"me/availability/{rule['id']}/")
                    if res is not None and res.status_code == 200:
                        st.success("Removed.")
                        st.rerun()
                    else:
                        show_api_error(res, "Couldn't remove that.")

    with st.form("add_rule"):
        st.markdown("**Open some hours**")
        mode = st.radio("Repeats", ["Every week", "One specific date"],
                        horizontal=True, key="rule_mode")
        c1, c2, c3 = st.columns(3)
        if mode == "Every week":
            weekday = c1.selectbox("Day", range(7),
                                   format_func=lambda i: WEEKDAY_NAMES[i])
            specific = None
        else:
            weekday = None
            specific = c1.date_input("Date", value=datetime.date.today(),
                                     min_value=datetime.date.today())
        start = c2.time_input("From", value=datetime.time(9, 0))
        end = c3.time_input("To", value=datetime.time(13, 0))
        c4, c5 = st.columns(2)
        slot_minutes = c4.number_input("Slot length (minutes)", 5, 240, 30, step=5)
        service_choice = {"All services": None}
        for s in services:
            service_choice[s["name"]] = s["id"]
        scope = c5.selectbox("Applies to", list(service_choice))

        if st.form_submit_button("Publish hours", use_container_width=True):
            if end <= start:
                st.error("The end time must be after the start time.")
            else:
                payload = {"start_time": start.strftime("%H:%M:%S"),
                           "end_time": end.strftime("%H:%M:%S"),
                           "slot_minutes": int(slot_minutes),
                           "service": service_choice[scope]}
                if specific is not None:
                    payload["date"] = specific.isoformat()
                else:
                    payload["weekday"] = int(weekday)
                res = api_request("POST", "me/availability/", payload)
                if res is not None and res.status_code == 201:
                    st.success("Hours published.")
                    st.rerun()
                else:
                    show_api_error(res, "Couldn't publish those hours.")


def _time_off_manager():
    entries = _get_json("me/time-off/")
    if entries is None:
        show_api_error(None, "Couldn't load your blocked time.")
        return

    st.markdown("##### Blocked time")
    st.caption("Lunch, maintenance, leave — nothing can be booked in these "
               "periods, whatever your opening hours say.")
    if not entries:
        empty_state("Nothing blocked", "", "🚫")
    for entry in entries:
        with st.container(border=True):
            head, action = st.columns([4, 1])
            window = ("All day" if not entry["start_time"]
                      else f"{entry['start_time'][:5]}–{entry['end_time'][:5]}")
            with head:
                st.markdown(f"**{entry['date']}** · {window}"
                            + (f" — {html.escape(entry['reason'])}"
                               if entry.get("reason") else ""))
            with action:
                if st.button("Remove", key=f"off_del_{entry['id']}",
                             use_container_width=True):
                    res = api_request("DELETE", f"me/time-off/?id={entry['id']}")
                    if res is not None and res.status_code == 200:
                        st.rerun()
                    else:
                        show_api_error(res, "Couldn't remove that.")

    with st.form("add_time_off"):
        st.markdown("**Block a period**")
        c1, c2 = st.columns(2)
        off_date = c1.date_input("Date", value=datetime.date.today(),
                                 min_value=datetime.date.today(), key="off_date")
        all_day = c2.checkbox("All day", value=False)
        c3, c4 = st.columns(2)
        start = c3.time_input("From", value=datetime.time(13, 0), disabled=all_day)
        end = c4.time_input("To", value=datetime.time(14, 0), disabled=all_day)
        reason = st.text_input("Reason (optional)", placeholder="e.g. Lunch")
        if st.form_submit_button("Block this time", use_container_width=True):
            payload = {"date": off_date.isoformat(), "reason": reason}
            if not all_day:
                if end <= start:
                    st.error("The end time must be after the start time.")
                    payload = None
                else:
                    payload["start_time"] = start.strftime("%H:%M:%S")
                    payload["end_time"] = end.strftime("%H:%M:%S")
            if payload:
                res = api_request("POST", "me/time-off/", payload)
                if res is not None and res.status_code == 201:
                    st.success("Blocked.")
                    st.rerun()
                else:
                    show_api_error(res, "Couldn't block that period.")


def _accepting_work_panel():
    """The facility's own on/off switch, alongside the schedule."""
    data = _get_json("profile/")
    if data is None:
        return
    st.markdown("##### Accepting work")
    st.markdown(badge("Accepting work", "success") if data.get("available")
                else badge("Not accepting work", "muted"), unsafe_allow_html=True)
    st.caption("Turning this off hides you from patient search without "
               "cancelling anything already booked.")
    with st.form("accepting_form"):
        available = st.toggle("Accepting work",
                              value=bool(data.get("available", True)))
        hours = st.text_input("Opening hours (shown on your profile)",
                              value=data.get("operating_hours") or "",
                              placeholder="e.g. Sun–Thu 9:00–17:00")
        if st.form_submit_button("Save", use_container_width=True):
            _save_profile({"available": available, "operating_hours": hours})
            st.rerun()


def page_provider_availability():
    """Opening hours, blocked time and services — for any provider role."""
    page_header("Availability", "Publish when patients can book you.", "🕐")
    role = st.session_state.get("role")
    tabs = st.tabs(["Opening hours", "Blocked time", "Services", "Status"])
    with tabs[0]:
        _opening_hours_manager()
    with tabs[1]:
        _time_off_manager()
    with tabs[2]:
        _services_manager()
    with tabs[3]:
        if role == role_defs.DOCTOR:
            _doctor_accepting_panel()
        else:
            _accepting_work_panel()


def _doctor_accepting_panel():
    data = _get_json("profile/") or {}
    st.markdown("##### Accepting appointments")
    st.markdown(badge("Accepting appointments", "success") if data.get("available")
                else badge("Not accepting appointments", "muted"),
                unsafe_allow_html=True)
    with st.form("doctor_accepting"):
        available = st.toggle("Accepting appointments",
                              value=bool(data.get("available", True)))
        if st.form_submit_button("Save", use_container_width=True):
            _save_profile({"available": available})
            st.rerun()


def _doctor_new_order_tab():
    # A doctor may only order for patients they treat, so the list is built
    # from their own appointments rather than from a directory of everyone —
    # and the backend re-checks that relationship when the order is submitted.
    appointments = _get_json("appointments/provider/") or []
    seen, options = set(), {}
    for a in appointments:
        patient = a.get("patient") or {}
        pid = patient.get("id")
        if pid and pid not in seen:
            seen.add(pid)
            label = patient.get("first_name") or patient.get("username")
            options[f"{label} (@{patient.get('username')})"] = pid

    if not options:
        empty_state("No patients yet",
                    "You can order imaging for patients you have appointments "
                    "with. Book or complete a consultation first.", "🧑‍⚕️")
        return

    modality_list = _get_json("radiology/modalities/") or []
    modality_labels = {m["label"]: m["value"] for m in modality_list}

    with st.form("new_imaging_order"):
        st.markdown("##### New imaging order")
        c1, c2 = st.columns(2)
        patient_label = c1.selectbox("Patient", list(options))
        modality_label = c2.selectbox("Modality", list(modality_labels))
        study_name = st.text_input("Study", placeholder="e.g. MRI Brain with contrast")
        indication = st.text_area(
            "Clinical indication",
            placeholder="Why this study is being requested")
        notes = st.text_input("Notes (optional)")
        if st.form_submit_button("Create order", use_container_width=True,
                                 type="primary"):
            if not study_name.strip():
                st.error("Name the study you are requesting.")
            else:
                res = api_request("POST", "radiology/orders/", {
                    "patient_id": options[patient_label],
                    "modality": modality_labels[modality_label],
                    "study_name": study_name.strip(),
                    "clinical_indication": indication, "notes": notes})
                if res is not None and res.status_code == 201:
                    # No rerun here: it would discard this confirmation before
                    # the doctor could read it. The form clears itself, and the
                    # order shows up on the next tab.
                    st.success("✅ Imaging order created. The patient can now "
                               "book it at a centre of their choice.")
                else:
                    show_api_error(res, "Couldn't create that order.")


def _doctor_orders_tab():
    orders = _get_json("radiology/orders/")
    if orders is None:
        show_api_error(None, "Couldn't load your imaging orders.")
        return
    if not orders:
        empty_state("No imaging orders yet",
                    "Orders you create appear here with their status.", "🩻")
        return
    for order in orders:
        _order_card(order, show_patient=True)


def _doctor_reports_tab():
    reports = _get_json("radiology/reports/")
    if reports is None:
        show_api_error(None, "Couldn't load reports.")
        return
    if not reports:
        empty_state("No released reports yet",
                    "Reports for studies you ordered appear here once the "
                    "radiology centre has verified and released them.", "📄")
        return
    for report in reports:
        with st.container(border=True):
            st.markdown(f"**Report #{report['id']}**")
            _report_block(report, key=f"doc_{report['id']}")


def page_doctor_radiology_orders():
    page_header("Radiology Orders", "Request imaging and track its progress.",
                "🩻")
    new, mine, reports = st.tabs(["New order", "My orders", "Reports"])
    with new:
        _doctor_new_order_tab()
    with mine:
        _doctor_orders_tab()
    with reports:
        _doctor_reports_tab()


# ---------------------------------------------------------------------------
# Radiology centre
# ---------------------------------------------------------------------------
_EXAM_ACTIONS = [
    ("scheduled", "checked_in", "Check in", "primary"),
    ("checked_in", "in_progress", "Start examination", "primary"),
    ("in_progress", "completed", "Complete", "primary"),
]


def _exam_actions(exam):
    """Only the one legal next step is offered — the API refuses the rest."""
    for current, target, label, kind in _EXAM_ACTIONS:
        if exam["status"] != current:
            continue
        if st.button(label, key=f"adv_{exam['id']}_{target}", type=kind,
                     use_container_width=True):
            res = api_request(
                "POST", f"radiology/examinations/{exam['id']}/status/",
                {"status": target})
            if res is not None and res.status_code == 200:
                st.success(f"{label} recorded.")
                st.rerun()
            else:
                show_api_error(res, "Couldn't update this examination.")


def _exam_files_panel(exam):
    st.markdown("**Images and documents**")
    for stored in exam.get("files") or []:
        st.caption(f"{stored['original_name']} · {stored['size_bytes']:,} bytes"
                   + (f" · {stored['modality_code']}" if stored.get("modality_code") else ""))
    if not exam.get("files"):
        st.caption("Nothing attached yet.")

    if exam["status"] == "cancelled":
        return
    with st.form(f"upload_{exam['id']}"):
        uploaded = st.file_uploader("Attach an image", type=["jpg", "jpeg", "png"],
                                    key=f"up_{exam['id']}")
        description = st.text_input("Description (optional)",
                                    key=f"updesc_{exam['id']}",
                                    placeholder="e.g. Axial T1")
        if st.form_submit_button("Upload", use_container_width=True):
            if uploaded is None:
                st.error("Choose a file first.")
            else:
                res = api_request(
                    "POST", f"radiology/examinations/{exam['id']}/files/",
                    data={"description": description},
                    files={"file": _upload_part(uploaded)})
                if res is not None and res.status_code == 201:
                    st.success("Uploaded.")
                    st.rerun()
                else:
                    show_api_error(res, "Couldn't attach that file.")


def _exam_report_panel(exam):
    report = exam.get("report") or {}
    st.markdown("**Report**")
    if exam["status"] != "completed":
        st.caption("A report can be written once the examination is complete.")
        return

    editable = report.get("status") in (None, "draft", "pending_review")
    with st.form(f"report_{exam['id']}"):
        findings = st.text_area("Findings", value=report.get("findings", ""),
                                key=f"rf_{exam['id']}", disabled=not editable)
        impression = st.text_area("Impression",
                                  value=report.get("impression", ""),
                                  key=f"ri_{exam['id']}", disabled=not editable)
        if st.form_submit_button("Save draft", use_container_width=True,
                                 disabled=not editable):
            res = api_request(
                "POST", f"radiology/examinations/{exam['id']}/report/",
                {"findings": findings, "impression": impression})
            if res is not None and res.status_code == 200:
                st.success("Draft saved.")
                st.rerun()
            else:
                show_api_error(res, "Couldn't save the report.")

    if not report.get("id"):
        return
    st.markdown(badge(report["status_display"],
                      REPORT_TONE.get(report["status"], "muted")),
                unsafe_allow_html=True)

    # The workflow, one legal step at a time.
    steps = {"draft": ("pending_review", "Submit for review"),
             "pending_review": ("verified", "Verify"),
             "verified": ("released", "Release to patient")}
    step = steps.get(report["status"])
    if step:
        target, label = step
        if st.button(label, key=f"rep_{report['id']}_{target}", type="primary",
                     use_container_width=True):
            res = api_request("POST",
                              f"radiology/reports/{report['id']}/status/",
                              {"status": target})
            if res is not None and res.status_code == 200:
                st.success(f"{label} done.")
                st.rerun()
            else:
                show_api_error(res, "Couldn't update the report.")
    elif report["status"] == "released":
        st.caption("Released — the patient and the ordering doctor can read it.")


def page_center_examinations():
    page_header("Examinations", "Today's studies and their progress.", "🖼️")
    only_today = st.toggle("Today only", value=True, key="exam_today")
    endpoint = "radiology/examinations/" + ("?when=today" if only_today else "")
    with st.spinner("Loading examinations…"):
        examinations = _get_json(endpoint)
    if examinations is None:
        show_api_error(None, "Couldn't load examinations.")
        return
    if not examinations:
        empty_state("No examinations" + (" today" if only_today else ""),
                    "Studies appear here as patients book them.", "🖼️")
        return

    for exam in examinations:
        appointment = exam["appointment"]
        patient = exam["patient"]
        order = exam.get("order") or {}
        with st.container(border=True):
            head, chip = st.columns([4, 1])
            with head:
                service = (exam.get("service") or {}).get("name") or "Imaging"
                st.markdown(
                    f"**{appointment['time']}** · {html.escape(service)} · "
                    f"{html.escape(patient.get('first_name') or patient.get('username'))}")
                if order.get("clinical_indication"):
                    st.caption(f"Indication: {order['clinical_indication']}")
                if order and order.get("is_self_requested"):
                    st.caption("Patient-requested (no doctor's referral).")
            with chip:
                st.markdown(badge(exam["status_display"],
                                  EXAM_TONE.get(exam["status"], "muted")),
                            unsafe_allow_html=True)
            _exam_actions(exam)
            with st.expander("Images and report"):
                _exam_files_panel(exam)
                st.divider()
                _exam_report_panel(exam)


def page_center_orders():
    page_header("Imaging Orders", "Requests you are fulfilling.", "🧾")
    orders = _get_json("radiology/orders/")
    if orders is None:
        show_api_error(None, "Couldn't load imaging orders.")
        return
    if not orders:
        empty_state("No imaging orders yet",
                    "Orders appear here once a patient books one of your "
                    "services against a doctor's request.", "🧾")
        return
    for order in orders:
        _order_card(order, show_patient=True)


def page_center_reports():
    page_header("Reports", "Draft, verify and release radiology reports.", "📄")
    reports = _get_json("radiology/reports/")
    if reports is None:
        show_api_error(None, "Couldn't load reports.")
        return
    if not reports:
        empty_state("No reports yet",
                    "Complete an examination to start its report.", "📄")
        return
    for report in reports:
        with st.container(border=True):
            head, chip = st.columns([4, 1])
            with head:
                st.markdown(f"**Report #{report['id']}** · "
                            f"examination {report['examination']}")
                if report.get("impression"):
                    st.caption(report["impression"][:160])
            with chip:
                st.markdown(badge(report["status_display"],
                                  REPORT_TONE.get(report["status"], "muted")),
                            unsafe_allow_html=True)
    st.caption("Write and release reports from the Examinations page, where "
               "the study and its images are in front of you.")


def page_center_images():
    page_header("Images", "Imaging attached to your examinations.", "🖼️")
    examinations = _get_json("radiology/examinations/")
    if examinations is None:
        show_api_error(None, "Couldn't load your examinations.")
        return
    with_files = [e for e in examinations if e.get("files")]
    if not with_files:
        empty_state("No images stored yet",
                    "Attach images to an examination and they appear here.",
                    "🖼️")
        return
    for exam in with_files:
        with st.container(border=True):
            patient = exam["patient"]
            st.markdown(f"**{(exam.get('service') or {}).get('name', 'Imaging')}**"
                        f" · {html.escape(patient.get('first_name') or patient.get('username'))}"
                        f" · {exam['appointment']['date']}")
            for stored in exam["files"]:
                _download_file_button(stored, f"ctr_{stored['id']}")


# ---------------------------------------------------------------------------
# Pharmacy — shared pieces
#
# The three portals show the same three things (a prescription, a shelf, a
# request), so each is rendered by one function used from every side. The
# vocabulary differs by role, not the layout.
# ---------------------------------------------------------------------------
REQUEST_TONE = {
    "pending": "warning",
    "confirmed": "brand",
    "preparing": "info",
    "ready": "success",
    "completed": "success",
    "cancelled": "muted",
    "rejected": "danger",
}

PRESCRIPTION_TONE = {
    "draft": "muted",
    "issued": "brand",
    "cancelled": "danger",
}


def _money(value):
    """Prices as the API sends them, or an honest dash.

    Never "0.00" for an unpriced line: a pharmacy that has not set a price has
    not said the medication is free.
    """
    return "—" if value in (None, "") else f"{float(value):,.2f} EGP"


def _medication_picker(key, label="Medication"):
    """Search the shared catalogue and pick one product.

    Deliberately a search over ``Medication`` rather than a free-text box: the
    whole module works because a prescription and a shelf point at the *same*
    row, and a typed name points at nothing.
    """
    term = st.text_input(f"{label} — search", key=f"{key}_term",
                         placeholder="Start typing, e.g. Amox")
    found = _get_json(f"pharmacy/medications/?q={term.strip()}") or []
    if not found:
        st.caption("No medication matches that. Add it to the catalog below if "
                   "it is missing." if term.strip() else
                   "Type a few letters to search the medication catalog.")
        return None
    options = {f"{m['label']} · {m['form_label']}": m for m in found}
    chosen = st.selectbox(label, list(options), key=f"{key}_pick")
    return options[chosen]


def _prescription_card(prescription, show_patient=False, key=""):
    """One prescription and its items, rendered the same way everywhere."""
    with st.container(border=True):
        head, chip = st.columns([4, 1])
        with head:
            who = (f" · {prescription['patient_name']}" if show_patient else "")
            doctor = prescription.get("doctor_name")
            st.markdown(f"**Prescription #{prescription['id']}**"
                        f"{html.escape(who)}")
            line = []
            if doctor and not show_patient:
                line.append(f"Dr. {doctor}")
            if prescription.get("diagnosis"):
                line.append(prescription["diagnosis"])
            line.append(prescription["created_at"][:10])
            st.caption(" · ".join(line))
        with chip:
            st.markdown(badge(prescription["status_label"],
                              PRESCRIPTION_TONE.get(prescription["status"],
                                                    "muted")),
                        unsafe_allow_html=True)

        for item in prescription["items"]:
            medication = item["medication"]
            st.markdown(f"**{html.escape(medication['label'])}** "
                        f"· {medication['form_label']}")
            detail = [d for d in (item.get("dosage"), item.get("frequency"),
                                  item.get("duration")) if d]
            detail.append(f"{item['quantity']} unit(s)")
            st.caption(" · ".join(detail))
            if item.get("instructions"):
                st.caption(f"ℹ️ {item['instructions']}")
    return prescription


def _availability_rows(entries, on_select=None, key=""):
    """The pharmacy comparison list a patient chooses from.

    Out-of-stock pharmacies are shown rather than filtered away: "this one does
    not have it" is an answer worth having when you are deciding where to go.
    """
    if not entries:
        empty_state("No pharmacies currently have this medication",
                    "No pharmacy on Roshada has stocked it yet. Try another "
                    "medication, or ask your doctor about an alternative.",
                    "🏥")
        return
    for entry in entries:
        with st.container(border=True):
            head, chip = st.columns([4, 1])
            with head:
                verified = " ✅" if entry.get("verified") else ""
                st.markdown(f"**{html.escape(entry['name'])}**{verified}")
                st.caption(html.escape(entry.get("address") or "")
                           or "No address on file")
                st.caption(f"{html.escape(entry['medication'])} · "
                           f"{_money(entry.get('price'))}")
            with chip:
                if entry["available"]:
                    st.markdown(badge("Available", "success"),
                                unsafe_allow_html=True)
                    st.caption(f"{entry['quantity_available']} unit(s)")
                else:
                    st.markdown(badge("Out of stock", "danger"),
                                unsafe_allow_html=True)
            if on_select is not None and entry["available"]:
                if st.button("Select this pharmacy",
                             key=f"sel_{key}_{entry['pharmacy_id']}",
                             use_container_width=True, type="primary"):
                    on_select(entry)


def _request_card(request, as_pharmacy=False, key=""):
    """One medication request, from either side of it."""
    with st.container(border=True):
        head, chip = st.columns([4, 1])
        with head:
            who = (request["patient_name"] if as_pharmacy
                   else request["pharmacy_name"])
            st.markdown(f"**Request #{request['id']}** · {html.escape(who)}")
            st.caption(f"Placed {request['created_at'][:16].replace('T', ' ')}")
        with chip:
            st.markdown(badge(request["status_label"],
                              REQUEST_TONE.get(request["status"], "muted")),
                        unsafe_allow_html=True)

        for item in request["items"]:
            medication = item["medication"]
            st.markdown(f"• **{html.escape(medication['label'])}** × "
                        f"{item['quantity']} — {_money(item.get('unit_price'))}")
            detail = [d for d in (item.get("dosage"), item.get("frequency"),
                                  item.get("duration")) if d]
            if detail:
                st.caption(" · ".join(detail))
        if request.get("total_price"):
            st.caption(f"Total: {_money(request['total_price'])}")

        reference = request.get("prescription_reference")
        if as_pharmacy:
            if reference:
                st.caption(f"Against prescription #{reference['id']}"
                           + (f", prescribed by Dr. {reference['prescribed_by']}"
                              if reference.get("prescribed_by") else ""))
            else:
                st.caption("No prescription — an over-the-counter request.")
        if request.get("pharmacy_note"):
            st.caption(f"Pharmacy: {html.escape(request['pharmacy_note'])}")
        if request.get("cancellation_reason"):
            st.caption(f"Reason: {html.escape(request['cancellation_reason'])}")

        _request_actions(request, as_pharmacy, key)


#: current status -> the steps that side may take next. Only legal moves are
#: offered; the API refuses the rest regardless of what the UI shows.
_PHARMACY_STEPS = {
    "pending": [("confirmed", "Confirm", "primary"),
                ("rejected", "Reject", "secondary")],
    "confirmed": [("preparing", "Start preparing", "primary"),
                  ("cancelled", "Cancel", "secondary")],
    "preparing": [("ready", "Mark ready for pickup", "primary"),
                  ("cancelled", "Cancel", "secondary")],
    "ready": [("completed", "Mark collected", "primary")],
}


def _request_actions(request, as_pharmacy, key):
    steps = (_PHARMACY_STEPS.get(request["status"], []) if as_pharmacy
             else ([("cancelled", "Cancel request", "secondary")]
                   if request["status"] in ("pending", "confirmed", "preparing",
                                            "ready") else []))
    if not steps:
        return
    columns = st.columns(len(steps))
    for column, (target, label, kind) in zip(columns, steps):
        with column:
            if st.button(label, key=f"req_{key}_{request['id']}_{target}",
                         type=kind, use_container_width=True):
                res = api_request(
                    "POST", f"pharmacy/requests/{request['id']}/status/",
                    {"status": target})
                if res is not None and res.status_code == 200:
                    st.rerun()
                else:
                    show_api_error(res, "Couldn't update that request.")


def _submit_request(pharmacy_id, items, prescription_id=None):
    payload = {"pharmacy_id": pharmacy_id, "items": items}
    if prescription_id:
        payload["prescription_id"] = prescription_id
    res = api_request("POST", "pharmacy/requests/", payload)
    if res is not None and res.status_code == 201:
        return True
    show_api_error(res, "Couldn't submit that request.")
    return False


# ---------------------------------------------------------------------------
# Patient — prescriptions and pharmacies
# ---------------------------------------------------------------------------
def _find_in_pharmacies_panel(prescription):
    """"Find in pharmacies", answered per medication.

    Each line is asked about separately, because a prescription is not assumed
    to be fillable at one pharmacy — and when it is not, saying so beats
    silently returning nothing.
    """
    lines = _get_json(f"pharmacy/prescriptions/{prescription['id']}/pharmacies/")
    if lines is None:
        show_api_error(None, "Couldn't check pharmacy availability.")
        return

    for line in lines:
        st.markdown(f"##### {html.escape(line['medication'])}")
        st.caption(f"{line['quantity']} unit(s) prescribed")

        def select(entry, line=line):
            if _submit_request(entry["pharmacy_id"],
                               [{"prescription_item_id": line["item_id"],
                                 "quantity": line["quantity"]}],
                               prescription_id=prescription["id"]):
                st.session_state["_rx_open"] = None
                st.success(f"✅ Request submitted to {entry['name']}. "
                           "They will confirm it shortly.")

        _availability_rows(line["pharmacies"], on_select=select,
                           key=f"rx{prescription['id']}i{line['item_id']}")
        st.divider()


def page_prescriptions():
    """The patient's prescriptions, and the route from one to a pharmacy."""
    page_header("Prescriptions", "Your medications, and where to fill them.",
                "💊")
    prescriptions = _get_json("pharmacy/prescriptions/")
    if prescriptions is None:
        show_api_error(None, "Couldn't load your prescriptions.")
        return
    if not prescriptions:
        empty_state("No prescriptions yet",
                    "Prescriptions your doctor writes appear here, with the "
                    "pharmacies that stock each medication.", "💊")
        return

    open_id = st.session_state.get("_rx_open")
    for prescription in prescriptions:
        _prescription_card(prescription, key=f"pat_{prescription['id']}")
        if prescription["status"] != "issued":
            continue
        if open_id == prescription["id"]:
            if st.button("← Close", key=f"close_rx_{prescription['id']}"):
                st.session_state["_rx_open"] = None
                st.rerun()
            _find_in_pharmacies_panel(prescription)
        elif st.button("🔎 Find in pharmacies",
                       key=f"find_rx_{prescription['id']}",
                       use_container_width=True, type="primary"):
            st.session_state["_rx_open"] = prescription["id"]
            st.rerun()


def _patient_find_medication_tab():
    """searching for a medication is not prescribing one."""
    st.caption("Search any medication to see which pharmacies stock it. "
               "Searching does not create a prescription.")
    medication = _medication_picker("find_med")
    if medication is None:
        return
    quantity = st.number_input("How many units?", min_value=1, value=1, step=1,
                               key="find_med_qty")
    entries = _get_json(f"pharmacy/availability/?medication={medication['id']}"
                        f"&quantity={int(quantity)}")
    if entries is None:
        show_api_error(None, "Couldn't check availability.")
        return

    def select(entry):
        if _submit_request(entry["pharmacy_id"],
                           [{"medication_id": medication["id"],
                             "quantity": int(quantity)}]):
            st.success(f"✅ Request submitted to {entry['name']}.")

    _availability_rows(entries, on_select=select, key="find")


def _patient_requests_tab():
    requests = _get_json("pharmacy/requests/")
    if requests is None:
        show_api_error(None, "Couldn't load your medication requests.")
        return
    if not requests:
        empty_state("No medication requests yet",
                    "Requests you send to a pharmacy appear here with their "
                    "status.", "🧾")
        return
    ready = [r for r in requests if r["status"] == "ready"]
    if ready:
        st.success(f"✅ {len(ready)} request(s) ready for pickup.")
    for request in requests:
        _request_card(request, key="pat")


def page_patient_pharmacy():
    page_header("Pharmacy", "Find medication and track your requests.", "💊")
    find, mine = st.tabs(["Find medication", "My requests"])
    with find:
        _patient_find_medication_tab()
    with mine:
        _patient_requests_tab()


# ---------------------------------------------------------------------------
# Doctor — prescribing
# ---------------------------------------------------------------------------
def _doctor_new_prescription_tab():
    # Only patients this doctor treats, built from their own appointments —
    # and the backend re-checks the relationship when the prescription is
    # submitted, so the list is convenience rather than authorization.
    patients = _get_json("pharmacy/prescribable-patients/") or []
    if not patients:
        empty_state("No patients yet",
                    "You can prescribe for patients you have appointments "
                    "with. Book or complete a consultation first.", "🧑‍⚕️")
        return

    options = {f"{p['name']} (@{p['username']})": p["id"] for p in patients}
    st.markdown("##### Add a medication")
    medication = _medication_picker("rx_new")

    draft = st.session_state.setdefault("_rx_draft", [])
    if medication is not None:
        c1, c2, c3 = st.columns(3)
        dosage = c1.text_input("Dose", key="rx_dose", placeholder="1 capsule")
        frequency = c2.text_input("Frequency", key="rx_freq",
                                  placeholder="3 times/day")
        duration = c3.text_input("Duration", key="rx_dur",
                                 placeholder="7 days")
        c4, c5 = st.columns([1, 3])
        quantity = c4.number_input("Quantity", min_value=1, value=1, step=1,
                                   key="rx_qty")
        instructions = c5.text_input("Instructions", key="rx_instr",
                                     placeholder="Take after food")
        if st.button("➕ Add to prescription", key="rx_add",
                     use_container_width=True):
            draft.append({"medication_id": medication["id"],
                          "label": medication["label"], "dosage": dosage,
                          "frequency": frequency, "duration": duration,
                          "quantity": int(quantity),
                          "instructions": instructions})
            st.rerun()

    with st.expander("Medication not in the catalog?"):
        c1, c2, c3 = st.columns(3)
        new_name = c1.text_input("Name", key="rx_new_name")
        new_strength = c2.text_input("Strength", key="rx_new_strength",
                                     placeholder="500 mg")
        forms = _get_json("pharmacy/dosage-forms/") or []
        form_labels = {f["label"]: f["value"] for f in forms}
        new_form = c3.selectbox("Form", list(form_labels) or ["Tablet"],
                                key="rx_new_form")
        if st.button("Add to catalog", key="rx_new_med"):
            if not new_name.strip():
                st.error("Name the medication.")
            else:
                res = api_request("POST", "pharmacy/medications/", {
                    "name": new_name.strip(), "strength": new_strength.strip(),
                    "form": form_labels.get(new_form, "tablet")})
                if res is not None and res.status_code in (200, 201):
                    st.success("✅ Added to the shared catalog.")
                else:
                    show_api_error(res, "Couldn't add that medication.")

    if not draft:
        st.caption("Add at least one medication to write the prescription.")
        return

    st.markdown("##### This prescription")
    for index, item in enumerate(draft):
        with st.container(border=True):
            row_a, row_b = st.columns([5, 1])
            with row_a:
                st.markdown(f"**{html.escape(item['label'])}** × {item['quantity']}")
                st.caption(" · ".join(d for d in (item["dosage"],
                                                  item["frequency"],
                                                  item["duration"]) if d)
                           or "No dosing detail given")
            with row_b:
                if st.button("Remove", key=f"rx_rm_{index}"):
                    draft.pop(index)
                    st.rerun()

    patient_label = st.selectbox("Patient", list(options), key="rx_patient")
    diagnosis = st.text_input("Diagnosis (optional)", key="rx_diag")
    notes = st.text_area("Notes for the patient (optional)", key="rx_notes")
    issue_now, save_draft = st.columns(2)
    for column, (label, issue, kind) in zip(
            (issue_now, save_draft),
            [("Issue prescription", True, "primary"),
             ("Save as draft", False, "secondary")]):
        with column:
            if st.button(label, key=f"rx_submit_{issue}", type=kind,
                         use_container_width=True):
                res = api_request("POST", "pharmacy/prescriptions/", {
                    "patient_id": options[patient_label],
                    "diagnosis": diagnosis, "notes": notes, "issue": issue,
                    "items": [{k: v for k, v in item.items() if k != "label"}
                              for item in draft]})
                if res is not None and res.status_code == 201:
                    st.session_state["_rx_draft"] = []
                    # No rerun: it would discard this confirmation before the
                    # doctor could read it.
                    st.success("✅ Prescription issued. The patient can now "
                               "find these medications in pharmacies."
                               if issue else
                               "✅ Saved as a draft. It is not visible to the "
                               "patient until you issue it.")
                else:
                    show_api_error(res, "Couldn't write that prescription.")


def _doctor_prescriptions_tab():
    prescriptions = _get_json("pharmacy/prescriptions/")
    if prescriptions is None:
        show_api_error(None, "Couldn't load your prescriptions.")
        return
    if not prescriptions:
        empty_state("No prescriptions yet",
                    "Prescriptions you write appear here with their status.",
                    "💊")
        return
    for prescription in prescriptions:
        _prescription_card(prescription, show_patient=True,
                           key=f"doc_{prescription['id']}")
        if prescription["status"] == "draft":
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Issue", key=f"iss_{prescription['id']}",
                             type="primary", use_container_width=True):
                    res = api_request(
                        "POST",
                        f"pharmacy/prescriptions/{prescription['id']}/status/",
                        {"status": "issued"})
                    if res is not None and res.status_code == 200:
                        st.rerun()
                    else:
                        show_api_error(res, "Couldn't issue that prescription.")
            with c2:
                if st.button("Cancel", key=f"can_{prescription['id']}",
                             use_container_width=True):
                    res = api_request(
                        "POST",
                        f"pharmacy/prescriptions/{prescription['id']}/status/",
                        {"status": "cancelled"})
                    if res is not None and res.status_code == 200:
                        st.rerun()
                    else:
                        show_api_error(res, "Couldn't cancel that prescription.")


def page_doctor_prescriptions():
    page_header("Prescriptions", "Write prescriptions and track what you issued.",
                "💊")
    st.caption("Roshada records what you prescribe. It does not check doses, "
               "suggest medications or flag interactions.")
    new, mine = st.tabs(["New prescription", "My prescriptions"])
    with new:
        _doctor_new_prescription_tab()
    with mine:
        _doctor_prescriptions_tab()


# ---------------------------------------------------------------------------
# Pharmacy portal
# ---------------------------------------------------------------------------
def _inventory_row(line):
    with st.container(border=True):
        head, chip = st.columns([4, 1])
        medication = line["medication"]
        with head:
            st.markdown(f"**{html.escape(medication['label'])}** · "
                        f"{medication['form_label']}")
            st.caption(f"{line['available_quantity']} available of "
                       f"{line['quantity']} on hand"
                       + (f" · {line['reserved']} reserved"
                          if line["reserved"] else "")
                       + f" · {_money(line.get('price'))}")
            st.caption(f"Updated {line['updated_at'][:16].replace('T', ' ')}")
        with chip:
            if not line["is_active"]:
                st.markdown(badge("Inactive", "muted"), unsafe_allow_html=True)
            elif not line["in_stock"]:
                st.markdown(badge("Out of stock", "danger"),
                            unsafe_allow_html=True)
            elif line["is_low_stock"]:
                st.markdown(badge("Low stock", "warning"),
                            unsafe_allow_html=True)
            else:
                st.markdown(badge("In stock", "success"),
                            unsafe_allow_html=True)

        with st.expander("Update this line"):
            c1, c2, c3 = st.columns(3)
            quantity = c1.number_input("Stock on hand", min_value=0,
                                       value=line["quantity"], step=1,
                                       key=f"inv_q_{line['id']}")
            price = c2.text_input("Price", value=str(line["price"] or ""),
                                  key=f"inv_p_{line['id']}",
                                  placeholder="e.g. 100.00")
            threshold = c3.number_input("Low-stock at", min_value=0,
                                        value=line["low_stock_threshold"],
                                        step=1, key=f"inv_t_{line['id']}")
            active = st.checkbox("Offered to patients", value=line["is_active"],
                                 key=f"inv_a_{line['id']}")
            if st.button("Save", key=f"inv_save_{line['id']}",
                         type="primary", use_container_width=True):
                payload = {"quantity": int(quantity), "is_active": active,
                           "low_stock_threshold": int(threshold)}
                payload["price"] = price.strip() or None
                res = api_request("PATCH", f"pharmacy/inventory/{line['id']}/",
                                  payload)
                if res is not None and res.status_code == 200:
                    st.rerun()
                else:
                    show_api_error(res, "Couldn't update that line.")


def page_pharmacy_inventory():
    page_header("Inventory", "The medications you stock, and how many.", "📦")
    with st.expander("➕ Add a medication to your shelf"):
        medication = _medication_picker("inv_add")
        c1, c2 = st.columns(2)
        quantity = c1.number_input("Stock on hand", min_value=0, value=0,
                                   step=1, key="inv_add_q")
        price = c2.text_input("Price", key="inv_add_p", placeholder="100.00")
        if st.button("Add to inventory", key="inv_add_go", type="primary",
                     use_container_width=True):
            if medication is None:
                st.error("Choose a medication first.")
            else:
                res = api_request("POST", "pharmacy/inventory/", {
                    "medication_id": medication["id"],
                    "quantity": int(quantity),
                    "price": price.strip() or None})
                if res is not None and res.status_code in (200, 201):
                    st.success("✅ Inventory updated.")
                    st.rerun()
                else:
                    show_api_error(res, "Couldn't update your inventory.")

    c1, c2 = st.columns([3, 2])
    term = c1.text_input("Search your stock", key="inv_search",
                         placeholder="Medication name")
    choice = c2.selectbox("Show", ["All", "In stock", "Out of stock"],
                          key="inv_filter")
    query = {"All": "", "In stock": "&availability=in_stock",
             "Out of stock": "&availability=out_of_stock"}[choice]

    lines = _get_json(f"pharmacy/inventory/?q={term.strip()}{query}")
    if lines is None:
        show_api_error(None, "Couldn't load your inventory.")
        return
    if not lines:
        empty_state("Nothing stocked yet" if not term.strip() else "No match",
                    "Add a medication above and patients searching for it will "
                    "find you.", "📦")
        return
    for line in lines:
        _inventory_row(line)


def page_pharmacy_requests():
    page_header("Medication Requests",
                "Requests from patients, and what you owe them.", "🧾")
    requests = _get_json("pharmacy/requests/")
    if requests is None:
        show_api_error(None, "Couldn't load your medication requests.")
        return
    if not requests:
        empty_state("No medication requests yet",
                    "When a patient chooses your pharmacy for a medication, "
                    "the request appears here.", "🧾")
        return

    pending = [r for r in requests if r["status"] == "pending"]
    if pending:
        st.warning(f"⏳ {len(pending)} request(s) waiting for your confirmation.")
    open_tab, done_tab = st.tabs(["Open", "Closed"])
    with open_tab:
        live = [r for r in requests
                if r["status"] in ("pending", "confirmed", "preparing", "ready")]
        if not live:
            empty_state("Nothing open", "Every request has been closed out.",
                        "✅")
        for request in live:
            _request_card(request, as_pharmacy=True, key="ph_open")
    with done_tab:
        closed = [r for r in requests
                  if r["status"] in ("completed", "cancelled", "rejected")]
        if not closed:
            empty_state("Nothing closed yet", "", "🗂️")
        for request in closed:
            _request_card(request, as_pharmacy=True, key="ph_done")


def page_pharmacy_prescriptions():
    """What a pharmacy is allowed to know about prescriptions.

    Not a list of prescriptions — a pharmacy has no prescription queryset at
    all. This is the set of prescription *references* attached to requests
    patients chose to bring here, which is the only prescribing information the
    permission model gives a pharmacy.
    """
    page_header("Prescriptions",
                "Prescription-backed requests patients brought to you.", "💊")
    st.caption("You see the medications a patient asked you to fill and a "
               "reference to the prescription behind them — not their medical "
               "record, and not the medications they are filling elsewhere.")
    requests = _get_json("pharmacy/requests/")
    if requests is None:
        show_api_error(None, "Couldn't load requests.")
        return
    backed = [r for r in requests if r.get("prescription_reference")]
    if not backed:
        empty_state("No prescription-backed requests yet",
                    "Requests a patient places against a doctor's prescription "
                    "appear here.", "💊")
        return
    for request in backed:
        _request_card(request, as_pharmacy=True, key="ph_rx")


def page_pharmacy_availability():
    """Pharmacies are not in the appointment engine.

    Dispensing is not a booked slot, so a pharmacy gets the profile-level
    switch and opening hours it always had — not an opening-hours grid that
    nothing would ever read.
    """
    page_header("Availability", "Control whether you are accepting work.", "🕐")
    _accepting_work_panel()
    st.caption("Pharmacies are not part of the appointment engine: medication "
               "is dispensed, not booked into a slot. If that changes, adding "
               "pharmacy to the bookable roles is the only step required.")


def page_service_catalogue():
    """The lab's Test Catalog / the centre's Imaging Services."""
    role = st.session_state.get("role")
    if role == role_defs.RADIOLOGY:
        page_header("Imaging Services", "The studies you offer.", "🩻")
    else:
        page_header("Test Catalog", "The tests you offer.", "🧪")
    _services_manager()


def page_provider_appointments():
    """The provider's own queue — same view for doctors, labs and centres."""
    page_header("Appointments", "Patients booked with you.", "🗓️")
    _render_appointments("appointments/provider/",
                         "Bookings will appear here as patients make them.",
                         "prov_appt_search", as_doctor=True)


# ===========================================================================
# Admin portal
# ===========================================================================
def page_admin_dashboard():
    page_header("Admin Dashboard", "Platform health at a glance.", "🛡️")
    summary = _dashboard_summary()
    if not summary:
        show_api_error(None, "Couldn't load platform statistics.")
        return

    stats = summary.get("stats", {})
    tiles = [
        ("total_users", "Total users", "👥"),
        ("total_appointments", "Appointments", "📅"),
        ("doctors_available", "Doctors available", "👨‍⚕️"),
        ("facilities_pending_verification", "Awaiting verification", "⏳"),
    ]
    columns = st.columns(len(tiles))
    for column, (key, label, icon) in zip(columns, tiles):
        with column:
            stat_card(label, _metric(stats, key), icon)

    st.markdown('<div style="height:1rem"></div>', unsafe_allow_html=True)
    left, right = st.columns(2)
    with left:
        _card_open()
        st.markdown("##### Accounts by role")
        by_role = summary.get("users_by_role", {})
        st.plotly_chart(
            _plotly_theme(go.Figure(go.Bar(
                x=[role_defs.label(r) for r in by_role],
                y=list(by_role.values()),
                marker_color=theme.CHART_COLORS[1])), height=260),
            use_container_width=True)
        _card_close()
    with right:
        _card_open()
        st.markdown("##### Appointments by status")
        by_status = summary.get("appointments_by_status", {})
        if by_status:
            st.plotly_chart(
                _plotly_theme(go.Figure(go.Pie(
                    labels=[s.replace("_", " ").title() for s in by_status],
                    values=list(by_status.values()), hole=.55)), height=260),
                use_container_width=True)
        else:
            empty_state("No appointments booked yet", "", "📅")
        _card_close()

    _card_open()
    st.markdown("##### Account status")
    by_status = summary.get("users_by_status", {})
    columns = st.columns(max(len(by_status), 1))
    for column, (state, count) in zip(columns, by_status.items()):
        with column:
            stat_card(state.title(), f"{count:,}", "🔐")
    _card_close()


def _admin_directory(title, subtitle, icon, role=None, empty=""):
    """One directory page, filtered to a role. Backs six sidebar entries."""
    def page():
        page_header(title, subtitle, icon)
        endpoint = f"admin/users/?role={role}" if role else "admin/users/"
        data = _get_json(endpoint)
        if data is None:
            show_api_error(None, "Couldn't load the directory.")
            return
        rows = data.get("results", [])
        if not rows:
            empty_state(empty or f"No {title.lower()} yet", "", icon)
            return

        query = (_consume_search() or "").lower()
        if query:
            rows = [r for r in rows
                    if query in (r.get("name", "") + r.get("username", "")).lower()]
            st.caption(f"Filtered by “{query}” — {len(rows)} match(es).")

        st.caption(f"Showing {len(rows)} of {data.get('total', len(rows))}.")
        st.dataframe(
            [{
                "Name": r.get("name"),
                "Username": r.get("username"),
                "Role": r.get("role_label"),
                "Status": r.get("status", "").title(),
                "Detail": r.get("detail") or "—",
                "Verified": ("✅" if r.get("verified") else "⏳")
                            if "verified" in r else "—",
                "Joined": (r.get("date_joined") or "")[:10],
            } for r in rows],
            use_container_width=True, hide_index=True)
    page.__name__ = f"page_admin_{(role or 'all')}"
    return page


# ---------------------------------------------------------------------------
# Medical Knowledge Base (administrators only)
#
# General medical reference material — never patient data. The backend refuses
# every other role, so these pages exist only in the admin portal.
# ---------------------------------------------------------------------------
SOURCE_TONE = {
    "pending": "warning",
    "approved": "success",
    "rejected": "danger",
    "archived": "muted",
}

KB_DOCUMENT_TONE = {
    "uploaded": "info",
    "processing": "warning",
    "processed": "success",
    "failed": "danger",
    "archived": "muted",
}


def _kb_vocabulary():
    if "_kb_vocab" not in st.session_state:
        st.session_state["_kb_vocab"] = _get_json("knowledge/vocabulary/") or {}
    return st.session_state["_kb_vocab"]


def _kb_choice(field, label, key, default_index=0):
    """A select built from the backend's vocabulary, never a hardcoded list."""
    options = {item["label"]: item["value"]
               for item in _kb_vocabulary().get(field, [])}
    if not options:
        return None
    chosen = st.selectbox(label, list(options), key=key,
                          index=min(default_index, len(options) - 1))
    return options[chosen]


def _source_card(source):
    with st.container(border=True):
        head, chip = st.columns([4, 1])
        with head:
            st.markdown(f"**{html.escape(source['name'])}**")
            detail = [source["source_type_label"]]
            if source.get("organization"):
                detail.append(html.escape(source["organization"]))
            if source.get("specialty"):
                detail.append(source["specialty"])
            detail.append(f"{source['document_count']} document(s)")
            st.caption(" · ".join(detail))
            if source.get("url"):
                st.caption(html.escape(source["url"]))
            if source.get("review_notes"):
                st.caption(f"Review: {html.escape(source['review_notes'])}")
        with chip:
            st.markdown(badge(source["status_label"],
                              SOURCE_TONE.get(source["verification_status"],
                                              "muted")),
                        unsafe_allow_html=True)

        # Only the transitions the backend will accept are offered; it refuses
        # the rest regardless of what is drawn here.
        allowed = {
            "pending": [("approved", "Approve", "primary"),
                        ("rejected", "Reject", "secondary")],
            "approved": [("archived", "Archive", "secondary")],
            "rejected": [("pending", "Send back for review", "secondary")],
            "archived": [("approved", "Re-approve", "primary")],
        }.get(source["verification_status"], [])
        if not allowed:
            return
        columns = st.columns(len(allowed))
        for column, (target, label_text, kind) in zip(columns, allowed):
            with column:
                if st.button(label_text, key=f"src_{source['id']}_{target}",
                             type=kind, use_container_width=True):
                    res = api_request(
                        "POST", f"knowledge/sources/{source['id']}/review/",
                        {"status": target})
                    if res is not None and res.status_code == 200:
                        st.rerun()
                    else:
                        show_api_error(res, "Couldn't update that source.")


def page_kb_sources():
    page_header("Knowledge Sources",
                "Organisations whose material may be retrieved.", "📚")
    st.caption("Only **approved** sources are retrievable. A source starts "
               "pending and stays unusable until an administrator reviews it.")

    with st.expander("➕ Register a source"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", key="kb_src_name",
                             placeholder="e.g. WHO Cardiovascular")
        organization = c2.text_input("Organization", key="kb_src_org")
        c3, c4 = st.columns(2)
        with c3:
            source_type = _kb_choice("source_types", "Type", "kb_src_type")
        specialty = c4.text_input("Specialty", key="kb_src_spec",
                                  placeholder="e.g. cardiology")
        url = st.text_input("URL", key="kb_src_url")
        description = st.text_area("Description", key="kb_src_desc")
        if st.button("Register source", key="kb_src_go", type="primary",
                     use_container_width=True):
            if not name.strip():
                st.error("Name the source.")
            else:
                res = api_request("POST", "knowledge/sources/", {
                    "name": name.strip(), "organization": organization,
                    "source_type": source_type or "other",
                    "specialty": specialty, "url": url,
                    "description": description})
                if res is not None and res.status_code == 201:
                    st.success("✅ Registered. It is pending review.")
                    st.rerun()
                else:
                    show_api_error(res, "Couldn't register that source.")

    c1, c2 = st.columns([3, 2])
    term = c1.text_input("Search sources", key="kb_src_search")
    statuses = {item["label"]: item["value"]
                for item in _kb_vocabulary().get("source_statuses", [])}
    choice = c2.selectbox("Status", ["All"] + list(statuses), key="kb_src_filter")
    query = f"?q={term.strip()}"
    if choice != "All":
        query += f"&status={statuses[choice]}"

    sources = _get_json(f"knowledge/sources/{query}")
    if sources is None:
        show_api_error(None, "Couldn't load knowledge sources.")
        return
    if not sources:
        empty_state("No knowledge sources yet",
                    "Register one above, then approve it before uploading "
                    "documents.", "📚")
        return
    for source in sources:
        _source_card(source)


def _kb_document_card(document):
    with st.container(border=True):
        head, chip = st.columns([4, 1])
        with head:
            live = "" if document["is_active"] else " (superseded)"
            st.markdown(f"**{html.escape(document['title'])}** "
                        f"v{document['version']}{live}")
            detail = [document["document_type_label"],
                      html.escape(document["source_name"] or "no source"),
                      f"{document['chunk_count']} chunk(s)",
                      document["language"]]
            st.caption(" · ".join(d for d in detail if d))
            st.caption(f"Identity: {html.escape(document['source'])}")
            if document.get("error_message"):
                st.error(document["error_message"])
            if document["is_retrievable"]:
                st.markdown(badge("Retrievable", "success"),
                            unsafe_allow_html=True)
            else:
                st.caption("Not retrievable — needs an approved source, a "
                           "processed status and to be the live version.")
        with chip:
            st.markdown(badge(document["status_label"],
                              KB_DOCUMENT_TONE.get(document["status"],
                                                   "muted")),
                        unsafe_allow_html=True)

        actions = [("reindex", "Re-index", "secondary")]
        actions.append(("archive", "Restore" if document["status"] == "archived"
                        else "Archive", "secondary"))
        columns = st.columns(len(actions))
        for column, (action, label_text, kind) in zip(columns, actions):
            with column:
                if st.button(label_text, key=f"kbd_{document['id']}_{action}",
                             type=kind, use_container_width=True):
                    payload = ({"restore": True}
                               if action == "archive"
                               and document["status"] == "archived" else {})
                    res = api_request(
                        "POST",
                        f"knowledge/documents/{document['id']}/{action}/",
                        payload)
                    if res is not None and res.status_code == 200:
                        st.rerun()
                    else:
                        show_api_error(res, f"Couldn't {label_text.lower()}.")


def page_kb_documents():
    page_header("Knowledge Documents",
                "Reference material in the retrieval corpus.", "📄")
    supported = ", ".join(_kb_vocabulary().get("supported_uploads", []))
    st.caption(f"Roshada extracts text from {supported or '.txt and .md'} "
               "only — it has no PDF or DOCX extraction, so those are refused "
               "rather than stored unindexed.")

    sources = _get_json("knowledge/sources/?status=approved") or []
    with st.expander("➕ Add a document"):
        if not sources:
            st.caption("Approve a knowledge source first — a document can only "
                       "be added under one.")
        else:
            options = {s["name"]: s["id"] for s in sources}
            c1, c2 = st.columns(2)
            source_name = c1.selectbox("Source", list(options), key="kb_doc_src")
            title = c2.text_input("Title", key="kb_doc_title")
            identity = st.text_input(
                "Identity", key="kb_doc_identity",
                placeholder="A stable key, e.g. who/hypertension")
            c3, c4 = st.columns(2)
            with c3:
                document_type = _kb_choice("document_types", "Document type",
                                           "kb_doc_type")
            with c4:
                content_type = _kb_choice("content_types", "Format",
                                          "kb_doc_format")
            uploaded = st.file_uploader("Upload .txt or .md",
                                        type=["txt", "md", "markdown", "text"],
                                        key="kb_doc_file")
            text = st.text_area("…or paste the text", key="kb_doc_text",
                                height=140)
            if st.button("Add and index", key="kb_doc_go", type="primary",
                         use_container_width=True):
                if not identity.strip():
                    st.error("Give the document a stable identity.")
                elif uploaded is None and not text.strip():
                    st.error("Upload a file or paste the text.")
                else:
                    payload = {"source_id": options[source_name],
                               "identity": identity.strip(),
                               "title": title.strip() or identity.strip(),
                               "document_type": document_type or "reference",
                               "source_type": content_type or "text"}
                    if uploaded is not None:
                        res = api_request(
                            "POST", "knowledge/documents/", payload,
                            files={"file": _upload_part(uploaded)})
                    else:
                        payload["text"] = text
                        res = api_request("POST", "knowledge/documents/",
                                          payload)
                    if res is not None and res.status_code == 201:
                        st.success("✅ Added and indexed.")
                        st.rerun()
                    else:
                        show_api_error(res, "Couldn't add that document.")

    c1, c2 = st.columns([3, 2])
    term = c1.text_input("Search documents", key="kb_doc_search")
    statuses = {item["label"]: item["value"]
                for item in _kb_vocabulary().get("document_statuses", [])}
    choice = c2.selectbox("Status", ["All"] + list(statuses), key="kb_doc_filter")
    query = f"?limit=25&q={term.strip()}"
    if choice != "All":
        query += f"&status={statuses[choice]}"

    payload = _get_json(f"knowledge/documents/{query}")
    if payload is None:
        show_api_error(None, "Couldn't load knowledge documents.")
        return
    if not payload["results"]:
        empty_state("No documents yet",
                    "Add reference material above. It is chunked, embedded and "
                    "indexed as soon as it is accepted.", "📄")
        return
    for document in payload["results"]:
        _kb_document_card(document)
    if payload.get("has_more"):
        st.caption(f"Showing {payload['count']} of {payload['total']}. "
                   "Narrow the search to see the rest.")


def page_kb_index():
    page_header("Knowledge Index",
                "What is indexed, and what may actually be retrieved.", "🧮")
    status = _get_json("knowledge/index/")
    if status is None:
        show_api_error(None, "Couldn't load the index status.")
        return

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        stat_card("Documents", status["documents"], "📄", "var(--brand-50)",
                  "var(--brand)")
    with c2:
        stat_card("Retrievable", status["retrievable_documents"], "✅",
                  "rgba(18,183,106,.12)", "var(--success)")
    with c3:
        stat_card("Chunks", status["chunks"], "🧩", "rgba(79,110,247,.12)",
                  "var(--accent)")
    with c4:
        stat_card("Sources", status["sources"], "📚", "rgba(247,144,9,.14)",
                  "var(--warning)")
    st.markdown('<div style="height:1rem"></div>', unsafe_allow_html=True)

    embedding = status.get("embedding") or {}
    with st.container(border=True):
        st.markdown("##### Embedding configuration")
        if embedding.get("error"):
            st.error(embedding["error"])
        else:
            st.markdown(
                f"**{embedding.get('embedder', '—')}** · model "
                f"`{embedding.get('model', '—')}` · "
                f"{embedding.get('dimension', '—')} dimensions")
            if embedding.get("semantic"):
                st.caption("Semantic embeddings are in use.")
            else:
                st.caption("Offline lexical embeddings — these match shared "
                           "words, not meaning. Set an embedding API key for "
                           "semantic retrieval, then re-index.")
        st.caption("Provider and model come from the environment. Changing "
                   "them requires a re-index: vectors from two different "
                   "models are not comparable.")

    with st.container(border=True):
        st.markdown("##### Documents by status")
        by_status = status.get("documents_by_status") or {}
        if not by_status:
            st.caption("Nothing indexed yet.")
        for state, count in sorted(by_status.items()):
            st.markdown(
                badge(f"{state}: {count}", KB_DOCUMENT_TONE.get(state, "muted")),
                unsafe_allow_html=True)

    spaces = (status.get("index") or {}).get("spaces") or []
    with st.container(border=True):
        st.markdown("##### Vector spaces")
        if not spaces:
            st.caption("No vectors stored yet.")
        for space in spaces:
            st.markdown(f"`{space['embedder']}:{space['embedding_model']}` "
                        f"· {space['dimension']} dims · "
                        f"{space['chunks']} chunk(s)")
        st.caption("Vectors live in the application database alongside "
                   "everything else — one store, one backup, one access story.")


def _rag_answer_panel(query, top_k, specialty, language):
    """The grounded-answer debug surface.

    An administrator's tool for inspecting what the pipeline produces — not a
    chat. There is no conversation, no history and no patient context, and the
    page says so rather than letting the shape imply otherwise.
    """
    payload = {"query": query.strip(), "top_k": int(top_k), "debug": True}
    if specialty.strip():
        payload["specialty"] = specialty.strip()
    if language.strip():
        payload["language"] = language.strip()

    with st.spinner("Retrieving and generating…"):
        res = api_request("POST", "knowledge/rag/query/", payload)
    if res is None:
        show_api_error(None, "Couldn't run that query.")
        return
    if res.status_code != 200:
        show_api_error(res, "Couldn't run that query.")
        return

    data = res.json()
    signals = data.get("retrieval") or {}

    if data.get("degraded"):
        # Every degraded reason is a real state, not an error to hide: no
        # approved material, no provider configured, or an answer that failed
        # a safety check. Each says which.
        reason = data.get("reason", "")
        st.warning(f"No grounded answer was generated — `{reason}`.")
    with st.container(border=True):
        st.markdown("##### Answer")
        st.write(data.get("answer", ""))

    for warning in data.get("warnings") or []:
        st.warning(warning)
    if data.get("fabricated_citations"):
        st.error(f"The model cited source(s) "
                 f"{data['fabricated_citations']} that were never provided. "
                 f"The answer is shown unedited so the overclaim is visible.")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        stat_card("Matches", signals.get("matches", 0), "🎯",
                  "var(--brand-50)", "var(--brand)")
    with c2:
        stat_card("Sources sent", signals.get("sources_used", 0), "📎",
                  "rgba(79,110,247,.12)", "var(--accent)")
    with c3:
        stat_card("Top score", f"{signals.get('top_score', 0):.3f}", "📈",
                  "rgba(18,183,106,.12)", "var(--success)")
    with c4:
        stat_card("Latency", f"{data.get('latency_ms', 0)} ms", "⏱️",
                  "rgba(247,144,9,.14)", "var(--warning)")
    st.caption(f"Match strength: {signals.get('match_strength', '—')} · "
               f"{signals.get('note', '')}")

    sources = data.get("sources") or []
    if sources:
        st.markdown("##### Sources the answer was built from")
        for source in sources:
            with st.container(border=True):
                head, chip = st.columns([4, 1])
                with head:
                    st.markdown(f"**[{source['n']}] "
                                f"{html.escape(source['reference'])}**")
                    trail = [f"Source: {source['source_name'] or '—'}"]
                    if source.get("section"):
                        trail.append(f"Section: {source['section']}")
                    if source.get("score") is not None:
                        trail.append(f"score {source['score']:.3f}")
                    st.caption(" · ".join(trail))
                with chip:
                    st.markdown(
                        badge("Cited" if source.get("cited") else "Not cited",
                              "success" if source.get("cited") else "muted"),
                        unsafe_allow_html=True)

    if data.get("provider"):
        st.caption(f"Generated by {data['provider']} · {data.get('model')} · "
                   f"prompt {data.get('prompt_version')}")


def page_kb_search():
    page_header("Knowledge Retrieval",
                "Check what the corpus returns, and what it answers with.",
                "🔍")
    st.caption("An administrator's debug surface. This is not the patient AI "
               "assistant: there is no conversation, no memory and no patient "
               "data — only the approved medical corpus.")

    c1, c2 = st.columns([4, 1])
    query = c1.text_input("Query", key="kb_q",
                          placeholder="e.g. What are common symptoms of hypertension?")
    top_k = c2.number_input("Results", min_value=1, max_value=20, value=5,
                            key="kb_k")
    c3, c4 = st.columns(2)
    specialty = c3.text_input("Specialty (optional)", key="kb_spec")
    language = c4.text_input("Language (optional)", key="kb_lang")

    mode = st.radio(
        "Mode", ["Passages only", "Grounded answer"], horizontal=True,
        key="kb_mode",
        help="Passages only runs retrieval. Grounded answer additionally "
             "sends them to the configured model and verifies its citations.")

    if mode == "Grounded answer":
        if not query.strip():
            empty_state("Ask the corpus a question",
                        "The retrieved passages are sent to the configured "
                        "model, which must answer from them alone.", "💬")
            return
        _rag_answer_panel(query, top_k, specialty, language)
        return

    if not query.strip():
        empty_state("Search the corpus",
                    "Type a question to see which passages would be retrieved "
                    "and where they came from.", "🔍")
        return

    parts = [f"q={query.strip()}", f"top_k={int(top_k)}"]
    if specialty.strip():
        parts.append(f"specialty={specialty.strip()}")
    if language.strip():
        parts.append(f"language={language.strip()}")

    res = api_request("GET", "knowledge/search/?" + "&".join(parts))
    if res is None:
        show_api_error(None, "Couldn't run that search.")
        return
    if res.status_code == 503:
        # The corpus exists but is unreadable — a different thing from "no
        # results", and the fix is a re-index rather than a better query.
        st.warning(res.json().get("error", "The corpus needs re-indexing."))
        return
    if res.status_code != 200:
        show_api_error(res, "Couldn't run that search.")
        return

    payload = res.json()
    coverage = payload.get("coverage", {})
    st.caption(f"Searching {coverage.get('documents', 0)} retrievable "
               f"document(s) from {coverage.get('sources', 0)} approved "
               f"source(s).")
    if not payload["results"]:
        empty_state("No matching passages",
                    "Nothing in the approved corpus matches that closely.",
                    "🔍")
        return

    for index, hit in enumerate(payload["results"]):
        with st.container(border=True):
            head, chip = st.columns([4, 1])
            with head:
                st.markdown(f"**{html.escape(hit['reference'])}**")
            with chip:
                st.markdown(badge(f"score {hit['score']:.3f}", "info"),
                            unsafe_allow_html=True)
            # A retrieved chunk is Markdown *source*, so rendering it would
            # draw its `##` headings as page headings and the passage would
            # read as a document rather than as a quotation. `<pre>` is the
            # reliable escape: markdown does not reprocess the contents of a
            # raw HTML block, which a plain escaped <div> does not prevent.
            # Explicit <br>: Streamlit's markdown pass collapses the newlines
            # before the browser ever sees them, so pre-wrap alone is not
            # enough to keep the passage's line structure.
            passage = html.escape(hit["text"]).replace("\n", "<br>")
            st.markdown(
                f"<div style='word-break:break-word;color:var(--ink);"
                f"font-size:.92rem;line-height:1.55'>{passage}</div>",
                unsafe_allow_html=True)
            provenance = hit["provenance"]
            trail = [f"Source: {provenance['source_name'] or '—'}",
                     f"Document: {provenance['document_title']} "
                     f"v{provenance['document_version']}"]
            if hit["section"]:
                trail.append(f"Section: {hit['section']}")
            if hit.get("page"):
                trail.append(f"Page: {hit['page']}")
            st.caption(" · ".join(trail))
            if provenance.get("source_url"):
                st.caption(html.escape(provenance["source_url"]))


def page_admin_permissions():
    """The live permission matrix, read from the backend rather than restated.

    What is drawn here is the same ``accounts.roles.PERMISSIONS`` the API
    enforces, so this page cannot drift into describing access the platform does
    not actually grant.
    """
    page_header("Permissions", "What each role is allowed to do.", "🔐")
    summary = _dashboard_summary()
    matrix = summary.get("permissions", {})
    planned = set(summary.get("planned_capabilities", []))
    if not matrix:
        show_api_error(None, "Couldn't load the permission matrix.")
        return

    capabilities = sorted({c for caps in matrix.values() for c in caps})
    st.dataframe(
        [{"Capability": c + ("  (planned)" if c in planned else ""),
          **{role_defs.label(r): ("✅" if c in matrix.get(r, []) else "—")
             for r in role_defs.ALL_ROLES}}
         for c in capabilities],
        use_container_width=True, hide_index=True)
    st.caption("“Planned” capabilities are declared in the role matrix but have "
               "no endpoint yet — the Laboratory, Radiology and Pharmacy domains "
               "are a later task. Everything else is enforced by the API on "
               "every request.")


def page_admin_appointments():
    """Aggregates only — an administrator does not need to see who saw whom."""
    page_header("Appointments", "Platform-wide booking activity.", "📅")
    summary = _dashboard_summary()
    by_status = summary.get("appointments_by_status", {})
    if not by_status:
        empty_state("No appointments booked yet",
                    "Booking activity will appear here.", "📅")
        return
    columns = st.columns(len(by_status))
    for column, (state, count) in zip(columns, by_status.items()):
        with column:
            stat_card(state.replace("_", " ").title(), f"{count:,}", "📅")
    st.caption("Individual appointments are intentionally not listed here: "
               "administering the platform does not require reading who is "
               "seeing which doctor.")


def page_settings():
    page_header("Settings", "Account and preferences.", "⚙️")
    _card_open()
    st.markdown("##### Account")
    st.write(f"Signed in as **{st.session_state.get('user_name') or 'User'}** "
             f"({role_defs.label(st.session_state.get('role'))})")
    st.caption("Theme: Roshada Light (premium healthcare)")
    _card_close()
    st.markdown('<div style="height:1rem"></div>', unsafe_allow_html=True)
    if st.button("🚪 Log out", use_container_width=True):
        handle_logout()


# ===========================================================================
# Navigation
# ===========================================================================
# ---------------------------------------------------------------------------
# Navigation is declared as data: (section title, [(label, icon, page fn), …]).
# Grouping turns one long list into scannable sections, and adding or moving a
# page is a one-line change here rather than an edit to the render code.
#
# Icons are Material Symbols, which Streamlit renders natively — one family, one
# stroke weight, no extra dependency.
# ---------------------------------------------------------------------------
PATIENT_NAV_SECTIONS = [
    ("Overview", [
        ("Dashboard", "dashboard", page_patient_dashboard),
    ]),
    ("Appointments", [
        ("Find Doctors", "search", page_find_doctors),
        ("Book Appointment", "event_available", page_book_appointment),
        ("My Appointments", "event_note", page_my_appointments),
    ]),
    ("Diagnostics", [
        ("Laboratory", "science", page_patient_laboratory),
        ("Radiology", "monitor_heart", page_patient_radiology),
        ("Pharmacy", "local_pharmacy", page_patient_pharmacy),
    ]),
    ("Health & AI", [
        ("AI Assistant", "smart_toy", page_ai_assistant),
    ]),
    ("Records", [
        ("Medical Records", "folder_open", page_medical_records),
        ("Prescriptions", "medication", page_prescriptions),
    ]),
    ("Communication", [
        ("Messages", "chat", page_messages),
        ("Notifications", "notifications", page_notifications),
    ]),
    ("Account", [
        ("Profile", "account_circle", page_profile),
        ("Settings", "settings", page_settings),
    ]),
]

# Doctor nav mirrors the Dashboard.png sidebar (doctor-facing pages).
DOCTOR_NAV_SECTIONS = [
    ("Overview", [
        ("Dashboard", "dashboard", page_doctor_dashboard),
    ]),
    ("Appointments", [
        ("Appointments", "event_note", page_doctor_schedule),
        ("Availability", "schedule", page_provider_availability),
        ("Patients", "groups", _placeholder_page(
            "Patients", "The patients under your care.", "groups",
            "No patient roster yet",
            "Patients you have appointments with appear under Appointments. A "
            "dedicated roster arrives with the consultation records.")),
    ]),
    ("Clinical", [
        ("Consultations", "description", _placeholder_page(
            "Consultations", "Visit notes and outcomes.", "description",
            "No consultation notes yet",
            "Recording a consultation is part of the clinical records work, "
            "which is a later task. You can already close a visit as completed "
            "or no-show from Appointments.")),
        ("Lab Orders", "science", _placeholder_page(
            "Lab Orders", "Tests you have requested.", "science",
            "No lab orders yet",
            "Ordering tests from a laboratory arrives with the Laboratory "
            "system. Your permission to create them is already in place.")),
        ("Radiology Orders", "monitor_heart", page_doctor_radiology_orders),
    ]),
    ("Health & AI", [
        ("AI Assistant", "smart_toy", page_ai_assistant),
    ]),
    ("Records", [
        ("Medical Records", "folder_open", page_medical_records),
        ("Prescriptions", "medication", page_doctor_prescriptions),
    ]),
    ("Communication", [
        ("Messages", "chat", page_messages),
        ("Notifications", "notifications", page_notifications),
    ]),
    ("Account", [
        ("Profile", "account_circle", page_profile),
    ]),
]

# ---------------------------------------------------------------------------
# Provider portals.
#
# The three share a shape — a dashboard, a work queue, a catalogue, availability
# and an account section — because they are the same kind of participant in the
# platform. Only the vocabulary differs (tests / studies / prescriptions), which
# is exactly the difference the labels carry.
# ---------------------------------------------------------------------------
LABORATORY_NAV_SECTIONS = [
    ("Overview", [
        ("Dashboard", "dashboard", page_facility_dashboard),
    ]),
    ("Operations", [
        ("Appointments", "event_note", page_provider_appointments),
        ("Orders", "receipt_long", _placeholder_page(
            "Orders", "Test requests sent by doctors.", "receipt_long",
            "No lab orders yet",
            "Doctors will be able to send test requests here once the "
            "Laboratory system is built.")),
        ("Samples", "biotech", _placeholder_page(
            "Samples", "Specimens received and in process.", "biotech",
            "No samples logged yet",
            "Sample tracking arrives with the Laboratory system.")),
        ("Results", "checklist", _placeholder_page(
            "Results", "Completed results ready to release.", "checklist",
            "No results yet",
            "Publishing results to patients and doctors arrives with the "
            "Laboratory system.")),
    ]),
    ("Catalog", [
        ("Test Catalog", "science", page_service_catalogue),
        ("Availability", "schedule", page_provider_availability),
    ]),
    ("Communication", [
        ("Notifications", "notifications", page_notifications),
    ]),
    ("Account", [
        ("Profile", "account_circle", page_profile),
    ]),
]

RADIOLOGY_NAV_SECTIONS = [
    ("Overview", [
        ("Dashboard", "dashboard", page_facility_dashboard),
    ]),
    ("Operations", [
        ("Appointments", "event_note", page_provider_appointments),
        ("Imaging Orders", "receipt_long", page_center_orders),
        ("Examinations", "biotech", page_center_examinations),
        ("Images", "image", page_center_images),
        ("Reports", "summarize", page_center_reports),
    ]),
    ("Catalog", [
        ("Imaging Services", "monitor_heart", page_service_catalogue),
        ("Availability", "schedule", page_provider_availability),
    ]),
    ("Communication", [
        ("Notifications", "notifications", page_notifications),
    ]),
    ("Account", [
        ("Profile", "account_circle", page_profile),
    ]),
]

PHARMACY_NAV_SECTIONS = [
    ("Overview", [
        ("Dashboard", "dashboard", page_facility_dashboard),
    ]),
    ("Operations", [
        ("Medication Requests", "receipt_long", page_pharmacy_requests),
        ("Prescriptions", "medication", page_pharmacy_prescriptions),
        ("Delivery / Pickup", "local_shipping", _placeholder_page(
            "Delivery / Pickup", "How patients receive their medication.",
            "local_shipping", "Pickup only, for now",
            "Requests you mark ready are collected at your counter. Roshada "
            "has no delivery or payment system, so none is simulated here.")),
    ]),
    ("Inventory", [
        ("Inventory", "inventory_2", page_pharmacy_inventory),
        ("Availability", "schedule", page_pharmacy_availability),
    ]),
    ("Communication", [
        ("Notifications", "notifications", page_notifications),
    ]),
    ("Account", [
        ("Profile", "account_circle", page_profile),
    ]),
]

ADMIN_NAV_SECTIONS = [
    ("Overview", [
        ("Dashboard", "dashboard", page_admin_dashboard),
    ]),
    ("People", [
        ("Users", "manage_accounts", _admin_directory(
            "Users", "Every account on the platform.", "manage_accounts")),
        ("Doctors", "stethoscope", _admin_directory(
            "Doctors", "Registered clinicians.", "stethoscope",
            role=role_defs.DOCTOR)),
        ("Patients", "groups", _admin_directory(
            "Patients", "Registered patients.", "groups",
            role=role_defs.PATIENT)),
    ]),
    ("Providers", [
        ("Laboratories", "science", _admin_directory(
            "Laboratories", "Registered laboratories.", "science",
            role=role_defs.LABORATORY,
            empty="No laboratories registered yet")),
        ("Radiology Centers", "monitor_heart", _admin_directory(
            "Radiology Centers", "Registered imaging centers.", "monitor_heart",
            role=role_defs.RADIOLOGY,
            empty="No radiology centers registered yet")),
        ("Pharmacies", "local_pharmacy", _admin_directory(
            "Pharmacies", "Registered pharmacies.", "local_pharmacy",
            role=role_defs.PHARMACY,
            empty="No pharmacies registered yet")),
    ]),
    ("Knowledge Base", [
        ("Sources", "library_books", page_kb_sources),
        ("Documents", "description", page_kb_documents),
        ("Retrieval", "search", page_kb_search),
        ("Index Status", "database", page_kb_index),
    ]),
    ("Platform", [
        ("Appointments", "event_note", page_admin_appointments),
        ("Reports", "assessment", _placeholder_page(
            "Reports", "Operational and clinical reporting.", "assessment",
            "No reports built yet",
            "The figures on the Dashboard are live. Exportable reports are a "
            "later task.")),
        ("Permissions", "verified_user", page_admin_permissions),
        ("Audit Logs", "history", _placeholder_page(
            "Audit Logs", "Who did what, and when.", "history",
            "Audit logging is not enabled yet",
            "Roshada writes application logs but does not yet store a queryable "
            "audit trail. Nothing is shown here rather than showing a partial "
            "one that could be mistaken for complete.")),
        ("System Settings", "tune", _placeholder_page(
            "System Settings", "Platform configuration.", "tune",
            "Configuration lives in the environment",
            "Roshada is configured through environment variables and the Django "
            "admin site, not from this page.")),
    ]),
    ("Account", [
        ("Profile", "account_circle", page_profile),
    ]),
]


def _flatten(sections):
    """Flat [(label, icon, page_fn)] — the shape the router and helpers use."""
    return [item for _title, items in sections for item in items]


#: role -> its grouped navigation. Adding a seventh portal is one entry here.
ROLE_NAV_SECTIONS = {
    role_defs.PATIENT: PATIENT_NAV_SECTIONS,
    role_defs.DOCTOR: DOCTOR_NAV_SECTIONS,
    role_defs.LABORATORY: LABORATORY_NAV_SECTIONS,
    role_defs.RADIOLOGY: RADIOLOGY_NAV_SECTIONS,
    role_defs.PHARMACY: PHARMACY_NAV_SECTIONS,
    role_defs.ADMIN: ADMIN_NAV_SECTIONS,
}

# Kept for the existing router/helpers (_nav_labels, _first_valid, page lookup).
PATIENT_NAV = _flatten(PATIENT_NAV_SECTIONS)
DOCTOR_NAV = _flatten(DOCTOR_NAV_SECTIONS)
ROLE_NAV = {role: _flatten(sections)
            for role, sections in ROLE_NAV_SECTIONS.items()}


def _nav_sections(role):
    return ROLE_NAV_SECTIONS.get(role, PATIENT_NAV_SECTIONS)


def _toggle_sidebar():
    st.session_state["sidebar_collapsed"] = not st.session_state.get("sidebar_collapsed", False)


def render_sidebar(role, current):
    """The navigation shell: brand · grouped nav · user card · log out.

    Nav items are ordinary Streamlit buttons, so they are real <button>
    elements: keyboard reachable, focusable, and readable by screen readers.
    """
    collapsed = st.session_state.get("sidebar_collapsed", False)
    if collapsed:
        theme.sidebar_collapsed_css()

    with st.sidebar:
        # ---- Brand + collapse control ----
        if collapsed:
            theme.sidebar_brand(collapsed=True, size=26)
            st.button("Expand sidebar", icon=":material/keyboard_double_arrow_right:",
                      key="sb_toggle", help="Expand sidebar", on_click=_toggle_sidebar)
        else:
            brand_col, toggle_col = st.columns([1, 0.22], vertical_alignment="center")
            with brand_col:
                theme.sidebar_brand(size=26)
            with toggle_col:
                st.button("Collapse sidebar", icon=":material/keyboard_double_arrow_left:",
                          key="sb_toggle", help="Collapse sidebar", on_click=_toggle_sidebar)

        # ---- Grouped navigation ----
        # Its own container so it can absorb the free vertical space, which is
        # what keeps the footer against the bottom of the column.
        with st.container(key="rs_nav"):
            for title, items in _nav_sections(role):
                theme.sidebar_section_label(title)
                for label, icon, _page in items:
                    is_active = label == current
                    if st.button(
                        label,
                        icon=f":material/{icon}:",
                        key=f"nav_{label}",
                        # kind="primary" is what the CSS keys the active state off.
                        type="primary" if is_active else "secondary",
                        use_container_width=True,
                        # In icon-only mode the label is off-screen, so the
                        # native tooltip is the only visible affordance.
                        help=label if collapsed else None,
                    ):
                        goto(label)

        # ---- Footer: user identity + log out, anchored to the bottom ----
        # `margin-top:auto` on this container is what pins it, so the nav length
        # can change without the footer drifting up the column.
        with st.container(key="rs_footer"):
            display = _display_name(role)
            # Real signed-in details; falls back to the role when no email is set.
            secondary = st.session_state.get("user_email") or role_defs.label(role)
            theme.sidebar_user(display, secondary, collapsed=collapsed)

            if st.button("Log Out", icon=":material/logout:", key="sb_logout",
                         use_container_width=True,
                         help="Log Out" if collapsed else None):
                handle_logout()


def render_dashboard_shell():
    role = st.session_state.get("role")
    nav = _nav_for(role)
    labels = [n[0] for n in nav]

    if st.session_state.get("nav_current") not in labels:
        st.session_state["nav_current"] = labels[0]
    # honour a programmatic navigation request (goto) from any button/card
    goto_label = st.session_state.pop("_goto", None)
    if goto_label in labels:
        st.session_state["nav_current"] = goto_label

    choice = st.session_state["nav_current"]
    render_sidebar(role, choice)

    # Shared top bar on EVERY page — one reusable component, role-dynamic content.
    # The dashboard is the one page whose title names the portal you are in.
    title = f"{role_defs.label(role)} Dashboard" if choice == "Dashboard" else choice
    render_topbar(title, role)

    page_fn = {n[0]: n[2] for n in nav}[choice]
    page_fn()


# ===========================================================================
# Entry point
# ===========================================================================
if not st.session_state["token"]:
    render_auth()
else:
    render_dashboard_shell()
