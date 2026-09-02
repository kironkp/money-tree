"""allauth adapter.

Two jobs: (1) a mail failure must never 500 an auth flow — Resend's sandbox
refuses every recipient but the account owner, so log it, print the action
link to the console, and (in DEBUG) show it on the page; (2) new users are
observers unless their email is on the operator allowlist or their invite
said so.
"""
import logging
from smtplib import SMTPException

from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.utils import timezone

from .models import SignupInvite

logger = logging.getLogger(__name__)


def signup_allowed(email: str) -> bool:
    email = (email or '').strip().lower()
    if not email:
        return False
    if email in settings.SIGNUP_ALLOWED_EMAILS:
        return True
    return SignupInvite.objects.filter(email__iexact=email, used_at__isnull=True).exists()


class AccountAdapter(DefaultAccountAdapter):
    def save_user(self, request, user, form, commit=True):
        user = super().save_user(request, user, form, commit=False)
        email = (user.email or '').lower()
        invite = SignupInvite.objects.filter(email__iexact=email, used_at__isnull=True).first()
        user.is_staff = email in settings.SIGNUP_ALLOWED_EMAILS or bool(invite and invite.make_operator)
        user.save()
        if invite is not None:
            invite.used_at = timezone.now()
            invite.save(update_fields=['used_at'])
        return user

    def send_mail(self, template_prefix, email, context):
        link = (context.get('activate_url') or context.get('password_reset_url') or context.get('key') or '')
        try:
            super().send_mail(template_prefix, email, context)
        except (SMTPException, OSError) as exc:
            logger.warning('Auth email to %s failed: %s', email, exc)
            print(f'[email:fallback] could not send to {email} — {exc}\n[email:fallback] action link: {link}', flush=True)
            self._show_link(link, 'Email delivery failed on this server.')
            return
        if not settings.EMAIL_HOST:
            # Console backend: the mail only went to the server log. In dev the
            # owner is the only reader, so put the link on the page too.
            self._show_link(link, 'No email server is configured, so nothing was sent.')

    def _show_link(self, link: str, why: str) -> None:
        if settings.DEBUG and self.request is not None and link:
            messages.info(self.request, f'{why} Use this link instead: {link}')
