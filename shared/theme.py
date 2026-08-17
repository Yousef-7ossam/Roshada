"""Roshada design system.

A single source of truth for the premium-healthcare look: design tokens, the
global CSS injected into Streamlit, the brand logo, and reusable HTML component
helpers (cards, badges, stat tiles, empty states…). All dynamic text passed to
the HTML helpers is escaped, so there is no XSS surface.
"""
import base64
import html
from functools import lru_cache
from pathlib import Path

import streamlit as st

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
FAVICON_PATH = str(ASSETS_DIR / "favicon.png")

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------
TOKENS = {
    # Brand
    "brand": "#0EA98F",
    "brand_dark": "#0B7C6B",
    "brand_50": "#E9F8F4",
    "accent": "#4F6EF7",       # secondary (info/links, chart accent)
    # Neutrals
    "ink": "#0F1B2D",
    "muted": "#64748B",
    "bg": "#F4F7FB",
    "surface": "#FFFFFF",
    "border": "#E7ECF3",
    "subtle": "#F1F5FA",
    # Semantic
    "success": "#12B76A",
    "warning": "#F79009",
    "danger": "#F04438",
    "info": "#2E90FA",
    # Radius / shadow
    "radius_card": "20px",
    "radius_ctl": "12px",
}

# Categorical palette for charts (accessible, on-brand)
CHART_COLORS = ["#0EA98F", "#4F6EF7", "#F79009", "#12B76A", "#2E90FA", "#F04438"]

# ---------------------------------------------------------------------------
# Sidebar palette
#
# Deliberately derived from the existing identity rather than replacing it: the
# navy gradient is the shell the app already shipped, and the active state uses
# TOKENS["accent"] (#4F6EF7), the established Roshada indigo. Only the neutrals
# are new, and they exist to give the dark surface a proper text hierarchy.
# ---------------------------------------------------------------------------
SB_BG_TOP = "#0F3A57"          # existing sidebar gradient, unchanged
SB_BG_BOTTOM = "#0B2334"
SB_BORDER = "rgba(255,255,255,.09)"
SB_TEXT = "rgba(255,255,255,.80)"        # nav labels
SB_TEXT_STRONG = "#FFFFFF"               # active / hover / headings
SB_TEXT_FAINT = "rgba(255,255,255,.58)"  # section labels, secondary meta
SB_ICON = "rgba(255,255,255,.66)"
SB_HOVER = "rgba(255,255,255,.08)"
SB_ACTIVE_A = TOKENS["accent"]           # #4F6EF7 — brand indigo
SB_ACTIVE_B = "#3B5BDB"
SB_FOCUS = "#9DB9FF"                     # visible focus ring on dark navy
SB_ONLINE = TOKENS["success"]
SB_DANGER = "#FF7A70"                    # danger, lightened for contrast on navy


def _css():
    t = TOKENS
    return f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root {{
  --brand: {t['brand']}; --brand-dark: {t['brand_dark']}; --brand-50: {t['brand_50']};
  --accent: {t['accent']}; --ink: {t['ink']}; --muted: {t['muted']};
  --bg: {t['bg']}; --surface: {t['surface']}; --border: {t['border']}; --subtle: {t['subtle']};
  --success: {t['success']}; --warning: {t['warning']}; --danger: {t['danger']}; --info: {t['info']};
  --r-card: {t['radius_card']}; --r-ctl: {t['radius_ctl']};
  --shadow-sm: 0 1px 2px rgba(15,27,45,.05);
  --shadow: 0 6px 22px rgba(15,27,45,.07);
  --shadow-lg: 0 18px 44px rgba(15,27,45,.12);
}}

html, body, [class*="css"], .stApp {{ font-family: 'Inter', system-ui, sans-serif; }}
.stApp {{ background: var(--bg); color: var(--ink); }}

/* Desktop-first (1920 reference): use horizontal space, minimal vertical padding */
.block-container {{ padding: 1rem 2.2rem 1.4rem !important; max-width: 1680px; }}

h1, h2, h3, h4 {{ color: var(--ink); font-weight: 700; letter-spacing: -0.01em; }}
h1 {{ font-size: 1.7rem; }} h2 {{ font-size: 1.35rem; }} h3 {{ font-size: 1.1rem; }}

/* ---- 12-column layout: equal-height cards + tighter, consistent gaps ---- */
[data-testid="stHorizontalBlock"] {{ align-items: stretch; gap: 1rem; }}
[data-testid="stHorizontalBlock"] > [data-testid="column"] {{ display:flex; flex-direction:column; }}
[data-testid="column"] > [data-testid="stVerticalBlockBorderWrapper"] {{ height:100%; }}
[data-testid="column"] > [data-testid="stVerticalBlockBorderWrapper"] > div {{ height:100%; }}
[data-testid="stVerticalBlock"] {{ gap: .75rem; }}

/* ---- Sidebar: always full viewport height, footer anchored to bottom ---- */
section[data-testid="stSidebar"] {{
  background: var(--surface); border-right: 1px solid var(--border);
  box-shadow: var(--shadow-sm); width: 266px !important; min-width: 266px !important; }}
section[data-testid="stSidebar"] > div:first-child {{ height: 100vh; }}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
  height: calc(100vh - 2rem); display: flex; flex-direction: column; }}
section[data-testid="stSidebar"] .block-container {{ padding-top: 1.1rem; }}
.rs-sb-spacer {{ flex: 1 1 auto; min-height: .5rem; }}

/* ---- Buttons ---- */
.stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {{
  border-radius: var(--r-ctl);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--ink);
  font-weight: 600;
  padding: 0.55rem 1.1rem;
  box-shadow: var(--shadow-sm);
  transition: transform .15s ease, box-shadow .15s ease, background .15s ease;
}}
.stButton > button:hover, .stFormSubmitButton > button:hover {{
  transform: translateY(-1px); box-shadow: var(--shadow); border-color: var(--brand);
}}
/* Primary buttons (form submit + type=primary) */
.stFormSubmitButton > button, .stButton > button[kind="primary"] {{
  background: linear-gradient(135deg, var(--brand), var(--brand-dark));
  color: #fff; border: none;
}}
.stFormSubmitButton > button:hover, .stButton > button[kind="primary"]:hover {{
  filter: brightness(1.04); box-shadow: 0 10px 24px rgba(14,169,143,.35);
}}

/* ---- Inputs ---- */
.stTextInput input, .stNumberInput input, .stTextArea textarea,
div[data-baseweb="select"] > div, .stDateInput input, .stTimeInput input {{
  border-radius: var(--r-ctl) !important;
  border: 1px solid var(--border) !important;
  background: var(--surface) !important;
}}
.stTextInput input:focus, .stNumberInput input:focus, .stTextArea textarea:focus {{
  border-color: var(--brand) !important;
  box-shadow: 0 0 0 3px rgba(14,169,143,.15) !important;
}}
label, .stMarkdown p {{ color: var(--ink); }}

/* ---- Forms as cards ---- */
div[data-testid="stForm"] {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r-card);
  padding: 1.5rem 1.5rem 0.5rem;
  box-shadow: var(--shadow);
}}

/* ---- Bordered containers become soft cards ---- */
div[data-testid="stVerticalBlockBorderWrapper"] {{
  border-radius: var(--r-card) !important;
  border-color: var(--border) !important;
  box-shadow: var(--shadow-sm);
  background: var(--surface);
}}

/* ---- Metrics ---- */
div[data-testid="stMetric"] {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r-card);
  padding: 1.1rem 1.2rem;
  box-shadow: var(--shadow-sm);
}}
div[data-testid="stMetricLabel"] {{ color: var(--muted); }}

/* ---- Tabs ---- */
.stTabs [data-baseweb="tab-list"] {{ gap: 6px; }}
.stTabs [data-baseweb="tab"] {{
  border-radius: 10px; padding: 8px 16px; color: var(--muted); font-weight: 600;
}}
.stTabs [aria-selected="true"] {{ background: var(--brand-50); color: var(--brand-dark); }}

/* ---- Alerts ---- */
div[data-testid="stAlert"] {{ border-radius: 14px; border: 1px solid var(--border); }}

/* ---- Chat ---- */
[data-testid="stChatMessage"] {{ background: var(--surface); border-radius: 16px; box-shadow: var(--shadow-sm); }}

/* ---- Tables ---- */
[data-testid="stDataFrame"] {{ border-radius: 14px; overflow: hidden; border: 1px solid var(--border); }}

/* ---- Expander ---- */
div[data-testid="stExpander"] {{ border-radius: 14px; border: 1px solid var(--border); box-shadow: var(--shadow-sm); }}

/* Hide Streamlit chrome for an app-like feel */
#MainMenu {{ visibility: hidden; }} footer {{ visibility: hidden; }}
header[data-testid="stHeader"] {{ background: transparent; }}

/* ---- Design-system components (HTML helpers) ---- */
.rs-logo {{ display:flex; align-items:center; gap:10px; padding: 4px 4px 2px; }}
.rs-logo .wordmark {{ font-weight:800; font-size:1.35rem; color:var(--ink); letter-spacing:-0.02em; }}
.rs-logo .wordmark b {{ color: var(--brand); }}

.rs-pageheader {{ margin: 0 0 1.4rem; }}
.rs-pageheader .title {{ display:flex; align-items:center; gap:12px; }}
.rs-pageheader .ic {{ width:44px; height:44px; border-radius:14px; display:flex; align-items:center;
  justify-content:center; font-size:22px; background:var(--brand-50); }}
.rs-pageheader h1 {{ margin:0; font-size:1.6rem; }}
.rs-pageheader .sub {{ color:var(--muted); margin:4px 0 0 56px; font-size:.95rem; }}

.rs-card {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--r-card);
  padding:1rem 1.15rem; box-shadow:var(--shadow); height:100%; }}

/* Compact, equal-height KPI tiles */
.rs-stat {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--r-card);
  padding:.85rem 1rem; box-shadow:var(--shadow-sm); height:100%; min-height:104px; }}
