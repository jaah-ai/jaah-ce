"""Email templates: the closed set of template TYPES, their fields and placeholders, and the one layout that
renders them.

Split of authority, by operator decision: the LAYOUT and each type's FIELD SET are code — this module, edited
by a change to the repo — and the operator edits only the CONTENT of those fields, as rows in
`email_templates` (`app/models/notifications.py::EmailTemplate`, CRUD `app/routes/email_templates.py`, editor Settings >
Integrations > Email templates). A type may hold any number of rows; at most one is active (a partial unique
index). No active row means the type's built-in defaults below, so every type always renders.

Field text may carry `{{placeholder}}` from its type's fixed list. `validate_fields` refuses an unknown one on
save, so a typo never reaches a recipient; `render` substitutes the values, HTML-escaped in the HTML part and
verbatim in the subject and the plain-text part. Every message goes out as multipart: plain text plus HTML.

Stdlib-only, so `app/models/` can build the `email_templates.type` CHECK from `TYPE_KEYS` without an import
cycle (revisions 0332 and 0351 spell that CHECK as a frozen literal of their own).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Mapping, Optional

PRODUCT_NAME = "Jaah"
# `{{ name }}`, spaces optional — what `render` substitutes.
# `{{ name }}`, spaces optional. Anything else in braces is literal text.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
# Anything in double braces at all — so a misspelt `{{OrgName}}` is refused on save rather than sent as typed.
_BRACED_RE = re.compile(r"\{\{(.*?)\}\}")
FIELD_MAX = 2000


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    default: str
    multiline: bool = False
    # A field the layout cannot do without (the subject, a button's label). Blank refuses on save.
    required: bool = False


@dataclass(frozen=True)
class Placeholder:
    key: str
    description: str
    # What the editor's preview and a test send fill in.
    sample: str


@dataclass(frozen=True)
class SecondaryLink:
    """A sentence plus a link under the body, shown only when its URL placeholder has a value — so a
    template whose sender omits the URL sends no dangling sentence and no empty `href`."""

    text_field: str
    label_field: str
    url: str


@dataclass(frozen=True)
class TemplateType:
    key: str
    label: str
    description: str
    fields: tuple[Field, ...]
    placeholders: tuple[Placeholder, ...]
    # PRD R6: mail a person cannot unsubscribe from (account and billing). An optional type's send names whose
    # preference one click turns off (`app/email_unsubscribe.py`), and carries the headers and footer link.
    essential: bool
    # The placeholder whose value is the button's target; what a preview and a test send point it at.
    button: Optional[str] = None
    # A placeholder shown in the layout's code block (large, monospace) rather than in the body prose.
    code: Optional[str] = None
    # A multiline field of `Label: value` lines, rendered as a two-column table under the body.
    details_field: Optional[str] = None
    secondary: Optional[SecondaryLink] = None

    def field(self, key: str) -> Field:
        return next(f for f in self.fields if f.key == key)

    @property
    def defaults(self) -> dict[str, str]:
        return {f.key: f.default for f in self.fields}

    @property
    def samples(self) -> dict[str, str]:
        return {p.key: p.sample for p in self.placeholders}


USER_INVITATION = TemplateType(
    key="user_invitation",
    label="User invitation",
    description="Sent when an admin creates a user, and on Resend invitation.",
    fields=(
        Field("subject", "Subject", "{{inviter_name}} invited you to {{org_name}} on Jaah", required=True),
        Field("preheader", "Preview text", "Accept to join {{org_name}} as {{email}}."),
        Field("heading", "Heading", "Join {{org_name}} on Jaah", required=True),
        Field(
            "body", "Message",
            "Hi {{first_name}},\n\n{{inviter_name}} invited you to {{org_name}} on Jaah. Accept to sign in as "
            "{{email}}.",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Accept invitation", required=True),
        Field(
            "footer", "Footer",
            "Not expecting this? Ignore this email — nothing happens until you sign in.",
            multiline=True,
        ),
    ),
    placeholders=(
        Placeholder("first_name", "The invitee's first name, else \"there\"", "Alex"),
        Placeholder("name", "The invitee's full name", "Alex Morgan"),
        Placeholder("email", "The invitee's email", "alex@example.com"),
        Placeholder("org_name", "The organization", "Acme"),
        Placeholder("inviter_name", "Who created the user", "Sam Lee"),
        Placeholder(
            "login_url",
            "The button's target: a create-password link for a new invitee, the invitation's page for an "
            "existing account, else the sign-in page",
            "https://example.com/login",
        ),
    ),
    button="login_url",
    essential=True,
)

NOTIFICATION = TemplateType(
    key="notification",
    label="Notification",
    description="Wraps every notification email (task updates, questions, assignments, mentions). The "
                "event's own text fills the message.",
    fields=(
        Field("subject", "Subject", "{{headline}}", required=True),
        Field("button_label", "Button", "Open in Jaah", required=True),
        Field("footer", "Footer", "Sent because Email is on in Profile > Notifications.", multiline=True),
    ),
    placeholders=(
        Placeholder("headline", "The event's one-line summary", "Task #42 is waiting on your answer"),
    ),
    essential=False,
)

# ── The onboarding emails ──────────────────────────────────────────────────────────────────
# Senders go through `app/onboarding_mail.py::send`, which injects `tutorials_url` from the Sign-up settings.


def _common(who: str) -> tuple[Placeholder, ...]:
    """The placeholders every onboarding type takes; `who` names whose name and email they are."""
    return (
        Placeholder("first_name", f"The {who}'s first name, else \"there\"", "Alex"),
        Placeholder("email", f"The {who}'s email", "alex@example.com"),
        Placeholder(
            "tutorials_url", "The tutorials link (Settings > Organizations > Sign-up)", "https://jaah.ai/tutorials"
        ),
    )


_SIGNED_UP_FOOTER = "Sent to {{email}} because you applied for a Jaah account."

SIGNUP_VERIFY = TemplateType(
    key="signup_verify",
    label="Verify email",
    description="Sent when someone signs up with email: a code and a link that confirm the address.",
    fields=(
        Field("subject", "Subject", "{{code}} is your Jaah verification code", required=True),
        Field("preheader", "Preview text", "Enter it on the sign-up page. Expires in {{expires_minutes}} minutes."),
        Field("heading", "Heading", "Verify your email", required=True),
        Field(
            "body", "Message",
            "Hi {{first_name}},\n\nEnter this code on the sign-up page to confirm {{email}}. It expires in "
            "{{expires_minutes}} minutes.",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Verify email", required=True),
        Field(
            "footer", "Footer",
            "Didn't sign up? Ignore this email — no account is created without the code.",
            multiline=True,
        ),
    ),
    placeholders=_common("applicant") + (
        Placeholder("code", "The 6-digit code, shown in its own box", "482913"),
        Placeholder("verify_url", "The verify link (the button's target)", "https://example.com/signup/verify?token=sample"),
        Placeholder("expires_minutes", "Minutes until the code expires", "30"),
    ),
    button="verify_url",
    code="code",
    essential=True,
)

SIGNUP_ACCOUNT_EXISTS = TemplateType(
    key="signup_account_exists",
    label="Account already exists",
    description="Sent instead of a code when someone signs up with an email that already has an account.",
    fields=(
        Field("subject", "Subject", "You already have a Jaah account", required=True),
        Field("preheader", "Preview text", "Nothing changed. Sign in with {{email}}."),
        Field("heading", "Heading", "You already have a Jaah account", required=True),
        Field(
            "body", "Message",
            "Hi {{first_name}},\n\nSomeone tried to sign up with {{email}}, which already has a Jaah account. "
            "No new account was created — sign in instead.",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Sign in", required=True),
        Field("reset_text", "Reset sentence", "Forgot your password?"),
        Field("reset_label", "Reset link", "Reset your password", required=True),
        Field(
            "footer", "Footer",
            "Didn't try to sign up? Ignore this email — your account is unchanged.",
            multiline=True,
        ),
    ),
    placeholders=_common("account holder") + (
        Placeholder("login_url", "The sign-in link (the button's target)", "https://example.com/login?email=alex%40example.com"),
        Placeholder(
            "reset_url", "The password reset link; the reset sentence shows only when there is one",
            "https://example.com/forgot-password",
        ),
    ),
    button="login_url",
    secondary=SecondaryLink("reset_text", "reset_label", "reset_url"),
    essential=True,
)

APPLICATION_RECEIVED = TemplateType(
    key="application_received",
    label="Application received",
    description="Sent to an applicant when they submit their application.",
    fields=(
        Field("subject", "Subject", "We got your Jaah application", required=True),
        Field(
            "preheader", "Preview text",
            "We review every application and email you once yours is approved.",
        ),
        Field("heading", "Heading", "Thanks, {{first_name}}", required=True),
        Field(
            "body", "Message",
            "We review every new account by hand. Once yours is approved, a welcome email tells you how to sign "
            "in.\n\nUntil then, the tutorials show what Jaah does.",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Watch tutorials", required=True),
        Field("footer", "Footer", _SIGNED_UP_FOOTER, multiline=True),
    ),
    placeholders=_common("applicant"),
    button="tutorials_url",
    essential=True,
)

WELCOME = TemplateType(
    key="welcome",
    label="Welcome",
    description="Sent when an admin approves an organization, and on Send welcome email.",
    fields=(
        Field("subject", "Subject", "Your Jaah account is ready", required=True),
        Field("preheader", "Preview text", "{{org_name}} is approved. Sign in to start."),
        Field("heading", "Heading", "Welcome to Jaah, {{first_name}}", required=True),
        Field(
            "body", "Message",
            "{{org_name}} is approved. Sign in with {{sign_in_method}} to start.",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Sign in", required=True),
        Field("tutorials_text", "Tutorials sentence", "New to Jaah?"),
        Field("tutorials_label", "Tutorials link", "Watch the tutorials", required=True),
        Field("footer", "Footer", _SIGNED_UP_FOOTER, multiline=True),
    ),
    placeholders=_common("account owner") + (
        Placeholder("org_name", "The organization", "Acme"),
        Placeholder("login_url", "The sign-in link (the button's target)", "https://example.com/login?email=alex%40example.com"),
        Placeholder("sign_in_method", "How they signed up: \"your email and password\", Google, GitHub or Microsoft", "Google"),
    ),
    button="login_url",
    secondary=SecondaryLink("tutorials_text", "tutorials_label", "tutorials_url"),
    essential=True,
)

PASSWORD_RESET = TemplateType(
    key="password_reset",
    label="Reset password",
    description="Sent when someone asks to reset their password, or to set a first one.",
    fields=(
        Field("subject", "Subject", "Set a new Jaah password", required=True),
        Field("preheader", "Preview text", "The link works once and expires in {{expires_minutes}} minutes."),
        Field("heading", "Heading", "Set a new password", required=True),
        Field(
            "body", "Message",
            "Hi {{first_name}},\n\nSet a new password for {{email}}. The link works once and expires in "
            "{{expires_minutes}} minutes.",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Set password", required=True),
        Field(
            "footer", "Footer",
            "Didn't ask for this? Ignore this email — your password stays the same.",
            multiline=True,
        ),
    ),
    placeholders=_common("account holder") + (
        Placeholder("reset_url", "The single-use reset link (the button's target)", "https://example.com/reset-password?token=sample"),
        Placeholder("expires_minutes", "Minutes until the link expires", "60"),
    ),
    button="reset_url",
    essential=True,
)

SIGNUP_ALERT = TemplateType(
    key="signup_alert",
    label="New application",
    description="Sent to admins for each new application (Settings > Organizations > Sign-up sets who).",
    fields=(
        Field("subject", "Subject", "New application: {{applicant_name}}", required=True),
        Field("preheader", "Preview text", "{{status}} · {{account_type}} · {{org_name}}"),
        Field("heading", "Heading", "New application", required=True),
        Field(
            "body", "Message", "{{applicant_name}} applied for a Jaah account. {{status}}.",
            multiline=True, required=True,
        ),
        Field(
            "details", "Details",
            "Type: {{account_type}}\nName: {{applicant_name}}\nEmail: {{email}}\nOrganization: {{org_name}}\n"
            "Role: {{role}}\nIntended use: {{intended_use}}\nSigned up with: {{method}}",
            multiline=True,
        ),
        Field("button_label", "Button", "Review", required=True),
        Field("footer", "Footer", "Sent to Jaah admins for each new application.", multiline=True),
    ),
    placeholders=_common("applicant") + (
        Placeholder("applicant_name", "The applicant's full name", "Alex Morgan"),
        Placeholder("account_type", "Individual or Organization", "Organization"),
        Placeholder("org_name", "The organization applied for", "Acme"),
        Placeholder("role", "The applicant's occupation or job title", "Head of Engineering"),
        Placeholder("intended_use", "What they plan to use Jaah for", "Automating our release pipeline"),
        Placeholder("method", "How they signed up: Email, Google, GitHub or Microsoft", "Google"),
        Placeholder("review_url", "The organization's approval page (the button's target)", "https://example.com/orgs/sample"),
        Placeholder("status", "Waiting for review, or Approved automatically", "Waiting for review"),
    ),
    button="review_url",
    details_field="details",
    essential=False,
)

# ── Event-driven Task triggers ────────────────────────────────────────────
# Sent to a trigger's owner by `app/trigger_notices.py`, which fills every placeholder from display data only.

_TRIGGER_FOOTER = "Sent because you own this trigger and Email is on in Profile > Notifications."

TRIGGER_FAILED = TemplateType(
    key="trigger_failed",
    label="Trigger stopped",
    description="Sent to a trigger's owner the moment the system stops it (auto-pause, or its owner lost access).",
    fields=(
        Field("subject", "Subject", "Trigger stopped: {{trigger_name}}", required=True),
        Field("preheader", "Preview text", "{{reason}}"),
        Field("heading", "Heading", "{{trigger_name}} stopped", required=True),
        Field(
            "body", "Message",
            "Hi {{first_name}},\n\n{{trigger_name}} in {{project_name}} stopped filing Tasks: {{reason}}.\n\n"
            "New deliveries are skipped until you fix the cause and resume it.",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Open trigger", required=True),
        Field("footer", "Footer", _TRIGGER_FOOTER, multiline=True),
    ),
    placeholders=(
        Placeholder("first_name", "The owner's first name, else \"there\"", "Alex"),
        Placeholder("trigger_name", "The trigger", "Deploy failures"),
        Placeholder("project_name", "The trigger's project", "Acme web"),
        Placeholder("reason", "Why it stopped", "5 consecutive failures (last: preflight timed out)"),
        Placeholder("trigger_url", "The trigger's page (the button's target)", "https://example.com/triggers/sample"),
    ),
    button="trigger_url",
    essential=False,
)

TRIGGER_DIGEST = TemplateType(
    key="trigger_digest",
    label="Trigger digest",
    description="Sent once a day to each trigger owner who had deliveries, or a trigger not Active, in the last 24 hours.",
    fields=(
        Field("subject", "Subject", "Your triggers: {{headline}}", required=True),
        Field("preheader", "Preview text", "{{headline}} in the last 24 hours."),
        Field("heading", "Heading", "Your triggers, last 24 hours", required=True),
        Field(
            "body", "Message", "Hi {{first_name}},\n\n{{headline}} in the last 24 hours.\n\n{{trigger_lines}}",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Open triggers", required=True),
        Field("footer", "Footer", _TRIGGER_FOOTER, multiline=True),
    ),
    placeholders=(
        Placeholder("first_name", "The owner's first name, else \"there\"", "Alex"),
        Placeholder(
            "headline", "Totals: triggers, deliveries, and how many need attention",
            "2 triggers, 14 deliveries, 1 needs attention",
        ),
        Placeholder(
            "trigger_lines", "One paragraph per trigger: its health, its counts by outcome, any budget breach, its link",
            "Deploy failures — Degraded: 1 failure — last: preflight timed out\n12 dispatched, 1 failed\n"
            "https://example.com/triggers/sample",
        ),
        Placeholder("triggers_url", "The Triggers page (the button's target)", "https://example.com/triggers"),
    ),
    button="triggers_url",
    essential=False,
)

# ── Trust document requests ───────────────────────────────────────────────────────────────
# Sent to the requester by `app/trust_requests.py::decide` through `onboarding_mail.send`. The documents
# themselves never travel through the console: an operator sends them separately.

_TRUST_FOOTER = "Sent to {{email}} because you requested documents from the Jaah Trust Center."


def _trust_placeholders() -> tuple[Placeholder, ...]:
    return _common("requester") + (
        Placeholder("company", "The requester's company", "Acme"),
        Placeholder("documents", "The documents requested, by name", "SOC 2 report, Penetration test summary"),
        Placeholder("trust_url", "The Trust Center page (the button's target)", "https://example.com/legal/trust"),
    )


TRUST_REQUEST_APPROVED = TemplateType(
    key="trust_request_approved",
    label="Trust documents approved",
    description="Sent to a Trust Center visitor when an admin approves their document request.",
    fields=(
        Field("subject", "Subject", "Jaah document request approved", required=True),
        Field("preheader", "Preview text", "We'll send the documents separately."),
        Field("heading", "Heading", "Request approved", required=True),
        Field(
            "body", "Message",
            "Hi {{first_name}},\n\nYour request for {{documents}} is approved. We'll send the documents "
            "separately.",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Open Trust Center", required=True),
        Field("footer", "Footer", _TRUST_FOOTER, multiline=True),
    ),
    placeholders=_trust_placeholders(),
    button="trust_url",
    essential=True,
)

TRUST_REQUEST_DECLINED = TemplateType(
    key="trust_request_declined",
    label="Trust documents declined",
    description="Sent to a Trust Center visitor when an admin declines their document request.",
    fields=(
        Field("subject", "Subject", "Your Jaah document request", required=True),
        Field("preheader", "Preview text", "We can't share these documents right now."),
        Field("heading", "Heading", "Request declined", required=True),
        Field(
            "body", "Message",
            "Hi {{first_name}},\n\nWe can't share {{documents}} right now.\n\n{{reason}}",
            multiline=True, required=True,
        ),
        Field("button_label", "Button", "Open Trust Center", required=True),
        Field("footer", "Footer", _TRUST_FOOTER, multiline=True),
    ),
    placeholders=_trust_placeholders() + (
        Placeholder("reason", "The admin's reason, when they gave one", "We share these with customers under contract."),
    ),
    button="trust_url",
    essential=True,
)

ORG_REJECTED = TemplateType(
    key="org_rejected",
    label="Application declined",
    description="Sent to an applicant when an admin rejects their organization. It never carries the admin's reason.",
    fields=(
        Field("subject", "Subject", "About your Jaah application", required=True),
        Field("preheader", "Preview text", "We couldn't approve your account."),
        Field("heading", "Heading", "About your application", required=True),
        Field(
            "body", "Message",
            "Hi {{first_name}},\n\nThanks for applying to Jaah. We couldn't approve your account.\n\n"
            "Questions? Reply to this email and we'll get back to you.",
            multiline=True, required=True,
        ),
        Field("footer", "Footer", _SIGNED_UP_FOOTER, multiline=True),
    ),
    placeholders=_common("applicant") + (Placeholder("org_name", "The organization", "Acme"),),
    essential=True,
)

TYPES: dict[str, TemplateType] = {
    t.key: t
    for t in (
        USER_INVITATION, NOTIFICATION, SIGNUP_VERIFY, SIGNUP_ACCOUNT_EXISTS, APPLICATION_RECEIVED, WELCOME,
        PASSWORD_RESET, SIGNUP_ALERT, TRIGGER_FAILED, TRIGGER_DIGEST, TRUST_REQUEST_APPROVED,
        TRUST_REQUEST_DECLINED, ORG_REJECTED,
    )
}
TYPE_KEYS: tuple[str, ...] = tuple(TYPES)


class InvalidFields(ValueError):
    """`str(exc)` is a human sentence naming the field and what is wrong with it."""


def validate_fields(type_key: str, fields: Mapping[str, object]) -> dict[str, str]:
    """The stored shape of one template's `fields`: every key of the type, each a string. A missing key takes
    its default; an unknown key, a non-string, an over-long value, a blank required field or an unknown
    placeholder raises `InvalidFields`."""
    ttype = TYPES[type_key]
    known = {f.key for f in ttype.fields}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise InvalidFields(f"unknown field(s) {', '.join(unknown)}; {ttype.label} has {', '.join(sorted(known))}")
    allowed = {p.key for p in ttype.placeholders}
    out: dict[str, str] = {}
    for f in ttype.fields:
        value = fields.get(f.key, f.default)
        if not isinstance(value, str):
            raise InvalidFields(f"{f.label} must be text")
        if len(value) > FIELD_MAX:
            raise InvalidFields(f"{f.label} is {len(value)} characters; the limit is {FIELD_MAX}")
        if "\x00" in value:
            raise InvalidFields(f"{f.label} contains a NUL character")
        if f.required and not value.strip():
            raise InvalidFields(f"{f.label} cannot be blank")
        if not f.multiline and ("\n" in value or "\r" in value):
            raise InvalidFields(f"{f.label} must be one line")
        bad = sorted({m.strip() for m in _BRACED_RE.findall(value) if m.strip() not in allowed})
        if bad:
            names = ", ".join("{{" + p + "}}" for p in bad)
            offered = ", ".join("{{" + p + "}}" for p in sorted(allowed))
            raise InvalidFields(f"{f.label} uses unknown placeholder(s) {names}; {ttype.label} offers {offered}")
        out[f.key] = value
    return out


def _fill(text: str, values: Mapping[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)


@dataclass(frozen=True)
class Rendered:
    subject: str
    text: str
    html: str


# ── The layout ─────────────────────────────────────────────────────────────────────────────────────────
# Table-based with inline styles: the subset Gmail, Outlook and Apple Mail all honour. One 600px card on a
# tinted page, a system font stack, a bulletproof button, no images (nothing needs one to be read). Light is
# inline; dark is a `prefers-color-scheme` block (Apple Mail, Outlook for Mac/iOS) plus `[data-ogsc]` rules
# (Outlook.com), every colour pair at least 4.5:1. Gmail ignores both and applies its own inversion, which
# the light palette survives because nothing relies on a background image.

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

# The palette, copied from the console theme's raw values (`apps/web/src/theme.ts` `RAW` and the rail's
# `textStrong`/`text`/`accent`); apps never cross-import, so a theme change is mirrored here by hand.
_ACCENT = "#1d4ed8"      # light primary: links and the button, white on it 6.7:1
_INK = "#111827"         # light textPrimary
_BODY = "#374151"        # light textSecondary: prose under the heading
_MUTED = "#475569"       # rail text: footer and fine print, 7.2:1 on the page
_PAPER = "#f1f5f9"       # light background.default: the page
_CARD = "#ffffff"        # light background.paper
_RULE = "#e2e8f0"
_DARK_ACCENT = "#60a5fa"  # dark rail accent: links on dark paper
_DARK_FILL = "#2563eb"    # dark primaryFill: the button, white on it 5.17:1
_DARK_INK = "#f1f5f9"
_DARK_BODY = "#cbd5e1"
_DARK_MUTED = "#94a3b8"
_DARK_PAPER = "#0f172a"
_DARK_CARD = "#1e293b"
_DARK_RULE = "#334155"

# Class hooks the dark block repaints; the inline styles are the light (and only, for Gmail) rendering.
_STYLE = """
body,table,td,a{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}
table,td{mso-table-lspace:0pt;mso-table-rspace:0pt;}
@media (max-width:620px){
.em-outer{padding:20px 12px 28px 12px!important;}
.em-card{padding:28px 22px 26px 22px!important;}
.em-h1{font-size:22px!important;line-height:30px!important;}
.em-code{font-size:28px!important;letter-spacing:6px!important;padding-left:18px!important;}
.em-btn-table{width:100%!important;}
.em-btn{display:block!important;text-align:center!important;}
}
""".replace("\n", "")
# Separate block: Gmail drops a whole <style> on a selector it dislikes, so the phone rules above stand alone.
_STYLE_DARK = f"""
:root{{color-scheme:light dark;supported-color-schemes:light dark;}}
a[x-apple-data-detectors]{{color:inherit!important;text-decoration:none!important;}}
@media (prefers-color-scheme:dark){{
.em-page{{background:{_DARK_PAPER}!important;}}
.em-card{{background:{_DARK_CARD}!important;border-color:{_DARK_RULE}!important;border-top-color:{_DARK_FILL}!important;}}
.em-ink{{color:{_DARK_INK}!important;}}
.em-body{{color:{_DARK_BODY}!important;}}
.em-muted{{color:{_DARK_MUTED}!important;}}
.em-link{{color:{_DARK_ACCENT}!important;}}
.em-rule{{border-color:{_DARK_RULE}!important;}}
.em-code{{background:{_DARK_PAPER}!important;border-color:{_DARK_RULE}!important;color:{_DARK_INK}!important;}}
.em-btn-td,.em-btn{{background:{_DARK_FILL}!important;border-color:{_DARK_FILL}!important;}}
.em-mark{{background:{_DARK_FILL}!important;}}
}}
[data-ogsc] .em-ink{{color:{_DARK_INK}!important;}}
[data-ogsc] .em-body{{color:{_DARK_BODY}!important;}}
[data-ogsc] .em-muted{{color:{_DARK_MUTED}!important;}}
[data-ogsc] .em-link{{color:{_DARK_ACCENT}!important;}}
[data-ogsb] .em-page{{background:{_DARK_PAPER}!important;}}
[data-ogsb] .em-card,[data-ogsb] .em-code{{background:{_DARK_CARD}!important;}}
""".replace("\n", "")

# Pads the inbox preview so the client does not pull the body's first words in after the preheader.
# A long URL wraps at any character rather than widening the card on a phone.
_WRAP = "word-break:break-word;overflow-wrap:anywhere;"

_PREHEADER_PAD = "&#847;&zwnj;&nbsp;" * 60

# Never mid-word; ends before trailing punctuation or a closing quote, and before a `)` the URL did not open.
# The link text is always the URL itself, so a link can never disguise where it goes.
_URL_RE = re.compile(
    r"(?<![\w/])https?://(?:\([^\s<>\"'()\u201c\u201d\u2018\u2019]*\)|[^\s<>\"'()\u201c\u201d\u2018\u2019])*"
    r"(?:\([^\s<>\"'()\u201c\u201d\u2018\u2019]*\)|[^\s<>\"'()\u201c\u201d\u2018\u2019.,;:!?\]])"
)


@dataclass(frozen=True)
class _Extras:
    """What only some types carry: a code block, a details table, a sentence-plus-link under the button."""

    code: Optional[str] = None
    details: tuple[tuple[str, str], ...] = ()
    # (sentence, link label, url)
    secondary: Optional[tuple[str, str, str]] = None


def _link(url: str, label: str, *, style: str = "") -> str:
    """An `<a>` whose href is `url` (already escaped) and whose text is `label` (already escaped)."""
    return (
        f'<a href="{url}" target="_blank" class="em-link" '
        f'style="color:{_ACCENT};text-decoration:underline;{style}">{label}</a>'
    )


def _rich(text: str) -> str:
    """Raw text as escaped HTML, each bare http(s) URL a link to itself (no rewriting, no tracking) and each
    newline a `<br>`."""
    out, pos = [], 0
    for m in _URL_RE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        out.append(_link(html.escape(m.group(0), quote=True), html.escape(m.group(0)), style=_WRAP))
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out).replace("\n", "<br>")


def _paragraphs(text: str) -> str:
    """Text as `<p>`s: a blank line splits paragraphs, a single newline is a `<br>`, a URL is a link."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text.strip()) if b.strip()]
    return "".join(f'<p class="em-body" style="margin:0 0 16px 0;color:{_BODY};">{_rich(b)}</p>' for b in blocks)


