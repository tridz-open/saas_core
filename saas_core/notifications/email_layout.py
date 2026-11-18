"""One shared HTML layout for every transactional email (saas-* here; a recipe may vendor this file for its own templates).

Design: neutral page (#F6F6F4), white card, crimson wordmark straight on the card (no header band, no wrapper box),
one solid CTA, neutral grey info box. Dark mode: neutral greys via prefers-color-scheme (Apple Mail, Outlook.com)
plus a light/dark logo swap. Gmail inverts clients-side, so the only saturated fill is the small CTA.

Pure Python (no frappe import) so build scripts can use it. Output is a Jinja template string (rendered by frappe).
"""
SANS = "'Geist',-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
MONO = "'Geist Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace"
INK, BODY, MUTED, LINE, PAGE = "#0D0D0F", "#4B5563", "#6B7280", "#E5E7EB", "#F6F6F4"
DEFAULT_CTA = "#AA1845"

DARK_CSS = (
    ":root{color-scheme:light dark;supported-color-schemes:light dark}"
    "@media (prefers-color-scheme:dark){.bg{background-color:#111113!important}.card{background-color:#1C1C1F!important;border-color:#2E2E33!important}"
    ".tx{color:#F5F5F5!important}.bd{color:#D4D4D8!important}.mu{color:#A1A1AA!important}.bx{background-color:#232327!important;border-color:#2E2E33!important}"
    ".ln{border-color:#2E2E33!important}.logo-light{display:none!important;max-height:0!important}"
    ".logo-dark{display:block!important;max-height:none!important;overflow:visible!important}}"
    "[data-ogsc] .bg{background-color:#111113!important}[data-ogsc] .card{background-color:#1C1C1F!important;border-color:#2E2E33!important}"
    "[data-ogsc] .tx{color:#F5F5F5!important}[data-ogsc] .bd{color:#D4D4D8!important}[data-ogsc] .mu{color:#A1A1AA!important}"
    "[data-ogsc] .bx{background-color:#232327!important;border-color:#2E2E33!important}"
    "[data-ogsc] .logo-light{display:none!important}[data-ogsc] .logo-dark{display:block!important;max-height:none!important;overflow:visible!important}"
)
MOBILE_CSS = ("@media only screen and (max-width:480px){.cta{width:100%!important}.cta td{display:block!important;width:100%!important}"
              ".cta a{display:block!important;text-align:center!important;box-sizing:border-box}.px{padding-left:20px!important;padding-right:20px!important}}")


def btn(url, label, cta=DEFAULT_CTA):
    """Bulletproof button: table/td fill + `v:roundrect` fallback for Outlook desktop (Word engine ignores radius)."""
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="cta" style="margin:24px 0"><tr>'
        f'<td bgcolor="{cta}" style="border-radius:8px;background-color:{cta};border:1px solid {cta}">'
        f'<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="{url}" arcsize="17%" strokecolor="{cta}" fillcolor="{cta}" '
        f'style="height:46px;v-text-anchor:middle;width:240px"><w:anchorlock xmlns:w="urn:schemas-microsoft-com:office:word"/>'
        f'<center style="color:#ffffff;font-family:Arial,sans-serif;font-size:16px;font-weight:600">{label}</center></v:roundrect><![endif]-->'
        f'<!--[if !mso]><!--><a href="{url}" target="_blank" style="display:inline-block;padding:13px 24px;font-family:{SANS};font-size:16px;'
        f'font-weight:600;line-height:20px;color:#ffffff;text-decoration:none;border-radius:8px">{label}</a><!--<![endif]--></td></tr></table>')


def paste(url):
    """Visible copy of the CTA URL: the plain-text alternative is derived from the HTML, so this puts the URL in it."""
    return (f'<p class="mu" style="margin:0 0 12px;font-size:13px;line-height:20px;color:{MUTED}">Button not working? Paste this link into your browser:<br>'
            f'<span style="word-break:break-all">{url}</span></p>')


def box(inner):
    return (f'<div class="bx" style="background-color:#F9FAFB;border:1px solid {LINE};border-radius:8px;padding:16px 18px;margin:0 0 12px;'
            f'font-size:14px;line-height:21px;color:{INK}"><span class="tx" style="color:{INK}">{inner}</span></div>')


def h(t):
    return f'<h1 class="tx" style="margin:0 0 12px;font-family:{SANS};font-size:22px;line-height:30px;font-weight:600;color:{INK}">{t}</h1>'


def p(t):
    return f'<p class="bd" style="margin:0 0 12px;color:{BODY}">{t}</p>'


def num(t):
    return f'<span class="tx" style="font-family:{MONO};font-weight:600;color:{INK};font-variant-numeric:tabular-nums">{t}</span>'


def row(label, value, guard):
    """One label/value line, rendered only when `guard` (a jinja expression) is truthy."""
    return (f'{{% if {guard} %}}<tr><td class="bd ln" style="padding:8px 0;color:{BODY};font-size:14px;border-top:1px solid {LINE}">{label}</td>'
            f'<td align="right" class="tx ln" style="padding:8px 0;font-size:14px;color:{INK};border-top:1px solid {LINE}">{value}</td></tr>{{% endif %}}')


def rows(*rs):
    return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 16px">' + "".join(rs) + '</table>'


def shell(title, preheader, body, *, brand="{{ brand }}", alt=None, cta=DEFAULT_CTA, footer=True, hours="", support="{{ support_email }}"):
    """The single layout. `brand` is the wordmark fallback / "Sent by" text; `hours` an optional footer string."""
    alt = alt or brand
    logo = (f'{{% if logo_url %}}<img class="logo-light" src="{{{{ logo_url }}}}" width="84" height="28" alt="{alt}" '
            f'style="display:block;border:0;width:84px;height:28px;font-family:{SANS};font-size:22px;line-height:28px;font-weight:600;color:{DEFAULT_CTA}">'
            f'{{% if logo_url_dark %}}<!--[if !mso]><!--><img class="logo-dark" src="{{{{ logo_url_dark }}}}" width="84" height="28" alt="{alt}" '
            f'style="display:none;max-height:0;overflow:hidden;mso-hide:all;border:0;width:84px;height:28px"><!--<![endif]-->{{% endif %}}'
            f'{{% else %}}<span class="tx" style="font-family:{SANS};font-size:22px;line-height:28px;font-weight:600;color:{DEFAULT_CTA}">{brand}</span>{{% endif %}}')
    contact = f'<a href="mailto:{support}" style="color:inherit;text-decoration:underline">{support}</a>' + (f' &middot; {hours}' if hours else "")
    foot = (f'<tr><td class="mu px" style="padding:20px 8px 0;font-family:{SANS};font-size:12px;line-height:18px;color:{MUTED}">'
            f'{contact}<br>Sent by {brand}.</td></tr>') if footer else ""
    return f'''<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark"><title>{title}</title>
<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
<style>{MOBILE_CSS}{DARK_CSS}</style></head>
<body class="bg" style="margin:0;padding:0;background-color:{PAGE};-webkit-text-size-adjust:100%;font-family:{SANS}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;font-size:1px;line-height:1px">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="bg" bgcolor="{PAGE}" style="background-color:{PAGE}"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:560px">
<tr><td class="card px" bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid {LINE};border-radius:8px;padding:32px;font-family:{SANS};color:{BODY};font-size:15px;line-height:24px">
<div style="margin:0 0 24px">{logo}</div>
{body}
</td></tr>
{foot}
</table></td></tr></table></body></html>'''