.rs-stat .top {{ display:flex; align-items:center; justify-content:space-between; }}
.rs-stat .ic {{ width:38px; height:38px; border-radius:11px; display:flex; align-items:center;
  justify-content:center; font-size:18px; }}
.rs-stat .label {{ color:var(--muted); font-size:.82rem; font-weight:600; margin-top:.55rem; }}
.rs-stat .value {{ font-size:1.65rem; font-weight:800; color:var(--ink); line-height:1.1; }}
.rs-stat .delta {{ font-size:.8rem; font-weight:600; }}

/* Hero / welcome banner (approved gradient) */
.rs-hero {{ position:relative; overflow:hidden; border-radius:var(--r-card); color:#fff;
  padding:1.4rem 1.7rem; box-shadow:var(--shadow);
  background:linear-gradient(135deg,#2F6BE4 0%,#2AA7C9 62%,#7B5FD0 120%); }}
.rs-hero h2 {{ color:#fff; font-size:1.55rem; font-weight:800; margin:0 0 .25rem; }}
.rs-hero p {{ color:rgba(255,255,255,.92); margin:0 0 .9rem; font-size:.95rem; }}
.rs-hero .cta {{ display:inline-block; background:#fff; color:#12294A; font-weight:700;
  padding:.5rem 1.2rem; border-radius:12px; box-shadow:var(--shadow-sm); }}
.rs-hero .illus {{ position:absolute; right:22px; top:50%; transform:translateY(-50%);
  opacity:.95; pointer-events:none; }}
@media (max-width:1100px) {{ .rs-hero .illus {{ display:none; }} }}

.rs-badge {{ display:inline-flex; align-items:center; gap:6px; padding:3px 11px; border-radius:999px;
  font-size:.78rem; font-weight:600; line-height:1.6; }}

.rs-doctor {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--r-card);
  padding:1.15rem 1.25rem; box-shadow:var(--shadow-sm); transition:transform .15s, box-shadow .15s; }}
.rs-doctor:hover {{ transform:translateY(-2px); box-shadow:var(--shadow); }}
.rs-doctor .row {{ display:flex; align-items:center; gap:14px; }}
.rs-avatar {{ width:52px; height:52px; border-radius:16px; display:flex; align-items:center;
  justify-content:center; font-weight:700; color:#fff; font-size:1.1rem;
  background:linear-gradient(135deg,var(--brand),var(--accent)); flex:none; }}
.rs-doctor .name {{ font-weight:700; color:var(--ink); }}
.rs-doctor .spec {{ color:var(--muted); font-size:.88rem; }}

.rs-appt {{ background:var(--surface); border:1px solid var(--border); border-left:4px solid var(--brand);
  border-radius:16px; padding:1rem 1.2rem; box-shadow:var(--shadow-sm); margin-bottom:.8rem; }}
.rs-appt .when {{ font-weight:700; color:var(--ink); }}
.rs-appt .meta {{ color:var(--muted); font-size:.88rem; margin-top:2px; }}

.rs-empty {{ text-align:center; padding:3rem 1rem; background:var(--surface);
  border:1px dashed var(--border); border-radius:var(--r-card); }}
.rs-empty .ic {{ font-size:2.6rem; }}
.rs-empty .t {{ font-weight:700; color:var(--ink); margin-top:.6rem; font-size:1.05rem; }}
.rs-empty .d {{ color:var(--muted); margin-top:.3rem; }}

/* ============================================================= */
/* Dashboard — dark shell, header, banner, widget cards */
/* ============================================================= */

/* =============================================================
   SIDEBAR — premium dark navigation shell
   Navigation items are real <button> elements (not an embedded
   component), so they are keyboard-reachable, expose an accessible
   name, and can be styled with the tokens below.
   ============================================================= */
section[data-testid="stSidebar"] {{
  background: linear-gradient(180deg,{SB_BG_TOP} 0%,{SB_BG_BOTTOM} 100%) !important;
  border-right: 1px solid {SB_BORDER} !important;
  box-shadow: 4px 0 24px rgba(8,20,33,.20) !important; }}
/* Hide Streamlit's chrome for an app-like shell — but only the noise. Hiding
   the whole stToolbar also hid the mobile "open sidebar" button that lives
   inside it, leaving no way back to navigation on a phone. */
[data-testid="stDecoration"], .stDeployButton, .stAppDeployButton,
[data-testid*="Deploy"], [data-testid="stToolbarActions"],
[data-testid="stStatusWidget"], [data-testid="stMainMenu"] {{ display:none !important; }}
[data-testid="stHeader"] {{ background: transparent !important; }}

/* Tighten the default vertical rhythm so the whole nav fits without scrolling */
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: .12rem; }}
section[data-testid="stSidebar"] .block-container {{
  padding: .85rem .85rem 1rem !important; }}
section[data-testid="stSidebar"] [data-testid="stSidebarHeader"] {{
  padding-bottom: 0 !important; height: 0; min-height: 0; }}

/* Streamlit reserves 96px of bottom padding here, which showed up as dead space
   under the footer. Reclaim it and keep a normal gutter instead. */
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
  padding-bottom: .9rem !important; }}
/* Carry the full column height down to the nav list. Streamlit puts a plain
   display:block div between stSidebarUserContent and the vertical block, which
   otherwise stops the footer from being pushed to the bottom. */
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div {{
  flex: 1 1 auto !important; display: flex !important; flex-direction: column !important;
  min-height: 0 !important; }}
/* min-height:0 on every flex ancestor: a flex item defaults to min-height:auto,
   which refuses to shrink below its content — that stopped the nav from ever
   scrolling and pushed the footer off short screens. */
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div
  > [data-testid="stVerticalBlock"] {{
  flex: 1 1 auto !important; min-height: 0 !important; }}
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {{
  overflow: hidden !important; }}
/* Footer (profile + log out) sits at the bottom whatever the nav length.
   st.container adds a stLayoutWrapper around the keyed block, and that wrapper
   — not the keyed block — is the flex item, so it carries the auto margin. */
section[data-testid="stSidebar"] .st-key-rs_footer,
section[data-testid="stSidebar"] [data-testid="stLayoutWrapper"]:has(> .st-key-rs_footer) {{
  margin-top: auto !important; }}
/* The nav block absorbs the free space, which pins the footer even where the
   auto margin cannot resolve (a wrapper in the chain is not a flex item).
   min-height:0 + overflow-y let the *list* scroll on a short viewport instead of
   pushing Log Out off the bottom of the screen. */
section[data-testid="stSidebar"] .st-key-rs_nav,
section[data-testid="stSidebar"] [data-testid="stLayoutWrapper"]:has(> .st-key-rs_nav) {{
  flex: 1 1 auto !important; min-height: 0 !important; }}
section[data-testid="stSidebar"] .st-key-rs_nav {{
  overflow-y: auto; overflow-x: hidden; scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,.22) transparent; }}
section[data-testid="stSidebar"] .st-key-rs_nav::-webkit-scrollbar {{ width: 6px; }}
section[data-testid="stSidebar"] .st-key-rs_nav::-webkit-scrollbar-thumb {{
  background: rgba(255,255,255,.20); border-radius: 3px; }}
/* The footer must never shrink away when the nav is scrolling. */
section[data-testid="stSidebar"] [data-testid="stLayoutWrapper"]:has(> .st-key-rs_footer) {{
  flex: 0 0 auto !important; }}
/* Clear the brand divider so the first section label never sits on the line. */
section[data-testid="stSidebar"] [data-testid="stLayoutWrapper"]:has(> .st-key-rs_nav) {{
  margin-top: .8rem; }}

/* ---- Brand header ---- */
/* The divider is a border on the brand block rather than a separate element:
   as its own element it sat in a sibling container whose box overlapped the
   first section label and painted a line through it. */
.rs-sb-brand {{ display:flex; align-items:center; gap:.55rem;
  padding:.1rem .3rem .55rem; border-bottom:1px solid {SB_BORDER}; }}
.rs-sb-brand .mark {{ flex:0 0 auto; display:flex; }}
.rs-sb-brand .word {{ font-size:1.12rem; font-weight:800; letter-spacing:-.02em;
  color:{SB_TEXT_STRONG}; white-space:nowrap; }}

/* ---- Section labels ----
   z-index keeps them above neighbouring blocks: Streamlit's negative element
   margins otherwise let the preceding block paint over the label's top half. */
.rs-sb-sec {{ font-size:.63rem; font-weight:700; letter-spacing:.1em; text-transform:uppercase;
  color:{SB_TEXT_FAINT}; padding:.5rem .6rem .18rem; position:relative; z-index:2;
  line-height:1.1; }}
/* Streamlit's markdown container does not grow to fit this label — it collapses
   to a few pixels, so the next nav button was drawn on top of the text. Reserve
   the height explicitly. */
section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.rs-sb-sec) {{
  height: auto !important; min-height: 1.55rem !important; }}

/* ---- Navigation items ----
   Descendant (not child) selectors: Streamlit wraps the <button> in an extra
   div inside .stButton, so `.stButton > button` does not match. */
section[data-testid="stSidebar"] .stButton,
section[data-testid="stSidebar"] .stButton > div {{ width:100%; }}
section[data-testid="stSidebar"] .stButton button {{
  width:100%; justify-content:flex-start; gap:.6rem;
  background: transparent !important; border: 1px solid transparent !important;
  box-shadow: none !important; color:{SB_TEXT} !important;
  font-size:.86rem; font-weight:600; line-height:1.15;
  padding:.4rem .6rem !important; border-radius:10px; min-height:34px;
  transition: background-color .18s ease, color .18s ease, border-color .18s ease; }}
/* Streamlit centres the icon+label inside two nested wrappers, so aligning the
   <button> alone leaves every icon at a different x depending on label length.
   Stretch the wrappers and align there. */
section[data-testid="stSidebar"] .stButton button > div,
section[data-testid="stSidebar"] .stButton button span[data-has-shortcut] {{
  width:100% !important; display:flex !important; align-items:center;
  justify-content:flex-start !important; gap:.6rem; }}
