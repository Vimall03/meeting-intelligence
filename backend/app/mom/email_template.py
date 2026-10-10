"""Small table-based email, inline CSS, and fully escaped model text.

No external images, fonts, scripts, stylesheets, forms, or client-dependent CSS.
This is presentation only: the underlying minutes remain plain text.
"""
from datetime import datetime, timezone
from html import escape
import re
from zoneinfo import ZoneInfo

HEADINGS = {'key points', 'decisions', 'next steps', 'open items'}


def render_minutes(minutes, row):
    date = datetime.fromtimestamp(row['startedAt'], timezone.utc).astimezone(ZoneInfo(row['timezone']))
    date_text = date.strftime('%d %B %Y · %H:%M') + ' · ' + row['timezone']
    content = []
    for line in minutes.splitlines():
        line = line.strip()
        if not line: continue
        heading = line.strip('*# ').rstrip(':').strip()
        if heading.lower() in HEADINGS:
            content.append('<h2 style="margin:24px 0 10px;color:#343849;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:22px;">' + escape(heading) + '</h2>')
        else:
            text = re.sub(r'^[*\-•]\s+', '', line)
            bullet = text != line
            content.append('<p style="margin:0 0 10px;color:#4d5566;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:23px;">' + ('<span style="color:#6554c0;">&#8226;&nbsp;</span>' if bullet else '') + escape(text) + '</p>')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Your meeting recap</title></head>
<body style="margin:0;padding:0;background-color:#f5f6fa;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f5f6fa;"><tr><td align="center" style="padding:28px 16px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px;">
<tr><td style="padding:0 0 16px;color:#443879;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:bold;">Meeting Intelligence</td></tr>
<tr><td style="padding:28px;background-color:#ffffff;border:1px solid #e2e5ed;border-radius:10px;">
<p style="margin:0 0 8px;color:#75688f;font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:bold;letter-spacing:1px;">MEETING RECAP</p>
<h1 style="margin:0 0 8px;color:#252a38;font-family:Arial,Helvetica,sans-serif;font-size:24px;line-height:32px;">Your meeting, clearly captured.</h1>
<p style="margin:0 0 22px;color:#7b8292;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:18px;">''' + escape(date_text) + '''</p>
''' + '\n'.join(content) + '''
</td></tr><tr><td style="padding:16px 4px 0;color:#7b8292;font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:18px;">
Based on the part of your meeting captured by Meeting Intelligence. You received this because meeting minutes are enabled for your account.
<br><a href="https://meetingintelligence.auriqis.com/privacy" style="color:#716982;text-decoration:underline;">Privacy policy</a>
</td></tr></table></td></tr></table></body></html>'''
