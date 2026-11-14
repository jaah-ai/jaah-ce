"""The "Integrations" Settings group of the Settings catalogue, in render order."""
from __future__ import annotations

from app import model_catalog
from app.integrations import catalog as integration_catalog
from app.operator_settings.knobs import Knob, _Section, _sectioned

KNOBS: tuple[Knob, ...] = _sectioned(
    _Section("Integrations", "GitHub"),
    # ── Git access: the credential of whoever started the work; operator tier, one app of each kind ──
    # Which of the two designated apps Connect GitHub and GitHub sign-in use: the GitHub App by
    # default, the OAuth App when chosen. When the chosen one is not designated the other serves
    # (`git_access.app_config`), so a deployment with only one app needs no choice made. Switching never
    # retires a connection made through the other app.
    Knob(
        key="git_connect_app",
        tier="operator",
        label="Connect with",
        help="GitHub Connect and sign-in go through this app.",
        more="If the chosen app is not set up, the other one serves, so with only one app no choice is needed. "
             "Switching never retires connections made through the other app.",
        kind="str", env=None, default="github_app", minimum=1, maximum=16,
        choices=("github_app", "oauth_app"), choice_labels=("GitHub App", "OAuth App"),
        choice_help=("Connect and sign-in use the GitHub App.", "Connect and sign-in use the OAuth App."),
    ),
    # Whether the "Sign in/up with GitHub" buttons and their start door are offered (`login_oauth.github_configured`).
    # Off by default: a designated app is not proof sign-in works — a GitHub App without the "Email addresses:
    # read" account permission gets a 403 on `/user/emails`, so every attempt fails after GitHub returns, and
    # nothing the console holds can see that. An operator turns it on once the app grants it.
    Knob(
        key="github_sign_in_enabled",
        tier="operator",
        label="Sign in with GitHub",
        help="On: sign-in and sign-up pages offer GitHub.",
        more="Turn on only once the app has the \"Email addresses: read\" permission (a GitHub App) or the "
             "user:email scope (an OAuth App). Without it every GitHub sign-in fails after GitHub returns, "
             "and the console cannot detect that.",
        docs="get-started/sign-in",
        kind="bool", env=None, default=False,
    ),
    # The GitHub App (`github_install_app`), on `git_app_credential_id`'s terms below. With its private key it
    # also mints Easy Mode runners' one-repo tokens on the Jaah org (`app/easy_mode_runner_tokens.py`).
    Knob(
        key="git_github_app_credential_id",
        tier="operator",
        label="GitHub App credential",
        help="Every organization's GitHub Connect uses this GitHub App.",
        more="Easy Mode projects need it too: install the App on the GitHub organization under Billing > Easy Mode "
             "for all repositories (Contents, Pull requests, Workflows, Commit statuses, Actions, Secrets, Variables "
             "and Environments: write; Checks: read), and add its private key to this "
             "credential. Their runners push with one-repository tokens from it, never a person's token.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("github_install_app",),
    ),
    # The OAuth App a person connects and signs in with GitHub through: one row for the whole deployment, a
    # `credentials.id` UUID. Operator tier (one app per deployment, never an org's); `routes/settings.py` and
    # `_validated_credential` guard who may write it and what it may name. Never re-add a deployment-wide
    # git credential. An id, not a name (names are not unique); the picker is
    # `apps/web/src/ui/CredentialSelect.tsx`. Blank means unconfigured, and Connect GitHub says so.
    # `env=None`: an env var could only carry another deployment's UUID. The 0..36 bounds are a shape
    # check; liveness is `git_access.py`'s question at resolution time, while `_validated_credential`
    # checks the writer can see the row at pick time. Complements, not substitutes.
    Knob(
        key="git_app_credential_id",
        tier="operator",
        label="OAuth App credential",
        help="GitHub Connect via OAuth uses this app. Blank: unconfigured.",
        more="One app serves every organization. Blank leaves the OAuth App unconfigured, and Connect GitHub "
             "says so.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        # `github_app` only: the OAuth App's own client pair, not a git credential
        # (`git_access.GIT_CREDENTIAL_TYPES` excludes it), so a personal `github` token cannot be picked.
        credential_type=("github_app",),
    ),
    _Section("Integrations", "GitLab"),
    # The other forges' Connect apps (`forge_oauth.FORGE_OAUTH[*].app_setting`), each one operator-plane row
    # for the deployment, on `git_app_credential_id`'s terms. Blank: that forge's Connect is withheld with a
    # sentence naming this setting (`routes/forge_connect.py::connect_offer`).
    Knob(
        key="gitlab_app_credential_id",
        tier="operator",
        label="App credential",
        help="Connect GitLab (gitlab.com) authorizes through this app. Blank hides it.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("gitlab_app",),
    ),
    _Section("Integrations", "Bitbucket"),
    Knob(
        key="bitbucket_app_credential_id",
        tier="operator",
        label="App credential",
        help="Connect Bitbucket authorizes through this consumer. Blank hides it.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("bitbucket_app",),
    ),
    _Section("Integrations", "Microsoft"),
    # ONE Entra app registration serves both Microsoft doors: Connect Azure DevOps
    # (`forge_oauth.FORGE_OAUTH["azure_devops"]`) and "Sign in with Microsoft" (`routes/login_oauth.py`), so
    # the client pair lives in a console credential, never in the environment. The key keeps its Azure DevOps
    # name: a rename would orphan every stored designation.
    Knob(
        key="azure_devops_app_credential_id",
        tier="operator",
        label="App credential",
        help="Azure DevOps Connect and Microsoft sign-in authorize through this app.",
        more="One Entra app registration serves both. Blank withholds Connect Azure DevOps.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("azure_devops_app",),
    ),
    # Whether the "Sign in/up with Microsoft" buttons and their start door are offered
    # (`login_oauth.microsoft_configured`), on `github_sign_in_enabled`'s terms: an app designated for Connect
    # is not proof sign-in works — sign-in needs personal accounts allowed and its own redirect URI registered,
    # and nothing the console holds can see either.
    Knob(
        key="microsoft_sign_in_enabled",
        tier="operator",
        label="Sign in with Microsoft",
        help="On: sign-in and sign-up pages offer Microsoft.",
        more="Turn on only once the app accepts personal accounts and lists the "
             "/api/auth/oauth/microsoft/callback redirect URI. The console cannot check either, so a "
             "missing one makes every Microsoft sign-in fail.",
        docs="get-started/sign-in",
        kind="bool", env=None, default=False,
    ),
    _Section("Integrations", "Slack"),
    # The one `slack` credential every per-user notification posts through, designated once for the
    # deployment. Operator tier: one Slack app per environment. `env=None`,
    # the type filter and 0/36 bounds for `git_app_credential_id`'s reasons; liveness is asked at delivery
    # (`notify_targets._org_slack_bot_token`). Blank means unconfigured and the panel says so; never guessed
    # (a wrong guess is somebody else's alerts).
    Knob(
        key="notifications_slack_credential_id",
        tier="operator",
        label="Credential",
        help="Every per-user Slack notification posts through this app's bot.",
        more="Each user adds only their Slack member id. Blank: no Slack notifications are sent.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("slack",),
    ),
    Knob(
        key="notifications_enabled",
        # Operator tier: org-less readers and a deployment-owned subject (see
        # tests/test_operator_settings.py::_TIER_VERDICTS).
        tier="operator",
        label="Operator alerts",
        help="A task turning blocked or failed alerts the operator channel.",
        kind="bool", env="JAAH_NOTIFY_ENABLED", default=True,
    ),
    _Section("Integrations", "Telegram"),
    # The one `telegram_bot` credential every per-user Telegram notification sends as, designated once for
    # the deployment as Slack's above (same `env=None`, filter and bounds). Saving it registers the bot's
    # webhook (`routes/settings.py` -> `telegram_bot.register_webhook`); blank means Telegram is off.
    Knob(
        key="notifications_telegram_credential_id",
        tier="operator",
        label="Bot",
        help="Every per-user Telegram notification is sent by this bot.",
        more="Saving registers the bot's webhook. Each user connects their own chat. Blank turns Telegram off.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("telegram_bot",),
    ),
    _Section("Integrations", "Email"),
    # ── "Integrations" > "Email": system mail sent through SES (`app/system_mail.py`).
    # The domain is the environment's (`SES_MAIL_DOMAIN`, set by Terraform); these two shape the From line only.
    # The retired Gmail mailbox's six `email_*` keys are likewise ignored on load if still stored.
    Knob(
        key="system_mail_sender_name",
        tier="operator",
        label="System mail sender name",
        help="Recipients see this name in the From line.",
        more="For example Jaah. Blank shows the sending address alone.",
        kind="str", env=None, default="Jaah", minimum=0, maximum=100,
    ),
    Knob(
        key="system_mail_from_local",
        tier="operator",
        label="System mail From address",
        help="System mail is sent from this mailbox name.",
        more="The part before the @, for example notifications; the domain is this environment's own. "
             "Blank means notifications.",
        kind="str", env=None, default="notifications", minimum=0, maximum=64, email_local_part=True,
    ),
    # The keyring one-click unsubscribe links are signed with (`app/email_unsubscribe.py`). Blank sends optional mail
    # without the unsubscribe headers and footer link — never a dead link.
    Knob(
        key="email_unsubscribe_credential_id",
        tier="operator",
        label="Unsubscribe signing key",
        help="Signs one-click unsubscribe links. Blank: mail has no unsubscribe link.",
        more="Blank sends optional mail without the unsubscribe headers and footer link, never a dead link.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("email_unsubscribe_secret",),
    ),
    _Section("Integrations", "Tenant Gmail"),
    # ── "Integrations" > "Tenant Gmail": the Google OAuth client an org's member consents under to connect their own
    # Gmail, send only (`app/tenant_gmail/`). Operator tier: one client per
    # deployment, its consent screen the deployment's. A `google` credential holds the pair: knobs carry no secret.
    # Blank hides Connect with Google; a service-account connection needs none of this.
    Knob(
        key="tenant_gmail_oauth_credential_id",
        tier="operator",
        label="Google OAuth client",
        help="Orgs connect Gmail through this Google client. Blank hides it.",
        more="A Google credential holding the OAuth client ID and secret, used for the gmail.send scope only. "
             "Blank hides Connect with Google; service-account connections need none of this.",
        docs="administration/security-and-audit/gmail",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("google",),
    ),
    _Section("Integrations", "Figma"),
    # ── "Integrations" > "Figma": the deployment's own Figma OAuth app every `figma` credential's Connect authorizes
    # against (`oauth_providers.PROVIDERS["figma"]`). Operator tier: one app per deployment, its consent screen the
    # deployment's. Blank: Connect Figma answers that it isn't set up; a pasted personal access token needs none of this.
    Knob(
        key="figma_app_credential_id",
        tier="operator",
        label="App credential",
        help="Connect Figma authorizes through this app. Blank turns it off.",
        more="A pasted personal access token works without it.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("figma_app",),
    ),
    _Section("Integrations", "Site enquiries"),
    # ── "Integrations" > "Site enquiries": the marketing site's contact/waitlist form (`app/site_enquiry.py`,
    # which reads every one per request). Operator tier: the site's leads are the deployment's own, and
    # one site posts to one CP. Every default is blank: the ids are one environment's (the site posts to
    # prod's CP), set there. Credential knobs keep `env=None` and 0/36 bounds for the reasons above.
    Knob(
        key="site_enquiry_allowed_hosts",
        tier="operator",
        label="Site hosts",
        help="Only these hosts may post the enquiry form.",
        more="Comma-separated. The browser enforces it (CORS), and a reCAPTCHA token minted on any other host "
             "is refused. Blank refuses every enquiry.",
        kind="str", env=None, default="", minimum=0, maximum=500,
        feature="site_enquiry",
    ),
    Knob(
        key="site_enquiry_recaptcha_credential_id",
        tier="operator",
        label="reCAPTCHA credential",
        help="Each enquiry's reCAPTCHA token is checked with this key.",
        more="A Generic API Key whose API key is a Google service-account JSON key allowed to create "
             "reCAPTCHA Enterprise assessments. Blank refuses every enquiry.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("generic_api_key",),
        feature="site_enquiry",
    ),
    Knob(
        key="site_enquiry_recaptcha_project",
        tier="operator",
        label="reCAPTCHA project",
        help="reCAPTCHA checks run in this Google Cloud project.",
        more="Blank refuses every enquiry.",
        kind="str", env=None, default="", minimum=0, maximum=100,
        feature="site_enquiry",
    ),
    Knob(
        key="site_enquiry_recaptcha_site_key",
        tier="operator",
        label="reCAPTCHA site key",
        help="The form mints reCAPTCHA tokens with this key.",
        more="Blank refuses every enquiry.",
        kind="str", env=None, default="", minimum=0, maximum=100,
        feature="site_enquiry",
    ),
    Knob(
        key="site_enquiry_slack_channel",
        tier="operator",
        label="Slack channel",
        help="Each enquiry posts to this Slack channel. Blank skips Slack.",
        more="A channel id. The notifications Slack app posts it and must be a member of the channel.",
        kind="str", env=None, default="", minimum=0, maximum=40,
        feature="site_enquiry",
    ),
    Knob(
        key="site_enquiry_hubspot_credential_id",
        tier="operator",
        label="HubSpot credential",
        help="Each enquiry becomes a HubSpot contact note. Blank skips HubSpot.",
        more="The contact is found or created, and the enquiry is logged on it as a note.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("hubspot",),
        feature="site_enquiry",
    ),
    Knob(
        key="site_enquiry_hubspot_owner_id",
        tier="operator",
        label="HubSpot owner",
        help="New HubSpot contacts are assigned to this owner. Blank: unassigned.",
        kind="str", env=None, default="", minimum=0, maximum=40,
        feature="site_enquiry",
    ),
    _Section("Integrations", "Google Analytics"),
    # ── "Integrations" > "Google Analytics": the optional GA tag on the public Live Cast viewer (`app/live_analytics.py
    # ::measurement_id`, read per config request). Operator tier: one property per deployment, read org-less by the
    # public viewer's config. Blank by default: the viewer then loads no tag. The dashboard's Live tab reads Jaah's own
    # measurement, never Google. `ga_property_id` and `ga_credential_id`, the retired GA report's two, are
    # ignored on load if still stored (`_stored_candidates` reads the store by catalogue key) and name no consumer.
    Knob(
        key="ga_measurement_id",
        tier="operator",
        label="Measurement ID",
        help="The public Live Cast page sends page views here.",
        more="The property's web data stream id, G-XXXXXXXXXX. Blank loads no tag and sends nothing. The "
             "dashboard's Live tab reads Jaah's own measurement, never Google.",
        kind="str", env=None, default="", minimum=0, maximum=40,
        feature="live_cast",
    ),
    _Section("Integrations", "Support agent"),
    # ── "Integrations" > "Support agent": the LLM the /support chat talks to (`llm_client.support_llm`,
    # which reads both per call). Operator tier: one support desk per deployment, billed to the operator's
    # key. Blank provider keeps the env configuration (`SUPPORT_LLM_PROVIDER` and the env keys). The
    # credential knob keeps `env=None` and 0/36 bounds for the reasons above.
    Knob(
        key="support_llm_credential_id",
        tier="operator",
        label="Provider",
        help="Chat, titles and tags call the provider of this key.",
        more="Org slugs use it too. Blank keeps the server's own provider configuration.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("anthropic_api_key", "deepseek_api_key"),
    ),
    Knob(
        key="support_llm_model",
        tier="operator",
        label="Model",
        help="The chat answers with a model of this tier.",
        more="The same tiers a task picks from, turned into the provider's model. Images are sent only to "
             "models that accept them.",
        # A tier from `model_catalog.MODELS`, the task picker's own catalogue, so the two selects cannot
        # disagree; `llm_client.SUPPORT_TIER_MODELS` turns it into the provider's model. Empty is the
        # load-bearing default (`llm_client.SUPPORT_DEFAULT_MODELS`). The 200 bound, not the tier width, still
        # admits a free-text id stored before this was a select: it keeps resolving as written.
        kind="str", env=None, default="", minimum=0, maximum=200,
        choices=(model_catalog.INHERIT, *model_catalog.MODEL_IDENTIFIERS),
        choice_labels=("Default", *(choice.label for choice in model_catalog.MODELS)),
        choice_help=(
            "The provider's default model answers.",
            "The provider's most capable, most expensive model answers.",
            "The provider's high tier model answers.",
            "A faster, cheaper model answers.",
            "The cheapest model answers.",
        ),
        feature="support_assistant",
    ),
    _Section("Integrations", "Trigger preflight"),
    # ── "Integrations" > "Trigger preflight": the platform's key for a trigger's model preflight
    # (`llm_client.trigger_preflight_llm`), used only when the trigger names no key of its own (architecture
    # AD-I4). Operator tier, billed to the operator. No env fallback: blank means a model preflight needs its
    # own key, and saving one without is refused. 0/36 bounds as the credential knobs above.
    Knob(
        key="trigger_preflight_llm_credential_id",
        tier="operator",
        label="Platform key",
        help="Trigger model preflights without their own key use this one.",
        more="Billed to the operator. Blank requires every model preflight to name its own key, and saving "
             "one without is refused.",
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("anthropic_api_key", "deepseek_api_key"),
    ),
    _Section("Integrations", "Support agent"),
    # Website Jarvis (`app/site_chat/`): the anonymous chat on docs.jaah.ai and jaah.ai, read per
    # request. Operator tier, like the LLM it spends. ON by default in every environment (D10); the daily cap
    # is the hard cost ceiling, 0 = contact form only. The host set extends the site-enquiry hosts.
    Knob(
        key="site_chat_enabled",
        tier="operator",
        label="Website Jarvis",
        help="Anonymous visitors chat with Jarvis on the public sites.",
        more="Off shows them the contact form only.",
        kind="bool", env=None, default=True,
        feature="site_chat",
    ),
    Knob(
        key="site_chat_daily_turn_cap",
        tier="operator",
        label="Website daily cap",
        help="Past this many daily messages, visitors get the contact form.",
        more="Counted per UTC day across every visitor; the hard cost ceiling for website Jarvis. 0 answers "
             "none.",
        kind="int", env=None, default=300, minimum=0, maximum=100000, unit="messages",
        feature="site_chat",
    ),
    Knob(
        key="site_chat_allowed_hosts",
        tier="operator",
        label="Website hosts",
        help="These extra hosts may also embed website Jarvis.",
        more="Comma-separated. The site-enquiry hosts and the docs host are always allowed.",
        kind="str", env=None, default="", minimum=0, maximum=500,
        feature="site_chat",
    ),
    _Section("Integrations", "Titles and tags"),
    # ── "Integrations" > "Titles and tags": the model calls behind task titles, Re-tag and org slugs ──
    # Read per call (`llm_client.summary_llm`/`summary_timeout`/`classify_timeout`), on the support agent's
    # provider and key. Operator tier: the title and tag paths run org-less. The model and title timeout keep
    # the env names the local compose stack sets, so a configured stack keeps its value.
    Knob(
        key="summary_model",
        tier="operator",
        label="Model",
        help="This model writes task titles, picks tags and makes slugs.",
        more="It runs on the chat's provider. Another provider's model becomes this provider's model of the "
             "same tier.",
        # Free text, not the support agent's tier select: the env value is a model id (`llm_client.provider_model`).
        kind="str", env="JAAH_SUMMARY_MODEL", default="deepseek-flash", minimum=1, maximum=200,
    ),
    Knob(
        key="summary_timeout_secs",
        tier="operator",
        label="Title timeout",
        help="Slower title calls fall back to a text-made title.",
        kind="float", env="JAAH_SUMMARY_TIMEOUT", default=20.0, minimum=1.0, maximum=300.0, unit="s",
    ),
    Knob(
        key="classify_timeout_secs",
        tier="operator",
        label="Tag timeout",
        help="A Re-tag call running longer than this fails.",
        more="Longer than the title budget: the whole tag catalogue goes to a thinking model.",
        # Its own budget: the whole tag catalogue goes to a thinking model, so it is slower than a title.
        kind="float", env=None, default=60.0, minimum=1.0, maximum=600.0, unit="s",
    ),
    _Section("Integrations", "Sentry"),
    # ── "Integrations" > "Sentry": error tracking, Sentry, OFF until a DSN is set (`app/error_tracking.py` owns why and the scrub).
    # A DSN holds only Sentry's public ingest key, so it is a plain value, not a credential. Blank = off: no SDK,
    # no traffic. Setting one on prod makes Sentry a subprocessor (the vendor register's Sentry row).
    Knob(
        key="error_tracking_dsn",
        tier="operator",
        label="API Sentry DSN",
        help="API errors go to this Sentry project. Applies on restart.",
        more=(
            "Blank, nothing is sent. Read when an API task starts, so a change applies once the API "
            "restarts. Requests, cookies, emails and tokens are removed before sending."
        ),
        kind="str", env=None, default="", minimum=0, maximum=300,
        sentry_dsn=True,
    ),
    Knob(
        key="error_tracking_dsn_web",
        tier="operator",
        label="Console Sentry DSN",
        help="Console errors go to this Sentry project.",
        more=(
            "Blank, the browser loads nothing from Sentry. Read when a page loads. Served to signed-out "
            "visitors, as every browser DSN must be."
        ),
        # Served unauthenticated by `GET /api/public/settings` (`routes/public.py::_public_settings_rest`).
        kind="str", env=None, default="", minimum=0, maximum=300,
        sentry_dsn=True,
    ),
)