section[data-testid="stSidebar"] .stButton button p {{
  font-size:.86rem; font-weight:600; margin:0; text-align:left; }}
section[data-testid="stSidebar"] .stButton button [data-testid="stIconMaterial"] {{
  font-size:18px !important; width:18px; color:{SB_ICON} !important;
  font-variation-settings:'wght' 400 !important; transition: color .18s ease; }}
section[data-testid="stSidebar"] .stButton button:hover {{
  background:{SB_HOVER} !important; color:{SB_TEXT_STRONG} !important;
  border-color: transparent !important; transform:none; }}
section[data-testid="stSidebar"] .stButton button:hover [data-testid="stIconMaterial"] {{
  color:{SB_TEXT_STRONG} !important; }}
section[data-testid="stSidebar"] .stButton button:focus-visible {{
  outline:2px solid {SB_FOCUS} !important; outline-offset:2px; }}

/* Active item — kind="primary" marks the current page */
section[data-testid="stSidebar"] .stButton button[kind="primary"] {{
  background: linear-gradient(135deg,{SB_ACTIVE_A},{SB_ACTIVE_B}) !important;
  color:#FFFFFF !important; font-weight:700;
  border-color: rgba(255,255,255,.18) !important;
  box-shadow: 0 6px 18px rgba(79,110,247,.30) !important; }}