def _extras_html(extras: _Extras) -> tuple[str, str]:
    """(above the button, below it). The code block reads as a code: large, monospace, letter-spaced, boxed."""
    esc = html.escape
    above = ""
    if extras.code:
        above += (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="margin:8px 0 24px 0;"><tr>'
            f'<td align="center" class="em-code code-block" bgcolor="{_PAPER}" style="background:{_PAPER};'
            f'border:1px solid {_RULE};border-radius:10px;padding:20px 12px 20px 22px;font-family:{_MONO};font-size:34px;'
            f'line-height:42px;font-weight:700;letter-spacing:10px;color:{_INK};">{esc(extras.code)}</td>'
            "</tr></table>"
        )
    if extras.details:
        rows = "".join(
            f'<tr><td valign="top" class="em-muted em-rule" style="padding:10px 16px 10px 0;font-size:14px;'
            f'line-height:20px;color:{_MUTED};white-space:nowrap;border-top:1px solid {_RULE};">{esc(label)}</td>'
            f'<td valign="top" class="em-ink em-rule" style="padding:10px 0;font-size:14px;line-height:20px;'
            f'color:{_INK};word-break:break-word;border-top:1px solid {_RULE};">'
            f'{esc(value).replace(chr(10), "<br>")}</td></tr>'
            for label, value in extras.details
        )
        above += (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'class="em-rule" style="margin:0 0 24px 0;border-bottom:1px solid {_RULE};">{rows}</table>'
        )
    below = ""
    if extras.secondary:
        sentence, label, url = extras.secondary
        lead = f"{esc(sentence)} " if sentence.strip() else ""
        below = (
            f'<p class="em-body" style="margin:20px 0 0 0;font-size:15px;line-height:24px;color:{_BODY};">{lead}'
            f'{_link(esc(url, quote=True), esc(label), style="font-weight:600;")}</p>'
        )
    return above, below


