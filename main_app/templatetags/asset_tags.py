"""Cache-busting {% static %}.

The dev server sends no ETag or Cache-Control for static files, so a phone
browser will happily keep serving a stale base.css after an edit. Appending the
file's mtime changes the URL whenever the file changes, which no cache can
ignore. In production WhiteNoise already hashes filenames, so this is a no-op
there (the file isn't findable in the source tree and the URL passes through).
"""
import pathlib

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def static_v(path):
    url = static(path)
    found = finders.find(path)
    if not found:
        return url
    try:
        stamp = int(pathlib.Path(found).stat().st_mtime)
    except OSError:
        return url
    sep = '&' if '?' in url else '?'
    return f'{url}{sep}v={stamp}'