section[data-testid="stSidebar"] .stButton button[kind="primary"] [data-testid="stIconMaterial"] {{
  color:#FFFFFF !important; }}
section[data-testid="stSidebar"] .stButton button[kind="primary"]:hover {{
  filter: brightness(1.07); }}

/* ---- User profile card ---- */
.rs-sb-user {{ display:flex; align-items:center; gap:.55rem; padding:.45rem .55rem;
  border:1px solid {SB_BORDER}; border-radius:12px; background:rgba(255,255,255,.035); }}
.rs-sb-user .av {{ position:relative; flex:0 0 auto; width:32px; height:32px; border-radius:50%;
  background:linear-gradient(135deg,{SB_ACTIVE_A},{SB_ACTIVE_B}); color:#fff;
  display:flex; align-items:center; justify-content:center; font-weight:700; font-size:.78rem; }}
.rs-sb-user .av::after {{ content:""; position:absolute; right:-1px; bottom:-1px;
  width:9px; height:9px; border-radius:50%; background:{SB_ONLINE};
  border:2px solid {SB_BG_BOTTOM}; }}
.rs-sb-user .meta {{ min-width:0; }}
.rs-sb-user .nm {{ font-size:.82rem; font-weight:700; color:{SB_TEXT_STRONG}; line-height:1.2;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.rs-sb-user .sub {{ font-size:.7rem; color:{SB_TEXT_FAINT}; line-height:1.2;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
/* Same collapse as the section labels: Streamlit sizes both the markdown wrapper
   and its element container to the card's text, ignoring its padding, border and
   avatar — the card overflowed by 16px and Log Out was drawn *over* it. Size both
   wrappers to their content instead (fit-content, not a hardcoded height, so the
   card can change without this breaking again). */
section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.rs-sb-user) {{
  /* avatar + vertical padding + borders — the card's own box, so this tracks
     the values above rather than being a magic number. (fit-content does not
     resolve on this flex item, hence the explicit calc.) */
  height: auto !important;
  min-height: calc(32px + .9rem + 2px) !important; }}

/* ---- Footer rhythm ----
   The footer inherits the tight .12rem nav gap, which leaves the profile card
   and Log Out visually stuck together. Give this block its own spacing, and a
   padding-top so the card never touches the last nav item once the auto margin
   collapses to zero on a short viewport. */
section[data-testid="stSidebar"] .st-key-rs_footer {{
  gap: 1rem !important; padding-top: .95rem; }}

/* ---- Log out — visually distinct, never dominant ----
   Each st.button gets an `st-key-<key>` class on its element container, so the
   button key alone is enough to target; no wrapper container is needed. */
section[data-testid="stSidebar"] .st-key-sb_logout button {{
  color:{SB_DANGER} !important; border:1px solid rgba(240,68,56,.26) !important; }}
section[data-testid="stSidebar"] .st-key-sb_logout button [data-testid="stIconMaterial"] {{
  color:{SB_DANGER} !important; }}
section[data-testid="stSidebar"] .st-key-sb_logout button:hover {{
  background: rgba(240,68,56,.12) !important; color:#FFA79F !important;
  border-color: rgba(240,68,56,.45) !important; }}
section[data-testid="stSidebar"] .st-key-sb_logout button:hover [data-testid="stIconMaterial"] {{
  color:#FFA79F !important; }}

/* ---- Collapse toggle ----
   Icon-only by design, but the label stays in the DOM (moved off-screen) so the
   button keeps an accessible name instead of being announced as unlabelled. */
section[data-testid="stSidebar"] .st-key-sb_toggle button {{
  min-height:34px; width:34px !important; padding:0 !important;
  justify-content:center; color:{SB_TEXT_FAINT} !important; border-radius:10px; }}
section[data-testid="stSidebar"] .st-key-sb_toggle button [data-testid="stMarkdownContainer"] {{
  position:absolute !important; width:1px !important; height:1px !important;
  overflow:hidden !important; clip-path: inset(50%) !important; white-space:nowrap !important; }}
section[data-testid="stSidebar"] .st-key-sb_toggle button:hover {{
  background:{SB_HOVER} !important; color:{SB_TEXT_STRONG} !important; }}
section[data-testid="stSidebar"] .st-key-sb_toggle button [data-testid="stIconMaterial"] {{
  font-size:19px !important; }}

/* ---- Sidebar visibility / responsive ------------------------------------
   >=768px the sidebar is pinned open and collapsing is handled by our own
   toggle (below), so Streamlit's native control is hidden to avoid two
   competing controls. Under 768px we hand control back to Streamlit, whose
   off-canvas drawer + backdrop is the native mobile pattern.               */
@media (min-width: 768px) {{
  section[data-testid="stSidebar"],
  section[data-testid="stSidebar"][aria-expanded="false"] {{
    transform: none !important; visibility: visible !important;
    margin-left: 0 !important; left: 0 !important; }}
  [data-testid="stSidebarCollapseButton"] {{ display: none !important; }}
}}
/* Below 768px the sidebar is an off-canvas drawer, so the reopen control is the
   only way back to navigation. Streamlit renders it inside a zero-height header
   wrapper, which collapses it to 0x0 — position:fixed lifts it out of that box
   and pins it as a normal mobile menu button. */
@media (max-width: 767px) {{
  section[data-testid="stSidebar"] {{ width: min(84vw, 300px) !important;
    min-width: min(84vw, 300px) !important; }}
  [data-testid="stToolbar"] {{ display: flex !important; }}
  /* Icon-only collapsing is a desktop affordance; on a drawer it is meaningless
     and the native menu button already covers open/close. */
  section[data-testid="stSidebar"] .st-key-sb_toggle {{ display: none !important; }}
  [data-testid="stSidebarCollapsedControl"], [data-testid="stExpandSidebarButton"] {{
    display: flex !important; visibility: visible !important; opacity: 1 !important;
    position: fixed !important; top: .55rem; left: .55rem; z-index: 1001 !important;
    width: 40px !important; height: 40px !important; min-width: 40px !important;
    align-items: center; justify-content: center; color: #fff !important;
    background: {SB_BG_BOTTOM} !important; border-radius: 10px;
    box-shadow: 0 2px 10px rgba(8,20,33,.28); }}
  /* Clear the fixed button so it never covers the page title. */
  [data-testid="stMain"] .block-container {{ padding-top: 3.4rem !important; }}
}}

/* Top header bar */
.rs-topbar {{ display:flex; align-items:center; gap:1.5rem; margin:.1rem 0 1.2rem; }}
.rs-topbar .ttl {{ font-size:1.95rem; font-weight:800; color:var(--ink); letter-spacing:-.02em; white-space:nowrap; }}
.rs-search {{ position:relative; flex:1 1 auto; max-width:600px; margin:0 auto; }}
.rs-search .box {{ width:100%; border:1px solid var(--border); background:var(--surface);
  border-radius:14px; padding:.72rem 1rem .72rem 2.7rem; font-size:.95rem; color:#9AA7B8;
  box-shadow:var(--shadow-sm); }}
.rs-search svg {{ position:absolute; left:1rem; top:50%; transform:translateY(-50%); }}
.rs-hact {{ display:flex; align-items:center; gap:1.15rem; margin-left:auto; }}
.rs-bell {{ position:relative; color:#5B6B80; display:flex; }}
.rs-bell .dot {{ position:absolute; top:-6px; right:-6px; background:var(--danger); color:#fff;
  font-size:.6rem; font-weight:700; min-width:16px; height:16px; border-radius:999px;
  display:flex; align-items:center; justify-content:center; padding:0 3px; border:2px solid var(--bg); }}
.rs-help {{ border-left:1px solid var(--border); padding-left:1.1rem; color:#475569;
  font-size:.82rem; line-height:1.25; }}
.rs-help b {{ display:block; color:#475569; font-weight:600; }}
.rs-hava {{ display:flex; align-items:center; gap:5px; }}
.rs-hava .a {{ width:40px; height:40px; border-radius:50%;
  background:linear-gradient(135deg,var(--brand),var(--accent));
  color:#fff; display:flex; align-items:center; justify-content:center; font-weight:700; font-size:.9rem; }}
.rs-hava .cv {{ color:var(--muted); font-size:.68rem; }}

/* Welcome banner */
.rs-welcome {{ position:relative; overflow:hidden; border-radius:22px; color:#fff;
  padding:2rem 2.3rem; min-height:196px; box-shadow:var(--shadow); margin-bottom:1.3rem;
  background:linear-gradient(100deg,#1E4FC6 0%,#2C74C6 44%,#27A79C 100%);
  display:flex; flex-direction:column; justify-content:center; }}
.rs-welcome h2 {{ color:#fff; font-size:2.05rem; font-weight:800; margin:0 0 .45rem; letter-spacing:-.01em; }}
.rs-welcome p {{ color:rgba(255,255,255,.9); margin:0 0 1.25rem; font-size:1rem; }}
.rs-welcome .cta {{ display:inline-block; width:max-content; background:#fff; color:#1E40AF;
  font-weight:700; padding:.62rem 1.5rem; border-radius:12px; box-shadow:var(--shadow-sm); }}
.rs-welcome .illus {{ position:absolute; right:26px; top:50%; transform:translateY(-50%); opacity:.96; pointer-events:none; }}
@media (max-width:1250px) {{ .rs-welcome .illus {{ display:none; }} }}

/* Generic widget card */
.rs-w {{ background:var(--surface); border:1px solid var(--border); border-radius:20px;
  padding:1.15rem 1.25rem; box-shadow:var(--shadow-sm); }}
.rs-w h4 {{ margin:0 0 1rem; font-size:1.08rem; font-weight:700; color:var(--ink); }}

/* Metric card (big number) */
.rs-metric {{ background:var(--surface); border:1px solid var(--border); border-radius:20px;
  padding:1.15rem 1.3rem; box-shadow:var(--shadow-sm); }}
.rs-metric .l {{ color:var(--ink); font-weight:700; font-size:1.02rem; }}
.rs-metric .r {{ display:flex; align-items:baseline; justify-content:space-between; margin-top:.5rem; }}
.rs-metric .v {{ font-size:2rem; font-weight:800; color:var(--ink); line-height:1; }}
.rs-metric .d {{ font-size:1.05rem; font-weight:700; }}

/* List rows (upcoming / med reminders) */
.rs-lrow {{ display:flex; align-items:center; gap:12px; padding:.55rem 0; }}
.rs-lrow + .rs-lrow {{ border-top:1px solid var(--subtle); }}
.rs-lrow .av {{ width:38px; height:38px; border-radius:50%; flex:none; color:#fff; font-weight:700;
  font-size:.82rem; display:flex; align-items:center; justify-content:center;
  background:linear-gradient(135deg,var(--brand),var(--accent)); }}
.rs-lrow .nm {{ font-weight:700; color:var(--ink); font-size:.92rem; line-height:1.2; }}
.rs-lrow .sb {{ color:var(--muted); font-size:.8rem; }}
.rs-lrow .rt {{ margin-left:auto; color:var(--ink); font-weight:600; font-size:.86rem; white-space:nowrap; }}
.rs-check {{ margin-left:auto; width:22px; height:22px; border-radius:6px; border:1.5px solid var(--border);
  display:flex; align-items:center; justify-content:center; flex:none; font-size:.8rem; color:transparent; }}
.rs-check.on {{ background:var(--brand); border-color:var(--brand); color:#fff; }}

/* Full-width card CTA */
.rs-wbtn {{ display:block; text-align:center; margin-top:1rem; background:#2563EB; color:#fff;
  font-weight:700; padding:.65rem; border-radius:12px; box-shadow:0 8px 18px rgba(37,99,235,.25); }}

/* Activity timeline */
.rs-tl {{ padding-left:22px; }}
.rs-tl .it {{ position:relative; padding:0 0 1.1rem; color:var(--ink); font-size:.9rem; line-height:1.35; }}
.rs-tl .it:last-child {{ padding-bottom:0; }}
.rs-tl .it::before {{ content:""; position:absolute; left:-22px; top:3px; width:12px; height:12px;
  border-radius:50%; background:var(--dot,#2563EB); box-shadow:0 0 0 3px rgba(37,99,235,.14); }}
.rs-tl .it::after {{ content:""; position:absolute; left:-16.5px; top:16px; bottom:-4px; width:2px; background:var(--border); }}
.rs-tl .it:last-child::after {{ display:none; }}

/* Calendar */
.rs-cal .cnav {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:.5rem; }}
.rs-cal .cnav .m {{ font-weight:700; color:var(--ink); font-size:.96rem; }}
.rs-cal .cnav .ar {{ color:var(--muted); font-size:1.15rem; }}
.rs-cal .grid {{ display:grid; grid-template-columns:repeat(7,1fr); text-align:center; }}
.rs-cal .dow {{ color:var(--muted); font-size:.72rem; font-weight:600; padding-bottom:.35rem; }}
.rs-cal .dy {{ font-size:.82rem; color:var(--ink); padding:.32rem 0; position:relative; border-radius:8px; }}
.rs-cal .dy.today {{ background:#2563EB; color:#fff; font-weight:700; }}
.rs-cal .dy .dt {{ position:absolute; bottom:2px; left:50%; transform:translateX(-50%);
  width:4px; height:4px; border-radius:50%; }}

/* Quick actions */
.rs-qa {{ display:flex; flex-direction:column; gap:.7rem; }}
.rs-qa .b {{ text-align:center; padding:.72rem; border-radius:12px; font-weight:700; font-size:.92rem; }}
.rs-qa .b.pri {{ background:#2563EB; color:#fff; box-shadow:0 8px 18px rgba(37,99,235,.25); }}
.rs-qa .b.sec {{ background:var(--surface); color:#1E293B; border:1px solid var(--border); }}

/* AI assistant shortcut */
.rs-ai {{ display:flex; align-items:center; justify-content:center; gap:10px; padding:.85rem;
  border-radius:14px; color:#fff; font-weight:700; font-size:1rem;
  background:linear-gradient(120deg,#8B5CF6 0%,#3B82F6 55%,#22D3EE 120%);
  box-shadow:0 10px 22px rgba(59,130,246,.28); margin-bottom:1.3rem; }}
.rs-ai img {{ width:22px; height:22px; }}

/* Notification rows */
.rs-nrow {{ display:flex; align-items:flex-start; gap:11px; padding:.6rem 0; }}
.rs-nrow + .rs-nrow {{ border-top:1px solid var(--subtle); }}
.rs-nrow .ic {{ width:34px; height:34px; border-radius:10px; flex:none; display:flex;
  align-items:center; justify-content:center; font-size:15px; }}
.rs-nrow .nt {{ font-weight:700; color:var(--ink); font-size:.88rem; line-height:1.25; }}
.rs-nrow .ns {{ color:var(--muted); font-size:.8rem; }}

/* Plotly card controls (Patient Overview) */
.rs-pill {{ display:inline-block; background:var(--subtle); color:var(--ink); font-weight:600;
  font-size:.82rem; padding:.32rem .8rem; border-radius:9px; }}
.rs-drop {{ display:inline-block; border:1px solid var(--border);
  color:var(--ink); font-weight:600; font-size:.82rem; padding:.32rem .7rem; border-radius:9px; }}

/* ============================================================= */
/* Patient dashboard — light, airy variant             */
/* ============================================================= */
.rs-pt-header {{ display:flex; align-items:flex-start; justify-content:space-between; margin:.1rem 0 1.2rem; }}
.rs-pt-header .lbl {{ color:var(--muted); font-size:.9rem; }}
.rs-pt-header .wel {{ font-size:1.9rem; font-weight:800; color:var(--ink); letter-spacing:-.02em; margin-top:2px; }}
.rs-pt-header .right {{ display:flex; align-items:center; gap:1.1rem; }}
.rs-pt-user {{ display:flex; align-items:center; gap:9px; }}
.rs-pt-user .a {{ width:44px; height:44px; border-radius:50%; flex:none;
  background:linear-gradient(135deg,var(--brand),var(--accent)); color:#fff;
  display:flex; align-items:center; justify-content:center; font-weight:700; }}
.rs-pt-user .nm {{ font-weight:700; color:var(--ink); font-size:.92rem; line-height:1.1; }}
.rs-pt-user .rl {{ color:var(--muted); font-size:.8rem; }}
.rs-pt-user .cv {{ color:var(--muted); font-size:.68rem; margin-left:2px; }}

/* Light welcome banner */
.rs-pt-banner {{ position:relative; overflow:hidden; border-radius:22px; padding:2.2rem 2.4rem; min-height:184px;
  background:linear-gradient(120deg,#E9F2FD 0%,#DCEAF9 58%,#EAF3FC 100%);
  box-shadow:var(--shadow-sm); margin-bottom:1.3rem;
  display:flex; flex-direction:column; justify-content:center; }}
.rs-pt-banner h2 {{ color:#17324F; font-size:2.2rem; font-weight:800; margin:0 0 .4rem; letter-spacing:-.01em; }}
.rs-pt-banner p {{ color:#3E5872; margin:0 0 1.25rem; font-size:1rem; }}
.rs-pt-banner .cta {{ display:inline-block; width:max-content; background:#2563EB; color:#fff;
  font-weight:700; padding:.62rem 1.5rem; border-radius:12px; box-shadow:0 8px 18px rgba(37,99,235,.25); }}
.rs-pt-banner .wave {{ position:absolute; right:0; top:0; height:100%; width:55%; opacity:1; pointer-events:none; }}

/* Patient stat card (icon left) */
.rs-pstat {{ display:flex; align-items:center; gap:14px; background:var(--surface); border:1px solid var(--border);
  border-radius:16px; padding:1.15rem 1.3rem; box-shadow:var(--shadow-sm); }}
.rs-pstat .ic {{ width:48px; height:48px; border-radius:13px; flex:none; display:flex;
  align-items:center; justify-content:center; }}
.rs-pstat .lb {{ color:var(--muted); font-size:.9rem; font-weight:600; }}
.rs-pstat .vl {{ font-size:1.9rem; font-weight:800; color:var(--ink); line-height:1; margin-top:2px; }}

/* AI assistant (patient) */
.rs-pai {{ display:flex; align-items:center; gap:13px; margin-bottom:1rem; }}
.rs-pai .logo {{ width:52px; height:52px; border-radius:14px; flex:none; display:flex;
  align-items:center; justify-content:center;
  background:linear-gradient(120deg,#8B5CF6,#3B82F6 55%,#22D3EE); box-shadow:0 8px 18px rgba(59,130,246,.28); }}
.rs-pai .t {{ font-weight:700; color:var(--ink); font-size:1.05rem; }}
.rs-pai .s {{ color:var(--muted); font-size:.85rem; }}

/* Appointment row (bordered) */
.rs-appt-row {{ display:flex; align-items:center; gap:12px; border:1px solid var(--border); border-radius:14px;
  padding:.7rem .85rem; margin-bottom:.7rem; box-shadow:var(--shadow-sm); }}
.rs-appt-row .av {{ width:42px; height:42px; border-radius:50%; flex:none; color:#fff; font-weight:700;
  font-size:.85rem; display:flex; align-items:center; justify-content:center;
  background:linear-gradient(135deg,var(--brand),var(--accent)); }}
.rs-appt-row .nm {{ font-weight:700; color:var(--ink); font-size:.92rem; }}
.rs-appt-row .sb {{ color:var(--muted); font-size:.8rem; }}
.rs-appt-row .tm {{ margin-left:auto; color:var(--ink); font-weight:700; font-size:.86rem; white-space:nowrap; }}

/* Outline full-width button */
.rs-obtn {{ display:block; text-align:center; margin-top:.4rem; border:1px solid var(--border); color:#2563EB;
  font-weight:700; padding:.65rem; border-radius:12px; background:var(--surface); }}

/* Adherence bar */
.rs-adh {{ display:flex; align-items:center; justify-content:space-between; margin:.2rem 0 .5rem; }}
.rs-adh .lb {{ color:var(--ink); font-weight:700; }}
.rs-adh .bdg {{ display:inline-flex; align-items:center; gap:5px; color:var(--success);
  background:rgba(18,183,106,.12); font-weight:700; font-size:.8rem; padding:.2rem .6rem; border-radius:999px; }}
.rs-bar {{ display:flex; height:9px; gap:3px; }}
.rs-bar span {{ display:block; height:100%; border-radius:999px; }}

/* Summary / reminder rows (icon · title/sub · right value) */
.rs-srow {{ display:flex; align-items:center; gap:11px; padding:.6rem 0; }}
.rs-srow + .rs-srow {{ border-top:1px solid var(--subtle); }}
.rs-srow .ic {{ width:34px; height:34px; border-radius:10px; flex:none; display:flex;
  align-items:center; justify-content:center; }}
.rs-srow .nt {{ font-weight:700; color:var(--ink); font-size:.88rem; line-height:1.2; }}
.rs-srow .ns {{ color:var(--muted); font-size:.78rem; }}
.rs-srow .rv {{ margin-left:auto; font-weight:700; color:var(--ink); font-size:.95rem; }}
.rs-srow .rv.time {{ font-size:.78rem; color:var(--muted); font-weight:600; }}
.rs-viewall {{ color:#2563EB; font-weight:600; font-size:.85rem; float:right; }}

/* ============================================================= */
/* Interactivity — functional buttons, header, calendar */
/* ============================================================= */
/* Primary buttons → blue to match the mockups' CTAs */
.stButton > button[kind="primary"] {{ background:#2563EB !important; background-image:none !important;
  border:none !important; color:#fff !important; box-shadow:0 8px 18px rgba(37,99,235,.25) !important; }}
.stButton > button[kind="primary"]:hover {{ filter:brightness(1.06);
  box-shadow:0 10px 22px rgba(37,99,235,.32) !important; }}

/* Header (functional) */
.rs-hdr-title .big {{ font-size:1.95rem; font-weight:800; color:var(--ink); letter-spacing:-.02em; white-space:nowrap; }}
.rs-hdr-title .lbl {{ color:var(--muted); font-size:.9rem; }}
.rs-hdr-title .wel {{ font-size:1.85rem; font-weight:800; color:var(--ink); letter-spacing:-.02em; margin-top:2px; }}
.rs-hdr-actions {{ display:flex; justify-content:flex-end; }}
/* Page subtitle beneath the shared top bar (title lives in the bar itself) */
.rs-subtitle {{ color:var(--muted); font-size:.95rem; margin:-.2rem 0 1.1rem; }}
/* "Need Help? / Support" — link-like button in the top bar (targeted by its key) */
.st-key-hdr_help button {{
  background:transparent !important; border:none !important; box-shadow:none !important;
  color:var(--muted) !important; font-weight:600 !important; line-height:1.15 !important;
  padding:.25rem .2rem !important; min-height:0 !important; }}
.st-key-hdr_help button:hover {{
  color:var(--ink) !important; transform:none !important; background:transparent !important; }}
.st-key-hdr_help button p {{ font-size:.82rem !important; }}

/* Hero with a real (functional) CTA button pulled up into the banner.
   Scoped to the specific container via the .rs-heromark marker. */
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .rs-heromark) .rs-welcome,
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .rs-heromark) .rs-pt-banner {{
  padding-bottom:4.4rem; margin-bottom:0; }}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] .rs-heromark) .stButton {{
  margin-top:-3.6rem; margin-left:2.3rem; max-width:200px; position:relative; z-index:3; margin-bottom:1.3rem; }}
/* patient hero button is dark-navy text on white per mockup; keep primary blue is fine */

/* Calendar nav arrow buttons (compact, borderless) */
.rs-cal-nav .stButton > button {{ padding:.1rem .4rem !important; min-height:0 !important;
  border:none !important; box-shadow:none !important; background:transparent !important;
  color:var(--muted) !important; font-size:1.2rem !important; line-height:1 !important; }}
.rs-cal-nav .stButton > button:hover {{ color:var(--ink) !important; transform:none; background:var(--subtle) !important; }}

/* segmented control (chart filters) → compact pill */
[data-testid="stSegmentedControl"] {{ margin-top:-.2rem; }}
</style>
"""


def inject_theme():
    """Inject the global design-system CSS. Call once per page render."""
    st.markdown(_css(), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Brand logo (verified raster assets, embedded as data URIs)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _data_uri(filename: str) -> str:
    data = (ASSETS_DIR / filename).read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode()


def logo_mark_html(size: int = 40) -> str:
    """Icon-only mark (R + heart + medical cross)."""
    return (f'<img src="{_data_uri("favicon_512.png")}" alt="Roshada" '
            f'style="width:{size}px;height:{size}px;object-fit:contain">')


def logo_html(height: int = 34, dark: bool = False) -> str:
    """Full horizontal logo (mark + wordmark). dark=True → white wordmark."""
    src = _data_uri("logo_dark.png" if dark else "logo.png")
    return (f'<img src="{src}" alt="Roshada" '
            f'style="height:{height}px;width:auto;object-fit:contain">')


def loading_splash():
    """Branded loading overlay shown once per session (CSS fade-out)."""
    if st.session_state.get("_splash_shown"):
        return
    st.session_state["_splash_shown"] = True
    st.markdown(
        f"""
<style>
#rs-splash {{ position:fixed; inset:0; z-index:99999; display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:18px; background:var(--surface);
  animation: rsFade 1.1s ease forwards; }}
@keyframes rsFade {{ 0%,55% {{opacity:1; visibility:visible}} 100% {{opacity:0; visibility:hidden}} }}
#rs-splash .spin {{ width:34px; height:34px; border-radius:50%;
  border:3px solid var(--brand-50); border-top-color:var(--brand);
  animation: rsSpin .8s linear infinite; }}
@keyframes rsSpin {{ to {{ transform: rotate(360deg) }} }}
#rs-splash .t {{ color:var(--muted); font-weight:600; }}
</style>
<div id="rs-splash">{logo_mark_html(64)}<div class="spin"></div>
  <div class="t">Loading Roshada…</div></div>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Login page (split-screen) — CSS + assets
# ---------------------------------------------------------------------------
LOGIN_PRIMARY = "#2563EB"   # royal-blue CTA from the mockup


def inject_login_css():
    st.markdown(f"""
<style>
/* Full-bleed login: hide sidebar & chrome, remove container padding */
section[data-testid="stSidebar"] {{ display:none; }}
[data-testid="stAppViewContainer"] .block-container {{
  padding:0 !important; max-width:100% !important; }}
[data-testid="stAppViewContainer"] {{ background:#F3F4F6; }}

/* Panels flush together (no gap/rounding between them) */
[data-testid="stAppViewContainer"] [data-testid="stHorizontalBlock"]:first-of-type {{ gap:0 !important; }}

/* Left panel: FIXED to the viewport's left half, always full height. It stays
   perfectly in place while only the right-hand form scrolls. */
.rs-login-left {{
  position:fixed; top:0; left:0; width:50%; height:100vh; border-radius:0;
  background:linear-gradient(150deg,#2F6BE4 0%,#2AA7C9 55%,#7B5FD0 100%);
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  padding:3rem 2rem; overflow:hidden; z-index:1; }}
/* Seat the R mark on a white badge so it stands out against the gradient
   (the icon shares the background's blue/cyan/violet palette otherwise). */
.rs-login-left .logo-badge {{ display:inline-flex; align-items:center; justify-content:center;
  width:112px; height:112px; border-radius:26px; background:#fff;
  box-shadow:0 12px 30px rgba(11,32,54,.28), 0 0 0 1px rgba(255,255,255,.6);
  margin-bottom:.2rem; }}
.rs-login-left .logo-badge img {{ filter:drop-shadow(0 2px 4px rgba(11,32,54,.12)); }}
.rs-login-left .brand-word {{ color:#fff; font-weight:800; font-size:2.6rem; margin-top:1rem; }}
.rs-login-left .tag {{ color:rgba(255,255,255,.9); font-size:1.05rem; margin-top:.2rem; }}
.rs-login-left .foot {{ position:absolute; bottom:0; left:0; right:0; padding:16px;
  text-align:center; color:rgba(255,255,255,.85); font-size:.85rem;
  background:rgba(11,32,54,.55); }}

.rs-login-right {{ min-height:100vh; display:flex; align-items:center;
  justify-content:center; padding:3rem 2rem; }}
.rs-login-card {{ width:100%; max-width:440px; }}
.rs-login-card h2 {{ font-size:1.7rem; font-weight:800; color:#12294A; margin:0 0 1.4rem; }}

.rs-divider {{ display:flex; align-items:center; gap:12px; color:var(--muted);
  font-size:.85rem; margin:1.2rem 0; }}
.rs-divider::before, .rs-divider::after {{ content:""; flex:1; height:1px; background:var(--border); }}

.rs-social {{ display:flex; align-items:center; justify-content:center; gap:12px;
  width:100%; padding:.8rem; border:1px solid var(--border); border-radius:14px;
  background:#fff; font-weight:600; color:#1f2937; margin-bottom:.7rem; cursor:pointer;
  box-shadow:var(--shadow-sm); transition:transform .15s, box-shadow .15s; }}
.rs-social:hover {{ transform:translateY(-1px); box-shadow:var(--shadow); }}
.rs-login-foot {{ text-align:right; color:var(--muted); font-size:.85rem; margin-top:2rem; }}
.rs-login-foot a {{ color:var(--muted); text-decoration:none; }}

/* Blue primary CTA on the login form (override the global teal gradient) */
div[data-testid="stForm"] .stFormSubmitButton > button {{
  background:{LOGIN_PRIMARY} !important; background-image:none !important;
  border:none !important; color:#fff !important; font-weight:700;
  box-shadow:0 8px 20px rgba(37,99,235,.30) !important; }}
div[data-testid="stForm"] .stFormSubmitButton > button:hover {{
  filter:brightness(1.05); }}
@media (max-width:900px) {{
  .rs-login-left {{ position:relative; width:100%; height:auto; min-height:34vh; }} }}
</style>
""", unsafe_allow_html=True)


def login_illustration_svg(width=300):
    return f"""
<svg width="{width}" viewBox="0 0 320 240" fill="none" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="160" cy="120" rx="150" ry="110" fill="rgba(255,255,255,.10)"/>
  <rect x="70" y="46" width="180" height="130" rx="16" fill="rgba(255,255,255,.16)"
        stroke="rgba(255,255,255,.35)"/>
  <circle cx="96" cy="70" r="6" fill="#fff" opacity=".85"/>
  <rect x="110" y="66" width="70" height="8" rx="4" fill="#fff" opacity=".7"/>
  <path d="M92 120 h16 l8 -18 10 34 8 -22 6 10 h20" stroke="#fff" stroke-width="3.4"
        fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M232 96 c0 14 -20 24 -20 24 s-20 -10 -20 -24 c0 -7 6 -12 12 -12
           c4 0 7 2 8 5 c1 -3 4 -5 8 -5 c6 0 12 5 12 12 z" fill="#FF6B7A"/>
  <rect x="92" y="140" width="30" height="26" rx="6" fill="#fff" opacity=".65"/>
  <rect x="130" y="140" width="30" height="26" rx="6" fill="#fff" opacity=".5"/>
  <rect x="168" y="140" width="60" height="10" rx="5" fill="#fff" opacity=".6"/>
  <rect x="168" y="156" width="44" height="10" rx="5" fill="#fff" opacity=".45"/>
  <circle cx="40" cy="60" r="5" fill="#fff" opacity=".6"/>
  <circle cx="285" cy="180" r="6" fill="#fff" opacity=".5"/>
  <circle cx="280" cy="70" r="4" fill="#fff" opacity=".7"/>
</svg>"""


_GOOGLE_ICON = (
    '<svg width="20" height="20" viewBox="0 0 48 48">'
    '<path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.6 30.2 0 24 0 '
    '14.6 0 6.5 5.4 2.6 13.2l7.8 6.1C12.3 13.3 17.7 9.5 24 9.5z"/>'
    '<path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.7c-.6 3-2.3 5.5-4.8 7.2'
    'l7.4 5.7C43.9 37.9 46.5 31.8 46.5 24.5z"/>'
    '<path fill="#FBBC05" d="M10.4 28.3c-.5-1.4-.7-2.8-.7-4.3s.3-2.9.7-4.3l-7.8-6.1'
    'C.9 16.7 0 20.2 0 24s.9 7.3 2.6 10.4l7.8-6.1z"/>'
    '<path fill="#34A853" d="M24 48c6.2 0 11.5-2 15.3-5.5l-7.4-5.7c-2 1.4-4.7 2.3-7.9 2.3'
    '-6.3 0-11.7-3.8-13.6-9.1l-7.8 6.1C6.5 42.6 14.6 48 24 48z"/></svg>')

_MICROSOFT_ICON = (
    '<svg width="18" height="18" viewBox="0 0 21 21">'
    '<rect x="1" y="1" width="9" height="9" fill="#F25022"/>'
    '<rect x="11" y="1" width="9" height="9" fill="#7FBA00"/>'
    '<rect x="1" y="11" width="9" height="9" fill="#00A4EF"/>'
    '<rect x="11" y="11" width="9" height="9" fill="#FFB900"/></svg>')


def social_button_html(provider):
    icon = _GOOGLE_ICON if provider == "Google" else _MICROSOFT_ICON
    return f'<div class="rs-social">{icon}<span>Continue with {provider}</span></div>'


# ---------------------------------------------------------------------------
# Component helpers (dynamic text is escaped)
# ---------------------------------------------------------------------------
def _e(x):
    return html.escape(str(x)) if x is not None else ""


def page_header(title, subtitle="", icon="🩺"):
    """The page title now lives in the shared top bar (render_topbar). This keeps
    only the subtitle as lightweight context beneath the bar (no duplicate title)."""
    if subtitle:
        st.markdown(f'<div class="rs-subtitle">{_e(subtitle)}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Layout helpers (12-column grid)
# ---------------------------------------------------------------------------
def row(spans, gap="medium"):
    """Return Streamlit columns for a 12-column-style row (spans = weights)."""
    return st.columns(spans, gap=gap)


def sidebar_spacer():
    """Flexible spacer that pushes following sidebar items to the bottom."""
    st.markdown('<div class="rs-sb-spacer"></div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Sidebar building blocks
#
# Presentation only — each renders one part of the navigation shell. The click
# handling lives with the router in streamlit_app.py, so these stay reusable.
# ---------------------------------------------------------------------------
def sidebar_collapsed_css(width: int = 84):
    """Narrow the sidebar to icons only.

    Injected only while collapsed. Nav labels are moved off-screen rather than
    removed with `display:none`, so each button keeps its accessible name for
    screen readers while showing just its icon.
    """
    st.markdown(f"""
<style>
@media (min-width: 768px) {{
  section[data-testid="stSidebar"] {{
    width: {width}px !important; min-width: {width}px !important; }}
  section[data-testid="stSidebar"] .block-container {{ padding-left:.6rem; padding-right:.6rem; }}
  /* Visually-hidden: off-screen but still announced. */
  section[data-testid="stSidebar"] .stButton button [data-testid="stMarkdownContainer"],
  .rs-sb-brand .word, .rs-sb-user .meta {{
    position:absolute !important; width:1px !important; height:1px !important;
    overflow:hidden !important; clip-path: inset(50%) !important; white-space:nowrap !important; }}
  section[data-testid="stSidebar"] .stButton button,
  section[data-testid="stSidebar"] .stButton button > div,
  section[data-testid="stSidebar"] .stButton button span[data-has-shortcut] {{
    justify-content:center !important; padding-left:0 !important; padding-right:0 !important; }}
  .rs-sb-brand, .rs-sb-user {{ justify-content:center; gap:0; padding-left:0; padding-right:0; }}
  /* An 84px column cannot fit "COMMUNICATION", and wrapping it produces an
     unreadable stack of 3-letter fragments. Render each section label as a thin
     divider instead: the grouping still reads, and the text stays in the DOM
     (indented off-screen) so screen readers keep announcing it. */
  .rs-sb-sec {{
    height:1px; padding:0; margin:.5rem .55rem; background:{SB_BORDER};
    overflow:hidden; text-indent:-9999px; white-space:nowrap; }}
}}
</style>""", unsafe_allow_html=True)


def sidebar_brand(collapsed: bool = False, size: int = 26):
    """Logo mark + wordmark. The wordmark hides itself when collapsed."""
    st.markdown(
        f'<div class="rs-sb-brand"><span class="mark">{logo_mark_html(size)}</span>'
        f'<span class="word">Roshada</span></div>',
        unsafe_allow_html=True)


def sidebar_section_label(title: str):
    """Subtle, visually secondary group heading."""
    st.markdown(f'<div class="rs-sb-sec">{_e(title)}</div>', unsafe_allow_html=True)


def sidebar_user(name: str, secondary: str = "", collapsed: bool = False):
    """Compact profile card: avatar + name + secondary line + status dot."""
    st.markdown(
        f'<div class="rs-sb-user">'
        f'<div class="av">{_e(avatar_initials(name))}</div>'
        f'<div class="meta"><div class="nm">{_e(name)}</div>'
        f'<div class="sub">{_e(secondary)}</div></div></div>',
        unsafe_allow_html=True)


def hero_banner(title, subtitle="", cta=None):
    """Full-width gradient welcome banner (matches the approved mockup hero)."""
    cta_html = f'<span class="cta">{_e(cta)}</span>' if cta else ""
    st.markdown(
        f'<div class="rs-hero"><h2>{_e(title)}</h2>'
        f'<p>{_e(subtitle)}</p>{cta_html}'
        f'<div class="illus">{login_illustration_svg(210)}</div></div>',
        unsafe_allow_html=True)


_BADGE_KINDS = {
    "success": ("var(--success)", "rgba(18,183,106,.12)"),
    "warning": ("var(--warning)", "rgba(247,144,9,.14)"),
    "danger": ("var(--danger)", "rgba(240,68,56,.12)"),
    "info": ("var(--info)", "rgba(46,144,250,.12)"),
    "brand": ("var(--brand-dark)", "var(--brand-50)"),
    "muted": ("var(--muted)", "var(--subtle)"),
}


def badge(text, kind="brand"):
    fg, bg = _BADGE_KINDS.get(kind, _BADGE_KINDS["brand"])
    return f'<span class="rs-badge" style="color:{fg};background:{bg}">{_e(text)}</span>'


def stat_card(label, value, icon="📊", tint="var(--brand-50)", color="var(--brand)", delta=None):
    delta_html = (f'<div class="delta" style="color:{color}">{_e(delta)}</div>'
                  if delta else "")
    st.markdown(
        f'<div class="rs-stat"><div class="top">'
        f'<div class="ic" style="background:{tint}">{_e(icon)}</div>{delta_html}</div>'
        f'<div class="label">{_e(label)}</div>'
        f'<div class="value">{_e(value)}</div></div>', unsafe_allow_html=True)


def empty_state(title, description="", icon="📭"):
    st.markdown(
        f'<div class="rs-empty"><div class="ic">{_e(icon)}</div>'
        f'<div class="t">{_e(title)}</div>'
        f'<div class="d">{_e(description)}</div></div>', unsafe_allow_html=True)


def avatar_initials(name):
    parts = [p for p in str(name or "?").split() if p]
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper() if parts else "?"


def doctor_card(name, specialization, available=True):
    status = badge("Available", "success") if available else badge("Unavailable", "muted")
    st.markdown(
        f'<div class="rs-doctor"><div class="row">'
        f'<div class="rs-avatar">{_e(avatar_initials(name))}</div>'
        f'<div style="flex:1"><div class="name">Dr. {_e(name)}</div>'
        f'<div class="spec">{_e(specialization)}</div></div>{status}</div></div>',
        unsafe_allow_html=True)


def appointment_card(when, doctor, specialization, reason=""):
    reason_html = f'<div class="meta">📝 {_e(reason)}</div>' if reason else ""
    st.markdown(
        f'<div class="rs-appt"><div class="when">🗓️ {_e(when)}</div>'
        f'<div class="meta">👨‍⚕️ Dr. {_e(doctor)} · {_e(specialization)}</div>'
        f'{reason_html}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Dashboard components — header, banner, widget cards
# ---------------------------------------------------------------------------
_MAG_ICON = ('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#9AA7B8" '
             'stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/>'
             '<path d="M21 21l-4.3-4.3"/></svg>')
_BELL_ICON = ('<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#5B6B80" '
              'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
              '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>'
              '<path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>')


def dashboard_illustration_svg(width=340):
    """Abstract medical-dashboard illustration for the welcome banner."""
    return f"""
<svg width="{width}" viewBox="0 0 360 200" fill="none" xmlns="http://www.w3.org/2000/svg">
  <ellipse cx="180" cy="100" rx="172" ry="92" fill="rgba(255,255,255,.08)"/>
  <rect x="118" y="34" width="150" height="106" rx="12" fill="rgba(255,255,255,.16)" stroke="rgba(255,255,255,.35)"/>
  <rect x="130" y="46" width="58" height="8" rx="4" fill="#fff" opacity=".7"/>
  <path d="M130 96 h14 l7 -16 9 30 7 -20 5 9 h18" stroke="#fff" stroke-width="3" fill="none"
        stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="208" y="60" width="48" height="30" rx="6" fill="rgba(255,255,255,.22)"/>
  <path d="M242 70 c0 9 -12 15 -12 15 s-12 -6 -12 -15 c0 -4 4 -7 7 -7 c2 0 4 1 5 3
           c1 -2 3 -3 5 -3 c3 0 7 3 7 7 z" fill="#FF6B7A"/>
  <rect x="130" y="112" width="40" height="16" rx="5" fill="rgba(255,255,255,.5)"/>
  <rect x="178" y="112" width="78" height="7" rx="3.5" fill="#fff" opacity=".55"/>
  <rect x="178" y="123" width="54" height="7" rx="3.5" fill="#fff" opacity=".4"/>
  <rect x="282" y="52" width="40" height="40" rx="11" fill="rgba(255,255,255,.2)"/>
  <path d="M302 62 v20 M292 72 h20" stroke="#fff" stroke-width="3" stroke-linecap="round"/>
  <circle cx="298" cy="120" r="16" fill="rgba(255,255,255,.18)"/>
  <path d="M298 113 v14 M291 120 h14" stroke="#8CFFE9" stroke-width="2.5" stroke-linecap="round"/>
  <circle cx="70" cy="50" r="5" fill="#fff" opacity=".5"/>
  <circle cx="92" cy="152" r="6" fill="#fff" opacity=".45"/>
  <circle cx="332" cy="40" r="4" fill="#fff" opacity=".6"/>
</svg>"""


def dash_header(title, notifications=1, user_name="Roshada"):
    """Top header bar: page title · search · notifications · support · avatar."""
    st.markdown(
        f'<div class="rs-topbar">'
        f'<div class="ttl">{_e(title)}</div>'
        f'<div class="rs-search">{_MAG_ICON}'
        f'<div class="box">Search for patients, records...</div></div>'
        f'<div class="rs-hact">'
        f'<div class="rs-bell">{_BELL_ICON}<span class="dot">{_e(notifications)}</span></div>'
        f'<div class="rs-help">Need Help?<b>Support</b></div>'
        f'<div class="rs-hava"><div class="a">{_e(avatar_initials(user_name))}</div>'
        f'<span class="cv">▼</span></div>'
        f'</div></div>', unsafe_allow_html=True)


def welcome_banner(title, subtitle="", cta="View My Day"):
    """Full-width blue→teal welcome banner with medical illustration."""
    st.markdown(
        f'<div class="rs-welcome"><h2>{_e(title)}</h2><p>{_e(subtitle)}</p>'
        f'<span class="cta">{_e(cta)}</span>'
        f'<div class="illus">{dashboard_illustration_svg(340)}</div></div>',
        unsafe_allow_html=True)


def metric_card(label, value, delta="", delta_color="var(--brand)"):
    """Big-number KPI card (Appointments Today, Medication Adherence…)."""
    d = f'<div class="d" style="color:{delta_color}">{_e(delta)}</div>' if delta else ""
    st.markdown(
        f'<div class="rs-metric"><div class="l">{_e(label)}</div>'
        f'<div class="r"><div class="v">{_e(value)}</div>{d}</div></div>',
        unsafe_allow_html=True)


def _avatar_row(name, sub, right_html):
    return (f'<div class="rs-lrow"><div class="av">{_e(avatar_initials(name))}</div>'
            f'<div style="min-width:0"><div class="nm">{_e(name)}</div>'
            f'<div class="sb">{_e(sub)}</div></div>{right_html}</div>')


def upcoming_card(items, title="Upcoming Appointments", cta="View Full Schedule"):
    """items = list of (name, subtitle, time)."""
    rows = "".join(_avatar_row(n, s, f'<div class="rt">{_e(t)}</div>') for n, s, t in items)
    st.markdown(
        f'<div class="rs-w"><h4 style="margin-bottom:.5rem">{_e(title)}</h4>{rows}'
        f'<div class="rs-wbtn">{_e(cta)}</div></div>', unsafe_allow_html=True)


def med_reminders_card(items, title="Medication Reminders"):
    """items = list of (name, subtitle, checked)."""
    rows = ""
    for n, s, checked in items:
        chk = '<div class="rs-check on">✓</div>' if checked else '<div class="rs-check"></div>'
        rows += _avatar_row(n, s, chk)
    st.markdown(f'<div class="rs-w"><h4 style="margin-bottom:.5rem">{_e(title)}</h4>{rows}</div>',
                unsafe_allow_html=True)


def activity_card(items, title="Recent Activity", cta="View More Activity"):
    """items = list of (text, dot_color)."""
    its = "".join(f'<div class="it" style="--dot:{c}">{_e(text)}</div>' for text, c in items)
    st.markdown(
        f'<div class="rs-w"><h4 style="margin-bottom:.9rem">{_e(title)}</h4>'
        f'<div class="rs-tl">{its}</div>'
        f'<div class="rs-wbtn">{_e(cta)}</div></div>', unsafe_allow_html=True)


def calendar_card(month_label="September 2023", today=2, title="Calendar Widget"):
    """Static month grid matching the mockup (day 1 under Mon)."""
    dows = "".join(f'<div class="dow">{d}</div>' for d in ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"])
    cells = ['<div class="dy"></div>']  # leading empty (Sun)
    dot_teal, dot_violet = {8, 15, 22, 29}, {9, 16, 23, 30}
    for d in range(1, 32):
        cls = "dy today" if d == today else "dy"
        dot = ""
        if d in dot_teal:
            dot = '<span class="dt" style="background:#12B76A"></span>'
        elif d in dot_violet:
            dot = '<span class="dt" style="background:#8B5CF6"></span>'
        cells.append(f'<div class="{cls}">{d}{dot}</div>')
    st.markdown(
        f'<div class="rs-w"><h4 style="margin-bottom:.5rem">{_e(title)}</h4>'
        f'<div class="rs-cal"><div class="cnav"><span class="ar">‹</span>'
        f'<span class="m">{_e(month_label)}</span><span class="ar">›</span></div>'
        f'<div class="grid">{dows}{"".join(cells)}</div></div></div>', unsafe_allow_html=True)


def quick_actions_card(actions, title="Quick Actions"):
    """actions = list of (label, is_primary)."""
    btns = "".join(f'<div class="b {"pri" if pri else "sec"}">{_e(label)}</div>'
                   for label, pri in actions)
    st.markdown(f'<div class="rs-w"><h4>{_e(title)}</h4><div class="rs-qa">{btns}</div></div>',
                unsafe_allow_html=True)


def ai_notifications_card(notifications, ai_label="Ask Roshada AI",
                          ai_title="AI Assistant Shortcut", notif_title="Notifications"):
    """Combined AI shortcut + notifications card. notifications = (emoji, bg, title, sub)."""
    nrows = ""
    for icon, bg, ntitle, sub in notifications:
        nrows += (f'<div class="rs-nrow"><div class="ic" style="background:{bg}">{icon}</div>'
                  f'<div><div class="nt">{_e(ntitle)}</div><div class="ns">{_e(sub)}</div></div></div>')
    st.markdown(
        f'<div class="rs-w"><h4 style="margin-bottom:.9rem">{_e(ai_title)}</h4>'
        f'<div class="rs-ai">{logo_mark_html(22)}<span>{_e(ai_label)}</span></div>'
        f'<h4 style="margin:.2rem 0 .4rem">{_e(notif_title)}</h4>{nrows}</div>',
        unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Patient dashboard components — light variant
# ---------------------------------------------------------------------------
_ICON_PATHS = {
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="3"/><path d="M3 9h18M8 2v4M16 2v4"/>',
    "doc": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
    "bell": '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
    "pill": '<path d="M10.5 20.5a5 5 0 0 1-7-7l6-6a5 5 0 0 1 7 7z"/><path d="M8.5 8.5l7 7"/>',
    "chat": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
}


def _icon(kind, color="#2563EB", size=22):
    """Reusable inline line-icon (stroke-based, recolorable)."""
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
            f'{_ICON_PATHS.get(kind, _ICON_PATHS["calendar"])}</svg>')


def _pt_wave():
    return ('<svg class="wave" viewBox="0 0 400 200" preserveAspectRatio="none" '
            'xmlns="http://www.w3.org/2000/svg">'
            '<path d="M60 200 C40 120 160 130 200 60 C240 -10 360 30 400 0 L400 200 Z" fill="rgba(37,99,235,.07)"/>'
            '<path d="M140 200 C120 150 220 150 260 100 C300 55 380 90 400 70 L400 200 Z" fill="rgba(37,99,235,.06)"/>'
            '</svg>')


def patient_header(welcome_name, role_label="Patient", notifications=1,
                   title="Patient Health Dashboard"):
    """Patient header: title + welcome (left) · notifications + user (right). No search."""
    st.markdown(
        f'<div class="rs-pt-header"><div>'
        f'<div class="lbl">{_e(title)}</div>'
        f'<div class="wel">Welcome, {_e(welcome_name)}</div></div>'
        f'<div class="right">'
        f'<div class="rs-bell">{_BELL_ICON}<span class="dot">{_e(notifications)}</span></div>'
        f'<div class="rs-pt-user"><div class="a">{_e(avatar_initials(welcome_name))}</div>'
        f'<div><div class="nm">{_e(welcome_name)}</div><div class="rl">{_e(role_label)}</div></div>'
        f'<span class="cv">▼</span></div>'
        f'</div></div>', unsafe_allow_html=True)


def patient_banner(title="Smart Care. Better Life.",
                   subtitle="Take care of your health today to tomorrow.", cta="Find Doctors"):
    st.markdown(
        f'<div class="rs-pt-banner">{_pt_wave()}'
        f'<h2>{_e(title)}</h2><p>{_e(subtitle)}</p>'
        f'<span class="cta">{_e(cta)}</span></div>', unsafe_allow_html=True)


def patient_stat_card(kind, tint, color, label, value):
    st.markdown(
        f'<div class="rs-pstat"><div class="ic" style="background:{tint}">{_icon(kind, color, 24)}</div>'
        f'<div><div class="lb">{_e(label)}</div><div class="vl">{_e(value)}</div></div></div>',
        unsafe_allow_html=True)


def patient_ai_card(title="AI Assistant", subtitle="Smart assistant ready. Start chat.", cta="Ask AI"):
    st.markdown(
        f'<div class="rs-w"><div class="rs-pai"><div class="logo">{logo_mark_html(30)}</div>'
        f'<div><div class="t">{_e(title)}</div><div class="s">{_e(subtitle)}</div></div></div>'
        f'<div class="rs-wbtn">{_e(cta)}</div></div>', unsafe_allow_html=True)


def patient_upcoming_card(items, title="Upcoming Appointments", cta="View Details"):
    """items = list of (doctor_name, subtitle, time)."""
    rows = "".join(
        f'<div class="rs-appt-row"><div class="av">{_e(avatar_initials(n))}</div>'
        f'<div style="min-width:0"><div class="nm">{_e(n)}</div><div class="sb">{_e(s)}</div></div>'
        f'<div class="tm">{_e(t)}</div></div>' for n, s, t in items)
    st.markdown(f'<div class="rs-w"><h4>{_e(title)}</h4>{rows}'
                f'<div class="rs-obtn">{_e(cta)}</div></div>', unsafe_allow_html=True)


def my_medications_card(segments, summary, title="My Medications",
                        adherence_label="Adherence", adherence_badge="Adherent"):
    """segments = list of (color, weight); summary = list of (kind,tint,color,title,sub,value)."""
    bars = "".join(f'<span style="flex:{w};background:{c}"></span>' for c, w in segments)
    srows = ""
    for kind, tint, color, nt, ns, rv in summary:
        srows += (f'<div class="rs-srow"><div class="ic" style="background:{tint}">{_icon(kind, color, 17)}</div>'
                  f'<div><div class="nt">{_e(nt)}</div><div class="ns">{_e(ns)}</div></div>'
                  f'<div class="rv">{_e(rv)}</div></div>')
    st.markdown(
        f'<div class="rs-w"><h4 style="margin-bottom:.7rem">{_e(title)}'
        f'<span class="rs-viewall">View all →</span></h4>'
        f'<div class="rs-adh"><span class="lb">{_e(adherence_label)}</span>'
        f'<span class="bdg">{_icon("check", "#12B76A", 13)}{_e(adherence_badge)}</span></div>'
        f'<div class="rs-bar">{bars}</div>'
        f'<div style="font-weight:700;color:var(--ink);font-size:.92rem;margin:1rem 0 .2rem">Summary</div>'
        f'{srows}</div>', unsafe_allow_html=True)


def medication_reminder_card(items, title="Medication Reminder"):
    """items = list of (kind,tint,color,title,sub,timestamp)."""
    st.markdown(f'<div class="rs-w"><h4>{_e(title)}</h4>{med_reminder_rows_html(items)}</div>',
                unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# HTML-fragment helpers — let pages put REAL buttons inside cards
# ---------------------------------------------------------------------------
def appt_rows_html(items):
    """Doctor upcoming rows: (name, sub, time)."""
    return "".join(_avatar_row(n, s, f'<div class="rt">{_e(t)}</div>') for n, s, t in items)


def med_reminders_html(items):
    """(name, sub, checked) rows with checkboxes."""
    rows = ""
    for n, s, checked in items:
        chk = '<div class="rs-check on">✓</div>' if checked else '<div class="rs-check"></div>'
        rows += _avatar_row(n, s, chk)
    return rows


def activity_rows_html(items):
    """(text, dot_color) timeline."""
    its = "".join(f'<div class="it" style="--dot:{c}">{_e(text)}</div>' for text, c in items)
    return f'<div class="rs-tl">{its}</div>'


def notif_rows_html(notifications):
    """(emoji, bg, title, sub) rows."""
    rows = ""
    for icon, bg, ntitle, sub in notifications:
        rows += (f'<div class="rs-nrow"><div class="ic" style="background:{bg}">{icon}</div>'
                 f'<div><div class="nt">{_e(ntitle)}</div><div class="ns">{_e(sub)}</div></div></div>')
    return rows


def patient_appt_rows_html(items):
    """Patient bordered doctor rows: (name, sub, time)."""
    return "".join(
        f'<div class="rs-appt-row"><div class="av">{_e(avatar_initials(n))}</div>'
        f'<div style="min-width:0"><div class="nm">{_e(n)}</div><div class="sb">{_e(s)}</div></div>'
        f'<div class="tm">{_e(t)}</div></div>' for n, s, t in items)


def med_reminder_rows_html(items):
    rows = ""
    for kind, tint, color, nt, ns, tm in items:
        rows += (f'<div class="rs-srow"><div class="ic" style="background:{tint}">{_icon(kind, color, 17)}</div>'
                 f'<div><div class="nt">{_e(nt)}</div><div class="ns">{_e(ns)}</div></div>'
                 f'<div class="rv time">{_e(tm)}</div></div>')
    return rows


def medications_body_html(segments, summary, adherence_label="Adherence", adherence_badge="Adherent"):
    bars = "".join(f'<span style="flex:{w};background:{c}"></span>' for c, w in segments)
    srows = ""
    for kind, tint, color, nt, ns, rv in summary:
        srows += (f'<div class="rs-srow"><div class="ic" style="background:{tint}">{_icon(kind, color, 17)}</div>'
                  f'<div><div class="nt">{_e(nt)}</div><div class="ns">{_e(ns)}</div></div>'
                  f'<div class="rv">{_e(rv)}</div></div>')
    return (f'<div class="rs-adh"><span class="lb">{_e(adherence_label)}</span>'
            f'<span class="bdg">{_icon("check", "#12B76A", 13)}{_e(adherence_badge)}</span></div>'
            f'<div class="rs-bar">{bars}</div>'
            f'<div style="font-weight:700;color:var(--ink);font-size:.92rem;margin:1rem 0 .3rem">Summary</div>{srows}')


def calendar_grid_html(year, month, today=None, events=None):
    """Real month grid via the stdlib calendar; highlights `today`, dots on `events` days."""
    import calendar as _calmod
    events = set(events or [])
    dows = "".join(f'<div class="dow">{d}</div>' for d in ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"])
    cells = []
    for week in _calmod.Calendar(firstweekday=6).monthdayscalendar(year, month):  # Sunday-first
        for d in week:
            if d == 0:
                cells.append('<div class="dy"></div>')
                continue
            is_today = today is not None and today.year == year and today.month == month and today.day == d
            dot = ('<span class="dt" style="background:#12B76A"></span>' if d in events else "")
            cells.append(f'<div class="{"dy today" if is_today else "dy"}">{d}{dot}</div>')
    return f'<div class="rs-cal"><div class="grid">{dows}{"".join(cells)}</div></div>'
