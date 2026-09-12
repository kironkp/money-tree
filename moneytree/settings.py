"""Django settings for MoneyTree — the day-trading agent.

Same shape as kre8essence/findit: one settings module, the ON_HEROKU switch
picks production behaviour, everything else defaults to local dev on SQLite.
"""
import os
import sys
from pathlib import Path

import dj_database_url
import environ
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    SECRET_KEY=(str, 'django-insecure-dev-key-change-in-production'),
    DB_NAME=(str, 'moneytree'),
    DB_USER=(str, ''),
    DB_PASSWORD=(str, ''),
    DB_HOST=(str, 'localhost'),
    DB_PORT=(str, '5432'),
)
environ.Env.read_env(os.path.join(BASE_DIR, '.env'))

SECRET_KEY = env('SECRET_KEY')
# Bump per release; tagged in git (v1.0, v1.1, …) with a matching
# backups/db-<tag>.sqlite3 snapshot. Rollback recipe lives in CLAUDE.md.
VERSION = '1.32'

# Tests must be deterministic even when a developer's local .env selects
# production behavior. Manifest storage is a deployment concern; requiring
# collectstatic before unit tests made view tests depend on machine state.
TESTING = 'test' in sys.argv

if 'ON_HEROKU' in os.environ:
    DEBUG = False
else:
    DEBUG = env('DEBUG')

if DEBUG:
    ALLOWED_HOSTS = ['*']
elif 'ON_HEROKU' in os.environ:
    ALLOWED_HOSTS = ['.herokuapp.com'] + env.list('ALLOWED_HOSTS', default=[])
else:
    ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=[])

if 'ON_HEROKU' in os.environ:
    CSRF_TRUSTED_ORIGINS = ['https://*.herokuapp.com']
elif DEBUG:
    # Phone testing through the Tailscale TLS proxy or a cloudflared quick tunnel.
    CSRF_TRUSTED_ORIGINS = ['https://*.ts.net', 'https://*.trycloudflare.com']
else:
    CSRF_TRUSTED_ORIGINS = []

if 'ON_HEROKU' in os.environ:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 180
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

INSTALLED_APPS = [
    'main_app',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Auth surface as in kre8essence: email login, signup (invite-only here),
    # password reset, passkeys (allauth.mfa), Google dormant until keyed.
    'allauth',
    'allauth.account',
    'allauth.mfa',
    'allauth.socialaccount',
]

GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')
GOOGLE_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
if GOOGLE_ENABLED:
    INSTALLED_APPS.append('allauth.socialaccount.providers.google')
    SOCIALACCOUNT_PROVIDERS = {
        'google': {'APPS': [{'client_id': GOOGLE_CLIENT_ID, 'secret': GOOGLE_CLIENT_SECRET}],
                   'SCOPE': ['profile', 'email']}
    }
    SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
    SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'moneytree.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'main_app.context_processors.site_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'moneytree.wsgi.application'

if 'ON_HEROKU' in os.environ:
    DATABASES = {
        'default': dj_database_url.config(
            env='DATABASE_URL', conn_max_age=600, conn_health_checks=True, ssl_require=True,
        ),
    }
elif env('DB_USER'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': env('DB_NAME'),
            'USER': env('DB_USER'),
            'PASSWORD': env('DB_PASSWORD'),
            'HOST': env('DB_HOST'),
            'PORT': env('DB_PORT'),
        }
    }
else:
    # Three processes write this file: the web app, the agent loop and the
    # optimizer subprocess. WAL + IMMEDIATE transactions + a long busy timeout
    # keep them from tripping over each other with "database is locked".
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': {
                'timeout': 30,
                'transaction_mode': 'IMMEDIATE',
                'init_command': 'PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;',
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
# Market time. Everything is stored in UTC and rendered in ET.
TIME_ZONE = 'America/New_York'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': (
            'django.contrib.staticfiles.storage.StaticFilesStorage'
            if DEBUG or TESTING
            else 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        ),
    },
}
WHITENOISE_MANIFEST_STRICT = False

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

