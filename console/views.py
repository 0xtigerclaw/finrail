"""Shared HTML presentation. Financial decisions remain in the dispatcher."""
from __future__ import annotations

import html
import json
from datetime import datetime
from urllib.parse import quote


def e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def icon(name: str, size: int = 18) -> str:
    paths = {
        'arrow': "<path d='M5 12h14m-5-5 5 5-5 5'/>",
        'chevron': "<path d='m9 5 7 7-7 7'/>",
        'menu': "<path d='M4 6h16M4 12h16M4 18h16'/>",
        'close': "<path d='m6 6 12 12M18 6 6 18'/>",
        'search': "<circle cx='10.5' cy='10.5' r='6.5'/><path d='m16 16 4 4'/>",
        'building': "<path d='M4 21V4h11v17M15 10h5v11M2 21h20M8 8h3M8 12h3M8 16h3'/>",
        'file': "<path d='M6 3h8l5 5v13H6zM14 3v6h5M9 13h7M9 17h5'/>",
        'shield': "<path d='m12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6zM8 12l3 3 5-6'/>",
        'clock': "<circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/>",
        'check': "<path d='m5 12 4 4L19 6'/>",
        'alert': "<path d='m12 3 10 18H2zM12 9v4M12 17h.01'/>",
        'copy': "<rect x='8' y='8' width='12' height='13' rx='2'/><path d='M15 8V3H3v13h5'/>",
        'info': "<circle cx='12' cy='12' r='9'/><path d='M12 11v6M12 7h.01'/>",
        'terminal': "<rect x='3' y='4' width='18' height='16' rx='2'/><path d='m7 9 3 3-3 3M12 15h5'/>",
        'activity': "<path d='M3 12h4l3-7 4 14 3-7h4'/>",
        'refresh': "<path d='M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 13-3l2 3M4 16l2 3a8 8 0 0 0 13-3'/>",
    }
    return (f"<svg width='{size}' height='{size}' viewBox='0 0 24 24' fill='none' "
            "stroke='currentColor' stroke-width='1.6' stroke-linecap='round' "
            "stroke-linejoin='round' aria-hidden='true'>" + paths.get(name, paths['file']) + "</svg>")


STATUS_LABELS = {'done': 'Completed', 'pending': 'Awaiting approval', 'running': 'Running',
                 'failed': 'Failed', 'verified': 'Verified', 'reported': 'Reported',
                 'rejected': 'Rejected', 'read': 'Read only', 'planned': 'Planned',
                 'judged': 'Model assisted', 'checked': 'Checked source',
                 'unverified_draft': 'Unverified draft', 'dropped': 'Dropped'}


def badge(status, label: str = '') -> str:
    status = str(status or 'pending').lower()
    safe = status if status in {*STATUS_LABELS, 'model_call', 'info', 'synthetic'} else 'info'
    return f"<span class='badge b-{safe}'>{e(label or STATUS_LABELS.get(status, status.replace('_', ' ').title()))}</span>"


def panel(title: str, body: str, action: str = '', extra: str = '') -> str:
    return (f"<section class='panel {e(extra)}'><div class='panel-header'><h2>{e(title)}</h2>"
            f"{action}</div><div class='panel-body'>{body}</div></section>")


def empty(title: str, description: str, action: str = '', glyph: str = 'file') -> str:
    return (f"<div class='empty-state'><span class='empty-icon'>{icon(glyph, 24)}</span>"
            f"<h3 class='empty-title'>{e(title)}</h3><p class='empty-description'>{e(description)}</p>{action}</div>")


def table(headers: list[str], rows: str, caption: str = '') -> str:
    head = ''.join(f"<th scope='col'>{e(h)}</th>" for h in headers)
    return ("<div class='table-wrap' tabindex='0' role='region' "
            f"aria-label='{e(caption or 'Data table')}'><table class='data-table'>"
            + (f"<caption class='sr-only'>{e(caption)}</caption>" if caption else '')
            + f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>")


def cli(cmd: str) -> str:
    return (f"<div class='term'><code class='cmd'>{e(cmd)}</code>"
            f"<button type='button' class='copy-button' data-copy='{e(cmd)}' aria-label='Copy command'>"
            f"{icon('copy', 16)}<span>Copy</span></button></div>")


def cli_equiv(cmd: str) -> str:
    return ("<details class='cli-equivalent disclosure'><summary>Same thing from the terminal</summary>"
            + cli(cmd) + "</details>")


def raw(data, title: str = 'Raw record (JSON)') -> str:
    return (f"<details class='disclosure'><summary>{e(title)}</summary>"
            f"<pre class='raw'>{e(json.dumps(data, indent=2, default=str))}</pre></details>")


def fields(data: dict) -> str:
    items = []
    for key, value in data.items():
        if value is None or value == '':
            continue
        shown = json.dumps(value, indent=2, default=str) if isinstance(value, (dict, list)) else value
        items.append(f"<div class='result-field'><dt class='result-label'>{e(key.replace('_', ' ').capitalize())}</dt>"
                     f"<dd class='result-value'>{e(shown)}</dd></div>")
    return "<dl class='result-grid'>" + ''.join(items) + "</dl>"


def company_context(name: str, detail: str, initials: str = 'MC') -> str:
    return (f"<div class='company-context'><span class='company-avatar'>{e(initials)}</span>"
            f"<div><strong>{e(name)}</strong><span class='company-meta'>{e(detail)}</span></div>"
            "<span class='context-tag'>Synthetic dataset</span></div>")


def time_label(value) -> str:
    if not value:
        return 'Time not recorded'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.strftime('%d %b, %H:%M')
    except (TypeError, ValueError):
        return str(value)


def run_link(rid, label='View run') -> str:
    return f"<a class='inline-action' href='/runs/{quote(str(rid), safe='')}'>{e(label)}{icon('arrow',16)}</a>"


_MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
           'October', 'November', 'December')


def long_date(text: str) -> str:
    """Every ISO date in `text` written the way a reader says it: 2026-06-30 -> 30 June 2026."""
    import re
    return re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b",
                  lambda m: f"{int(m[3])} {_MONTHS[int(m[2]) - 1]} {m[1]}", str(text or ''))
