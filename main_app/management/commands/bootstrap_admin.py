"""Create or update the owner account from DJANGO_SUPERUSER_* in .env, with
the verified EmailAddress row allauth's email login needs."""
import os

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Create/update the superuser from DJANGO_SUPERUSER_USERNAME/EMAIL/PASSWORD'

    def handle(self, *args, **options):
        username = os.getenv('DJANGO_SUPERUSER_USERNAME', '')
        email = os.getenv('DJANGO_SUPERUSER_EMAIL', '')
        password = os.getenv('DJANGO_SUPERUSER_PASSWORD', '')
        if not (username and email and password):
            raise CommandError('DJANGO_SUPERUSER_USERNAME, _EMAIL and _PASSWORD must be set in .env')
        User = get_user_model()
        user, created = User.objects.get_or_create(username=username, defaults={'email': email})
        user.email = email
        user.is_staff = user.is_superuser = True
        user.set_password(password)
        user.save()
        EmailAddress.objects.filter(user=user, primary=True).exclude(email=email).update(primary=False)
        EmailAddress.objects.update_or_create(user=user, email=email, defaults={'verified': True, 'primary': True})
        self.stdout.write(self.style.SUCCESS(f'{"created" if created else "updated"} superuser {username} <{email}> (verified)'))