def _button(label: str, url: str) -> str:
    """A bulletproof button: the cell carries the fill (Outlook desktop paints it, ignoring the link's padding),
    the link carries the padding and the whole tap target everywhere else."""
    return (
        '<table role="presentation" class="em-btn-table" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 0 0;">'
        f'<tr><td align="center" class="em-btn-td" bgcolor="{_ACCENT}" '
        f'style="border-radius:8px;background:{_ACCENT};mso-padding-alt:14px 28px;">'
        f'<a href="{url}" target="_blank" class="em-btn" style="display:inline-block;padding:14px 28px;'
        f'font-family:{_FONT};font-size:16px;line-height:20px;font-weight:600;color:#ffffff;text-decoration:none;'
        f'border-radius:8px;border:1px solid {_ACCENT};background:{_ACCENT};mso-padding-alt:0;">'
        f"{html.escape(label)}</a></td></tr></table>"
    )


def _layout(
    *, title: str, preheader: str, heading: Optional[str], body_html: str, button_label: str,
    button_url: Optional[str], footer: str, extras: _Extras = _Extras(), unsubscribe_url: Optional[str] = None,
) -> str:
    """`unsubscribe_url`: an optional email's one-click link (`app/email_unsubscribe.py`), shown under the footer."""
    esc = html.escape
    above, below = _extras_html(extras)
    heading_html = (
        f'<h1 class="em-ink em-h1" style="margin:0 0 20px 0;font-family:{_FONT};font-size:24px;line-height:32px;'
        f'font-weight:700;color:{_INK};">{esc(heading)}</h1>'
        if heading else ""
    )
    action_html = below
    if button_url and button_label:
        url = esc(button_url, quote=True)
        action_html = (
            f"{_button(button_label, url)}"
            # The secondary link sits with the button, above the fallback that only explains the button.
            f"{below}"
            f'<p class="em-muted" style="margin:24px 0 0 0;font-size:13px;line-height:20px;color:{_MUTED};">'
            "Or paste this link into your browser:<br>"
            f'{_link(url, esc(button_url), style=_WRAP)}</p>'
        )
    footer_html = (
        f'<p class="em-muted" style="margin:0;font-size:13px;line-height:20px;color:{_MUTED};">'
        f"{esc(footer).replace(chr(10), '<br>')}</p>"
        if footer.strip() else ""
    )
    if unsubscribe_url:
        footer_html += (
            f'<p class="em-muted" style="margin:{"8px" if footer_html else "0"} 0 0 0;font-size:13px;'
            f'line-height:20px;color:{_MUTED};">'
            f'<a href="{esc(unsubscribe_url, quote=True)}" target="_blank" class="em-muted" style="color:{_MUTED};'
            f'text-decoration:underline;">Unsubscribe</a> from emails like this</p>'
        )
    return (
        "<!doctype html>"
        '<html lang="en" dir="ltr" xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="format-detection" content="telephone=no,date=no,address=no,email=no,url=no">'
        '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
        f"<title>{esc(title)}</title>"
        "<!--[if mso]><noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch>"
        "</o:OfficeDocumentSettings></xml></noscript><![endif]-->"
        f"<style>{_STYLE}</style><style>{_STYLE_DARK}</style></head>"
        f'<body class="em-page" style="margin:0;padding:0;width:100%;background:{_PAPER};">'
        # The inbox preview line: hidden in the body, shown beside the subject.
        '<div style="display:none;font-size:1px;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;'
        f'mso-hide:all;">{esc(preheader)}{_PREHEADER_PAD}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{_PAPER}" '
        f'class="em-page" style="background:{_PAPER};"><tr>'
        f'<td align="center" class="em-outer" style="padding:32px 16px 40px 16px;">'
        "<!--[if mso]><table role=\"presentation\" width=\"600\" cellpadding=\"0\" cellspacing=\"0\" "
        "border=\"0\"><tr><td><![endif]-->"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:600px;margin:0 auto;">'
        # Brand row: a wordmark in text, so it reads with images off.
        f'<tr><td style="padding:0 4px 18px 4px;font-family:{_FONT};">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td class="em-mark" width="28" height="28" align="center" bgcolor="{_ACCENT}" '
        f'style="width:28px;height:28px;border-radius:7px;background:{_ACCENT};color:#ffffff;font-family:{_FONT};'
        f'font-size:16px;line-height:28px;font-weight:700;" aria-hidden="true">{esc(PRODUCT_NAME[:1])}</td>'
        f'<td class="em-ink" style="padding-left:10px;font-family:{_FONT};font-size:18px;line-height:28px;'
        f'font-weight:700;color:{_INK};">{esc(PRODUCT_NAME)}</td></tr></table></td></tr>'
        # Card.
        f'<tr><td class="em-card" bgcolor="{_CARD}" style="background:{_CARD};border:1px solid {_RULE};'
        f'border-top:4px solid {_ACCENT};border-radius:12px;padding:36px 40px 36px 40px;font-family:{_FONT};'
        f'font-size:16px;line-height:26px;color:{_BODY};">'
        f"{heading_html}{body_html}{above}{action_html}</td></tr>"
        # Footer.
        f'<tr><td style="padding:24px 8px 0 8px;font-family:{_FONT};">{footer_html}</td></tr>'
        "</table>"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table></body></html>"
    )


