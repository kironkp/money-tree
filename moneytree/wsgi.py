"""WSGI config for MoneyTree."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'moneytree.settings')

application = get_wsgi_application()
