"""Display helpers: money, percentages, signed classes, ET timestamps."""
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django import template

register = template.Library()
ET = ZoneInfo('America/New_York')


def _num(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


@register.filter
def money(value, places=2):
    n = _num(value)
    if n is None:
        return '—'
    sign = '-' if n < 0 else ''
    return f'{sign}${abs(n):,.{int(places)}f}'


@register.filter
def signed_money(value):
    n = _num(value)
    if n is None:
        return '—'
    sign = '+' if n > 0 else ('-' if n < 0 else '')
    return f'{sign}${abs(n):,.2f}'


@register.filter
def pct(value, places=2):
    n = _num(value)
    if n is None:
        return '—'
    return f'{n:+.{int(places)}f}%'


@register.filter
def num(value, places=2):
    n = _num(value)
    if n is None:
        return '—'
    return f'{n:,.{int(places)}f}'


@register.filter
def qty(value):
    n = _num(value)
    if n is None:
        return '—'
    if n == n.to_integral_value():
        return f'{int(n):,}'
    return f'{n.normalize():f}'


@register.filter
def sign_class(value):
    n = _num(value)
    if n is None or n == 0:
        return 'flat'
    return 'up' if n > 0 else 'down'


@register.filter
def et(value, fmt='%b %d %H:%M'):
    if not value:
        return '—'
    try:
        return value.astimezone(ET).strftime(fmt)
    except (AttributeError, ValueError):
        return str(value)


@register.filter
def et_time(value):
    return et(value, '%H:%M:%S')


@register.filter
def get(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def duration(seconds):
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return '—'
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m {s % 60}s'
    return f'{s // 3600}h {(s % 3600) // 60}m'


import json as _json
import re as _re

from django.utils.html import escape as _escape
from django.utils.safestring import mark_safe as _mark_safe


@register.simple_tag
def qs_replace(request, key, value):
    """Current query string with one key replaced (pagination links)."""
    q = request.GET.copy()
    q[key] = value
    return '?' + q.urlencode()


@register.filter
def joinlist(value, sep=', '):
    if isinstance(value, (list, tuple)):
        return sep.join(str(v) for v in value)
    return '' if value is None else str(value)


@register.filter
def json(value):
    return _json.dumps(value, default=str)


@register.filter
def json_pretty(value):
    return _json.dumps(value, default=str, indent=1)


@register.filter
def md(value):
    """Tiny markdown: **bold**, - bullets, blank-line paragraphs. Escapes first."""
    text = _escape(value or '')
    text = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    out, in_list = [], False
    for para in text.split('\n\n'):
        lines = para.split('\n')
        block = []
        for line in lines:
            if line.startswith('- '):
                if not in_list:
                    block.append('<ul>')
                    in_list = True
                block.append(f'<li>{line[2:]}</li>')
            else:
                if in_list:
                    block.append('</ul>')
                    in_list = False
                block.append(line)
        if in_list:
            block.append('</ul>')
            in_list = False
        out.append('<p>' + '<br>'.join(block) + '</p>' if not block[0].startswith('<ul>') else ''.join(block))
    return _mark_safe(''.join(out))


@register.filter
def pct_of(value, total):
    try:
        return float(value) / float(total) * 100 if float(total) else 0
    except (TypeError, ValueError):
        return 0