def _text_part(
    *, heading: Optional[str], body: str, button_label: str, button_url: Optional[str], footer: str,
    extras: _Extras = _Extras(), unsubscribe_url: Optional[str] = None,
) -> str:
    # Blank-line-separated blocks; an empty one (a brief notification's body) is dropped, not left as a gap.
    blocks = [heading or "", body.strip(), extras.code or ""]
    blocks.append("\n".join(f"{label}: {value}" if label else value for label, value in extras.details))
    if button_url:
        blocks.append(f"{button_label}: {button_url}" if button_label else button_url)
    if extras.secondary:
        sentence, label, url = extras.secondary
        blocks.append(" ".join(part for part in (sentence.strip(), f"{label}: {url}") if part))
    if footer.strip():
        blocks.append("—\n" + footer.strip())
    text = "\n\n".join(b for b in blocks if b)
    # Last, so the plain-text part ends with it (seam S6).
    return f"{text}\n\nUnsubscribe: {unsubscribe_url}" if unsubscribe_url else text


def _details(template: str, values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """A details field's `Label: value` lines, split on the TEMPLATE's first colon (so a colon in a value
    cannot move the split); a blank value shows as a dash rather than an empty row."""
    rows = []
    for line in template.splitlines():
        if not line.strip():
            continue
        label, sep, rest = line.partition(":")
        if sep:
            rows.append((_fill(label, values).strip(), _fill(rest, values).strip() or "—"))
        else:
            rows.append(("", _fill(line, values).strip()))
    return tuple(rows)


def render(
    type_key: str, fields: Mapping[str, str], values: Mapping[str, str], *, button_url: Optional[str],
    unsubscribe_url: Optional[str] = None,
) -> Rendered:
    """Any template type but `notification` (which wraps an event, `render_notification`). `fields`: a
    validated field set of the type; `values`: placeholder values, raw text — escaped here in the HTML part.
    A placeholder with no value renders empty, a blank `first_name` as "there"; the type's secondary link
    shows only when its URL has a value; `button_url` None, or a type with no button field, leaves the button out.
    `unsubscribe_url` (an optional type's one-click link) adds the footer link; an essential type never shows one."""
    ttype = TYPES[type_key]
    if ttype.essential:
        unsubscribe_url = None
    filled = {p.key: "" for p in ttype.placeholders}
    filled.update(values)
    if "first_name" in filled and not filled["first_name"].strip():
        filled["first_name"] = "there"
    f = {k: _fill(v, filled) for k, v in fields.items()}
    secondary = None
    if ttype.secondary and filled.get(ttype.secondary.url, "").strip():
        link = ttype.secondary
        secondary = (f[link.text_field], f[link.label_field], filled[link.url])
    extras = _Extras(
        code=(filled.get(ttype.code) or None) if ttype.code else None,
        details=_details(fields[ttype.details_field], filled) if ttype.details_field else (),
        secondary=secondary,
    )
    url = button_url or None
    return Rendered(
        subject=f["subject"],
        text=_text_part(
            heading=f["heading"], body=f["body"], button_label=f.get("button_label", ""), button_url=url,
            footer=f["footer"], extras=extras, unsubscribe_url=unsubscribe_url,
        ),
        html=_layout(
            title=f["subject"], preheader=f["preheader"], heading=f["heading"], body_html=_paragraphs(f["body"]),
            button_label=f.get("button_label", ""), button_url=url, footer=f["footer"], extras=extras,
            unsubscribe_url=unsubscribe_url,
        ),
    )


def render_invitation(fields: Mapping[str, str], values: Mapping[str, str]) -> Rendered:
    """`fields`: a validated `user_invitation` field set; `values`: every placeholder's value."""
    return render(USER_INVITATION.key, fields, values, button_url=values.get("login_url"))


def _first_line(text: str, limit: int = 140) -> str:
    """The inbox preview of an event: its message's first line, so the preview adds to the subject rather
    than repeating it."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def render_notification(
    fields: Mapping[str, str], *, headline: str, message: str, link: Optional[str],
    unsubscribe_url: Optional[str] = None,
) -> Rendered:
    """`headline`: the event's one-line summary, the card's heading; `message`: the event's text BELOW the
    headline (untrusted task prose, escaped here; empty for a brief email); `link`: the console URL;
    `unsubscribe_url`: the one-click link of an optional notification (None for an essential one)."""
    values = {"headline": headline}
    f = {k: _fill(v, values) for k, v in fields.items()}
    return Rendered(
        subject=f["subject"],
        text=_text_part(
            heading=headline, body=message, button_label=f["button_label"], button_url=link, footer=f["footer"],
            unsubscribe_url=unsubscribe_url,
        ),
        html=_layout(
            title=f["subject"], preheader=_first_line(message) or headline, heading=headline,
            body_html=_paragraphs(message),
            button_label=f["button_label"], button_url=link, footer=f["footer"], unsubscribe_url=unsubscribe_url,
        ),
    )


#: The one-click link a preview and a test send show on an optional type, where a real send shows its signed one.
SAMPLE_UNSUBSCRIBE_URL = "https://example.com/unsubscribe"


def render_sample(type_key: str, fields: Mapping[str, str]) -> Rendered:
    """A type rendered with its placeholders' sample values — the editor's preview and a test send."""
    ttype = TYPES[type_key]
    if type_key == NOTIFICATION.key:
        return render_notification(
            fields,
            headline=ttype.samples["headline"],
            message="Which region should the new cluster run in?",
            link="https://example.com/tasks/123",
            unsubscribe_url=SAMPLE_UNSUBSCRIBE_URL,
        )
    return render(
        type_key, fields, ttype.samples, button_url=ttype.samples.get(ttype.button or ""),
        unsubscribe_url=SAMPLE_UNSUBSCRIBE_URL,
    )