def catalog_app_knobs(specs=None) -> tuple[Knob, ...]:
    """One `oauth_app.<slug>` knob per catalog vendor with a Jaah-owned OAuth app, derived in this one loop (never
    hand-written): the `oauth_app` credential holding the deployment's client pair for that vendor. Blank leaves the
    vendor on its BYO client and API key (`GET /api/integrations` reads it as `app_configured: false`). The
    hand-written `*_app` knobs above keep their own keys."""
    chosen = integration_catalog.listed() if specs is None else specs
    knobs = [
        Knob(
            key=spec.app_knob,
            tier="operator",
            label=spec.label,
            help=f"Connect {spec.label} authorizes through this app.",
            more="Blank leaves people their own OAuth app or an API key.",
            kind="str", env=None, default="", minimum=0, maximum=36,
            credential_type=(integration_catalog.OAUTH_APP_TYPE,),
        )
        for spec in chosen if spec.app_knob is not None
    ]
    return _sectioned(_Section("Integrations", "OAuth apps"), *knobs) if knobs else ()


KNOBS = KNOBS + catalog_app_knobs()

#: The community edition has no support agent (D28), so its Settings show the titles' provider beside their model, in
#: words that name no support agent (`routes/settings.py::_settings_out`). `{key: fields served instead}`.
LOCAL_RECAST: dict[str, dict[str, str]] = {
    "support_llm_credential_id": {
        "section": "Titles and tags",
        "help": "Which provider writes task titles and picks tags, by its API key credential. Blank makes short "
                "titles from the task's text.",
    },
    "summary_model": {
        "help": "The model that writes task titles and picks tags. Another provider's model becomes this "
                "provider's model of the same tier.",
    },
}
