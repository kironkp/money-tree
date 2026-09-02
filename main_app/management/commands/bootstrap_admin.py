"""Create or update the owner account from DJANGO_SUPERUSER_* in .env."""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Create/update the superuser from DJANGO_SUPERUSER_USERNAME/EMAIL/PASSWORD'

    def handle(self, *args, **options):
        username = os.getenv('DJANGO_SUPERUSER_USERNAME', '')
        email = os.getenv('DJANGO_SUPERUSER_EMAIL', '')
        password = os.getenv('DJANGO_SUPERUSER_PASSWORD', '')
        if not (username and password):
            raise CommandError('DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD must be set in .env')
        User = get_user_model()
        user, created = User.objects.get_or_create(username=username, defaults={'email': email})
        user.email = email or user.email
        user.is_staff = user.is_superuser = True
        user.set_password(password)
        user.save()
        self.stdout.write(self.style.SUCCESS(f'{"created" if created else "updated"} superuser {username}'))