# Email — password reset and verification mail. Console backend by default;
# Resend SMTP drops in via EMAIL_* (host smtp.resend.com, user "resend",
# password = API key). The adapter never lets a send failure 500 a flow.
EMAIL_HOST = os.getenv('EMAIL_HOST', '')
if EMAIL_HOST:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
    EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', '1') == '1'
    EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'MoneyTree <onboarding@resend.dev>')

# allauth (65.x settings family).
ACCOUNT_ADAPTER = 'main_app.adapters.AccountAdapter'
ACCOUNT_FORMS = {'signup': 'main_app.forms.InviteSignupForm'}
ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']
# Verification is only enforced once real email delivery exists; until then
# an invited user can log in right away (the console still prints the link).
ACCOUNT_EMAIL_VERIFICATION = 'mandatory' if EMAIL_HOST else 'optional'
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
# A finished password reset logs you in and lands on the dashboard — no dead end.
ACCOUNT_LOGIN_ON_PASSWORD_RESET = True
ACCOUNT_LOGOUT_ON_GET = False
# Sign-up is invite-only. These addresses are always allowed and become operators.
# Shared secret other apps use to POST their model usage to /api/spend/.
# Empty disables ingest entirely (the endpoint returns 503).
SPEND_INGEST_TOKEN = os.getenv('SPEND_INGEST_TOKEN', '')

# Where the 17:30 daily report goes. Falls back to the owner's address.
REPORT_EMAIL = os.getenv('REPORT_EMAIL', os.getenv('DJANGO_SUPERUSER_EMAIL', ''))

SIGNUP_ALLOWED_EMAILS = [e.strip().lower() for e in
                         os.getenv('SIGNUP_ALLOWED_EMAILS', os.getenv('DJANGO_SUPERUSER_EMAIL', '')).split(',') if e.strip()]

# Passkeys: WebAuthn needs a secure context; localhost is allowed in DEBUG.
MFA_SUPPORTED_TYPES = ['webauthn', 'totp', 'recovery_codes']
MFA_PASSKEY_LOGIN_ENABLED = True
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = DEBUG

LOGIN_URL = 'account_login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'account_login'

# ---------------------------------------------------------------------------
# MoneyTree
# ---------------------------------------------------------------------------
# Where lock files and worker scratch live (gitignored).
RUN_DIR = BASE_DIR / 'run'

# Alpaca. Paper keys drive Sprout-with-real-quotes and Sapling (paper account);
# live keys are a separate pair and only matter in v2.
ALPACA_API_KEY = os.getenv('ALPACA_API_KEY', '')
ALPACA_SECRET_KEY = os.getenv('ALPACA_SECRET_KEY', '')
ALPACA_LIVE_API_KEY = os.getenv('ALPACA_LIVE_API_KEY', '')
ALPACA_LIVE_SECRET_KEY = os.getenv('ALPACA_LIVE_SECRET_KEY', '')
ALPACA_ENABLED = bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)

# Web-search lane briefings (services/briefing.py).
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
# Interlocks for real money. Both must be true, plus AgentConfig.mode == live.
LIVE_TRADING_ARMED = os.getenv('LIVE_TRADING_ARMED', '0') == '1'
LIVE_FLATTEN_ON_EXIT = os.getenv('LIVE_FLATTEN_ON_EXIT', '0') == '1'

# Coach.
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
# Measured 2026-09-09 on a real review: 5,073 in / 4,431 out = $0.41 on Opus,
# $0.08 on Sonnet. Four lanes daily is $49/month against $10. The coach
# summarises evidence and proposes experiments, which Sonnet does well;
# set COACH_MODEL=claude-opus-5 in .env to buy the deeper review back.
COACH_MODEL = os.getenv('COACH_MODEL', 'claude-sonnet-5')
COACH_ENABLED = bool(ANTHROPIC_API_KEY)

# Timeframes the app knows how to store and trade on.
TIMEFRAMES = ['1Min', '5Min', '15Min', '30Min', '1Hour', '4Hour', '1Day']
DEFAULT_TIMEFRAME = '5Min'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'agent': {'format': '%(asctime)s %(levelname)s %(name)s: %(message)s'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'agent'},
    },
    'loggers': {
        'moneytree': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}
