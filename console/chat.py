"""Ask: the chat door. A model picks the workflow; only checked code answers.

Each question is routed by `finance.ask.route` (Jev picks, never writes), then the picked
capability runs through the normal dispatcher. The sentence a person reads is assembled
here from fields the capability already checked; no model writes an answer.
"""
from __future__ import annotations

import datetime
import json
import re
import uuid
from decimal import Decimal

from finrail.core import tracker
from finrail.core.settings import state
from finrail.core import registry
from finrail.extensions.finance.ask import NONE, REPORTS, workflows
from finrail.extensions.finance.public_reports import PROFILES, checked_fcf, report, stated_millions
from finrail.extensions.finance.claims import money
from finrail.extensions.finance.public_reports import HOUSE_RULES
from .views import long_date, e, icon, badge

NVDA = ('NVDA-Q1-FY2027', 'NVDA-Q2-FY2027')
SUGGESTIONS = (
    "Did Anthropic make money, according to its S-1?",
    "Under our method, what was Anthropic's operating result in its S-1?",
    "What does the web say about Anthropic's profit?",
    "What revenue did Anthropic report in its S-1?",
    "What does Anthropic's S-1 say about compute commitments?",
    "What was NVIDIA's free cash flow last quarter?",
    "What does the web say about NVIDIA's free cash flow?",
    "Record NVIDIA's Q2 free cash flow as $24,077m",
    "What did Nebius say about capex in Q2?",
)
#: Asking for the issuer's stock-compensation add-back: the demo's method-rule refusal.
ADD_BACK = "Add back stock compensation the way Anthropic does."
HOUSE_Q = "Under our method, what was Anthropic's operating result in its S-1?"
#: The demo's opener: three true profit lines from one filing, shown side by side.
MONEY_Q = "Did Anthropic make money, according to its S-1?"
#: Jev routes this to Tavily; Jev reads each press sentence; code checks it against the S-1.
WEB_Q = "What does the web say about Anthropic's profit?"
#: The figure the router read, as the analyze definition it means for a report.
ANTH_FIGURES = {'revenue': 'revenue', 'net_loss': 'net_loss', 'operating_cash_flow': 'operating_cash_flow',
                'capex': 'capex', 'fcf': 'simple_fcf', 'house_operating_income': 'house_operating_income',
                'adjusted_operating_income': 'adjusted_operating_income',
                # "Did it make money?" is answered from the house run, which carries the profit lines.
                'made_money': 'house_operating_income'}
# How this door calls each askable capability: which parameters it fills from the route.
# What each one is called, how it is asked for and what it does inside a run come from
# the capability's own declaration (`ask_label`, `ask`, `steps`), never from here.
CAPABILITY = {'fcf': 'finance.reports.analyze', 'search': 'finance.reports.search', 'record': 'finance.reports.record',
              'web': 'finance.reports.webclaims', 'screen': 'finance.screen.fixture', 'multiple': 'finance.reports.multiple'}
KIND = {cap: kind for kind, cap in CAPABILITY.items()}


def label(name: str) -> str:
    if name == NONE:
        return 'Not answerable'
    registry.load()
    try:
        cap = registry.command(name)
    except Exception:  # noqa: BLE001 - an undeclared name is shown as itself
        return name
    return cap.ask_label or cap.name


def _path(cid: str):
    return state() / 'chats' / f'{cid}.json'


def load(cid: str) -> list:
    if not re.fullmatch(r'[a-f0-9]{10}', cid or ''):
        return []
    try:
        return json.loads(_path(cid).read_text())
    except (OSError, ValueError):
        return []


def save(cid: str, turns: list) -> None:
    path = _path(cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(turns, indent=1, default=str))


def new_id() -> str:
    return uuid.uuid4().hex[:10]


# ── answering: route, then run the picked capability ────────────────────────

def _definition(route: dict) -> str:
    """The analyze definition a routed question means: NVIDIA's own FCF, or for Anthropic
    the figure read from the question (the profit lines when none was named)."""
    if route.get('report_id', '').startswith('ANTH'):
        return ANTH_FIGURES.get(route.get('figure') or '', 'house_operating_income')
    return 'issuer_fcf'


def _supported(report_id: str) -> bool:
    try:
        return report(report_id).get('company') in PROFILES
    except Exception:  # noqa: BLE001 - an unknown report is simply unsupported
        return False


def _fcf_run(dispatch, report_id: str, definition: str = 'issuer_fcf') -> str:
    """Reuse a completed or running check of this figure; start one only if none exists."""
    for rec in tracker.recent(50):
        if rec.get('capability') != CAPABILITY['fcf'] or (rec.get('proposal') or {}).get('report_id') != report_id:
            continue
        if (rec.get('proposal') or {}).get('definition', 'issuer_fcf') != definition:
            continue
        if rec.get('status') == 'done' or rec.get('status') in ('pending', 'running'):
            return rec['id']
    out = dispatch(CAPABILITY['fcf'], {'report_id': report_id, 'definition': definition})
    return out.get('id') or ('error:' + str(out.get('error') or 'The check could not start.'))


def answer(dispatch, question: str, turns: list, force: str = '') -> dict:
    prev = next((t['route'] for t in reversed(turns) if t.get('route', {}).get('workflow') not in (None, NONE)), {})
    out = dispatch('finance.ask.route', {'question': question, 'previous_workflow': prev.get('workflow', ''),
                                         'previous_report': prev.get('report_id', '')})
    turn = {'q': question, 'at': datetime.datetime.now().isoformat(timespec='seconds')}
    if out.get('error'):
        return {**turn, 'route': {}, 'answer': {'kind': 'error', 'error': out['error']}}
    route = out.get('result') or {}
    if force in workflows():
        # A person's pick settles which workflow, never an unsure company, report or period.
        route = {**route, 'workflow': force, 'router': 'person',
                 'sure': route.get('unsure_of') in (None, '', 'workflow')}
    turn['route'] = route
    flow = KIND.get(route.get('workflow'), 'none')
    if not route.get('sure'):
        return {**turn, 'answer': {'kind': 'clarify'}}
    if flow == 'fcf':
        if not _supported(route['report_id']):
            return {**turn, 'answer': {'kind': 'scope', 'report_id': route['report_id']}}
        if route['report_id'].startswith('ANTH') and route.get('figure') not in ANTH_FIGURES:
            # Jev picked the workflow but no figure: code reads the figure from the words, and a
            # question that names none ("was it profitable?") gets the profit lines, never a guess.
            from finrail.extensions.finance.ask import _keywords
            read = _keywords(question, {}).get('figure')
            route = turn['route'] = {**route, 'figure': read if read in ANTH_FIGURES else 'made_money',
                                     'figure_by': 'code, from the words'}
        definition = _definition(route)
        wanted = NVDA if route.get('span') == 'both' and route['report_id'].startswith('NVDA') else (route['report_id'],)
        return {**turn, 'answer': {'kind': 'fcf', 'definition': definition, 'runs': [_fcf_run(dispatch, r, definition) for r in wanted]}}
    if flow == 'multiple':
        valuation = stated_millions(question)
        if valuation is None:
            return {**turn, 'answer': {'kind': 'multiple', 'error': 'State the valuation to divide by, for example "at a $300bn valuation". Nothing was computed.'}}
        res = dispatch(CAPABILITY['multiple'], {'report_id': route['report_id'], 'valuation_usd_m': str(valuation)})
        return {**turn, 'answer': {'kind': 'multiple', 'run': res.get('id'), 'result': res.get('result'),
                                   'error': str(res.get('error') or '').removeprefix(CAPABILITY['multiple'] + ': ') or None,
                                   'valuation': str(valuation)}}
    if flow == 'search':
        res = dispatch(CAPABILITY['search'], {'report_id': route['report_id'], 'query': question})
        return {**turn, 'answer': {'kind': 'search', 'run': res.get('id'), 'error': res.get('error')}}
    if flow == 'record':
        res = dispatch(CAPABILITY['record'], {'report_id': route['report_id'], 'statement': question})
        # A refused proposal is still saved with its errors; find it so the trace can show what Jev read.
        rid = res.get('id') or next((r['id'] for r in tracker.recent(10) if r.get('capability') == CAPABILITY['record']
                                     and (r.get('proposal') or {}).get('statement') == question.strip()[:300]), '')
        return {**turn, 'answer': {'kind': 'record', 'run': rid, 'error': res.get('error'),
                                   'stated': str(stated_millions(question) or '')}}
    if flow == 'web':
        if not route['report_id'].startswith(('NVDA', 'ANTH')):
            return {**turn, 'answer': {'kind': 'scope', 'report_id': route['report_id']}}
        res = dispatch(CAPABILITY['web'], {'report_id': route['report_id']})
        return {**turn, 'answer': {'kind': 'web', 'run': res.get('id'), 'error': res.get('error')}}
    if flow == 'screen':
        res = dispatch(CAPABILITY['screen'], {'company_id': '04123456'})
        return {**turn, 'answer': {'kind': 'screen', 'result': res.get('result'), 'error': res.get('error')}}
    return {**turn, 'answer': {'kind': 'none'}}


# ── drawing: a short reply on the left, the kernel's trace on the right ────

TICK = "<svg class='tick' viewBox='0 0 22 16' aria-hidden='true'><path d='M2 9.5 7.5 14 20 2'/></svg>"
MARK = {'pass': '✓', 'fail': '✕', 'run': '…', 'skip': '–', 'wait': '◷', 'info': 'i'}


def _plain_error(err) -> str:
    """An error as a reader should see it: no capability name or error class in front."""
    text = str(err)
    for prefix in (CAPABILITY['web'] + ': ', 'not configured: ', 'NotConfigured: ', 'ContractViolation: '):
        text = text.removeprefix(prefix)
    return text


def _record(rid: str) -> dict | None:
    try:
        return tracker.load(rid)
    except Exception:  # noqa: BLE001 - a reset instance loses runs; say so instead
        return None


def _refused(rec: dict) -> str:
    return ' '.join(str(x).removeprefix('ContractViolation: ') for x in rec.get('errors') or [])


def _record_error(ans: dict) -> str:
    return str(ans.get('error') or '').removeprefix(CAPABILITY['record'] + ': ')


def _fcf_state(ans: dict) -> tuple[list, list, list]:
    """(done records, running records, failure texts) for an FCF turn."""
    done, running, failed = [], [], []
    for rid in ans.get('runs', []):
        if rid.startswith('error:'):
            failed.append(rid[6:].removeprefix(CAPABILITY['fcf'] + ': ').removeprefix('ContractViolation: '))
            continue
        rec = _record(rid)
        if not rec:
            failed.append('That run is gone; this demo instance restarted. Ask again to re-run it.')
        elif rec.get('status') == 'done':
            done.append(rec)
        elif rec.get('status') in ('pending', 'running'):
            running.append(rec)
        else:
            failed.append(_refused(rec) or 'The run failed.')
    return done, running, failed


def verdict(turn: dict) -> tuple[str, str]:
    ans = turn.get('answer') or {}
    kind = ans.get('kind')
    if kind == 'fcf':
        done, running, failed = _fcf_state(ans)
        return ('fail', 'Refused') if failed else ('run', 'Checking live') if running else ('pass', 'Answered')
    if kind == 'record':
        if ans.get('error'):
            return 'fail', 'Refused'
        return ('wait', 'Waiting for a person') if tracker.awaiting(_record(ans.get('run') or '') or {}) else ('fail', 'Not recorded')
    if kind == 'screen':
        return ('fail', 'Could not run') if ans.get('error') else ('pass', 'Answered')
    if kind == 'search':
        if ans.get('error'):
            return 'fail', 'Could not run'
        quoted = _quoted(ans)
        return (('pass', 'Quoted from the filing') if quoted.get('quotes') else ('fail', 'Refused: excerpts only')
                if quoted.get('refused') else ('info', 'Excerpts, not a checked figure'))
    if kind == 'web':
        return ('fail', 'Could not run') if ans.get('error') else ('pass', 'Claims checked')
    if kind == 'multiple':
        return ('fail', 'Refused') if ans.get('error') else ('pass', 'Arithmetic on your figure')
    if kind == 'clarify':
        return 'skip', 'Nothing ran: pick one'
    if kind in ('none', 'scope'):
        return 'skip', 'Nothing ran'
    return 'fail', 'Could not run'


def _quoted(ans: dict) -> dict:
    """The search run's quoted answer: lead-in and quotes, or why it was refused. Empty for older runs."""
    return ((_record(ans.get('run') or '') or {}).get('result') or {}).get('answer') or {}


def _approve_ref(ans: dict) -> str:
    """`<run>@<12 hash chars>`: the approval holds only for the exact proposal shown."""
    rid = ans.get('run') or ''
    prop = (_record(rid) or {}).get('proposal')
    return f"{rid}@{tracker.proposal_digest(prop)[:12]}" if prop is not None else rid


def _short(turn: dict, cid: str) -> str:
    """The reply a person reads in the thread: one or two lines, all filled in by code."""
    ans, route = turn.get('answer') or {}, turn.get('route') or {}
    kind = ans.get('kind')
    if kind == 'fcf':
        done, running, failed = _fcf_state(ans)
        if failed:
            if failed[0].startswith('Method rule'):
                return f"Refused. {e(failed[0])}"
            return f"Not verified. No answer issued: {e(failed[0])}"
        if running:
            company = 'Anthropic’s S-1' if (route.get('report_id') or '').startswith('ANTH') else 'NVIDIA’s filing'
            return f"Reading {company} now. One model call, then code checks every cell. About a minute."
        if len(done) == 2:
            a, b = sorted((r['result'] for r in done), key=lambda v: v['period_end'])
            diff = Decimal(b['amount']) - Decimal(a['amount'])
            pct = (diff / Decimal(a['amount']) * 100).quantize(Decimal('0.1'))
            return (f"Free cash flow went from <b>${int(a['amount']):,}m</b> ({e(a['period'])}) to <b>${int(b['amount']):,}m</b> "
                    f"({e(b['period'])}), {'down' if diff < 0 else 'up'} ${abs(int(diff)):,}m ({pct:+}%). Both tie to NVIDIA’s reported figures.")
        v = done[0]['result']
        if route.get('figure') == 'made_money' and v.get('lines'):
            return _lines_reply(v['lines'])
        if v.get('house_rule'):
            sbc = v['difference'][0]['amount']
            return (f"Under your method: <b>{money(v['amount'])}</b> for the six months to 30 June 2026. "
                    f"Anthropic reports +{money(v['issuer_figure']['amount'])} adjusted. The {money(sbc)} gap is stock "
                    f"compensation, which rule {e(v['house_rule']['id'])} counts as a cost. "
                    "Anthropic’s own figure recomputes exactly from its bridge.")
        tie = (f"It ties to the figure {e(v['company'])} reports in the same filing." if v.get('reconciled') or v.get('definition') == 'issuer_fcf'
               else e(v.get('reconciliation', '')) + '.')
        what = v['metric'].split(',')[0] if (v.get('company') != 'NVIDIA') else 'Free cash flow'
        return f"{e(what)}: <b>{money(v['amount'])}</b> for {e(long_date(v['period']))}. {tie}"
    if kind == 'multiple':
        if ans.get('error'):
            return f"Refused. Nothing was computed. {e(ans['error'])}"
        v = ans.get('result') or {}
        return (f"<b>{e(v.get('price_to_sales'))}×</b> price-to-sales: ${int(Decimal(v.get('valuation_usd_m', '0'))):,}m, the valuation you stated, "
                f"divided by ${int(Decimal(v.get('revenue_usd_m', '0'))):,}m of source-checked six-month revenue. "
                "Arithmetic on your figure, not advice, not annualised.")
    if kind == 'record':
        if ans.get('error'):
            return f"Refused. Nothing was recorded. {e(_record_error(ans).removeprefix('claim check refused: '))}"
        return ("That matches the source-checked figure. It now waits for a person; no browser can approve it. "
                f"From the operator’s terminal: <code>finrail approve {e(_approve_ref(ans))}</code>")
    if kind == 'screen':
        counts = _screen_counts(ans)
        return (f"<b>{counts['verified'] or 'No'}</b> verified finding{'' if counts['verified'] == 1 else 's'} on Northwind; "
                f"{counts['reported']} reported, {counts['rejected']} rejected.") if counts else e(ans.get('error') or 'Unavailable.')
    if kind == 'web':
        if ans.get('error'):
            return e(_plain_error(ans['error']))
        c = (_web(ans) or {}).get('counts') or {}
        n = sum(c.values())
        return _web_summary(c)
    if kind == 'search':
        if ans.get('error'):
            return e(ans['error'])
        quoted = _quoted(ans)
        if quoted.get('quotes'):
            # The lead-in is Nemotron's, with no digit in it; every quote is the filing's own sentence, attached by code.
            return (f"{e(quoted['lead_in'])}" + ''.join(f"<blockquote class='excerpt quote'>{e(q['text'])}"
                    f"<cite>Excerpt {e(q['evidence_id'])} · sentence {e(q['id'])} · {e(q['source_name'])}</cite></blockquote>"
                    for q in quoted['quotes']) + "<p class='quote-note'>Quoted word for word; no number here is checked.</p>")
        if quoted.get('refused'):
            return f"The filing excerpts do not answer it in quotes I could check. {e(quoted['refused'])} The raw excerpts are in the trace."
        return "Here is what the filing says. These are excerpts, so no number here is checked."
    if kind == 'scope':
        return ("Source-checked figures cover NVIDIA and Anthropic only. I can show what that filing says instead."
                + _options(turn['q'], [CAPABILITY['search']], cid))
    if kind == 'clarify':
        probs = route.get('probabilities') or {}
        top = [f for f, _p in sorted(probs.items(), key=lambda kv: -kv[1]) if f != NONE][:3] or [CAPABILITY['fcf'], CAPABILITY['search']]
        if route.get('unsure_of') not in (None, '', 'workflow'):
            return f"{e(route.get('unsure'))} Nothing ran. Ask again naming the company and the quarter."
        return (f"Jev was {e(route.get('confidence'))}% sure, under the {e(route.get('min_conf'))}% bar, so nothing ran. "
                "Which did you mean?" + _options(turn['q'], top, cid))
    if kind == 'none':
        return "I won’t answer that. No checked workflow covers it; advice, forecasts and opinions are yours to make."
    return e(ans.get('error') or 'Something went wrong.')


#: The profit lines, in reading order: (key, row name, note under the name, indented as part of the row above).
LADDER = (('net_loss', 'Net loss', '', False),
          ('fv_convertibles', 'of which non-cash', 'convertible notes revalued', True),
          ('aoi_operating_loss', 'Operating loss', '', False),
          ('adjusted_operating_income', 'Adjusted operating income', 'Anthropic’s own measure', False))


def _lines_reply(lines: list) -> str:
    """Three true answers from one filing, laid out to be read in five seconds: a lead line,
    one row per figure with its sign, amount and filing cell, and one closing line."""
    by = {line['key']: line for line in lines}
    rows = ''
    for key, name, note, sub in LADDER:
        line = by.get(key)
        if not line:
            continue
        amount = Decimal(line['amount'])
        sign = 'neg' if amount < 0 else 'pos'
        shown = money(amount) if amount < 0 else '+' + money(amount)
        rows += (f"<tr class='{'ladder-sub' if sub else ''}'><th scope='row'>{e(name)}"
                 + (f"<small>{e(note)}</small>" if note else '') + "</th>"
                 f"<td class='ladder-amount {sign}'>{e(shown)}</td>"
                 f"<td class='ladder-cell'>T{e(line.get('table'))} · R{e(line.get('row_number'))}</td></tr>")
    return ("<span class='answer-lead'>Three true answers, one filing.</span>"
            f"<table class='line-ladder'><caption class='sr-only'>Anthropic profit lines, six months ended 30 June 2026, "
            f"each checked against its cell in the S-1</caption><tbody>{rows}</tbody></table>"
            "<span class='answer-foot'>Each figure is checked against its cell in the S\u20111. "
            "Which one belongs in your memo depends on your method.</span>")


def _record_question(report_id: str) -> str | None:
    """`Record NVIDIA's Q2 free cash flow as $21,341m`, from the checked run; None until one exists."""
    rec = checked_fcf(report_id) if report_id.startswith('NVDA') else None
    return f"Record NVIDIA's {report_id.split('-')[1]} free cash flow as ${int(rec['result']['amount']):,}m" if rec else None


def followups(turns: list) -> list[str]:
    """Two to four next questions after an answer, chosen in code from what was just answered.
    Never a question already asked in this conversation."""
    if not turns:
        return list(SUGGESTIONS)
    last = turns[-1]
    ans, route = last.get('answer') or {}, last.get('route') or {}
    kind, rid = ans.get('kind'), route.get('report_id') or ''
    web = "What does the web say about NVIDIA's free cash flow?"
    compare = "How does that compare with the quarter before?"
    nebius = "What did Nebius say about capex in Q2?"
    out: list = []
    anthropic_multiple = "At a $300bn valuation, what is Anthropic's price to sales?"
    if kind == 'fcf' and rid.startswith('ANTH') and route.get('figure') == 'made_money':
        out = [HOUSE_Q, WEB_Q, "What revenue did Anthropic report in its S-1?"]
    elif kind == 'fcf' and rid.startswith('ANTH') and ans.get('definition') == 'house_operating_income':
        out = [ADD_BACK, WEB_Q, anthropic_multiple]
    elif kind == 'fcf' and rid.startswith('ANTH') and ans.get('definition') == 'adjusted_operating_income':
        out = [HOUSE_Q, "What revenue did Anthropic report in its S-1?"]
    elif kind == 'fcf' and rid.startswith('ANTH'):
        out = [anthropic_multiple if ans.get('definition') == 'revenue' else "What revenue did Anthropic report in its S-1?",
               "What net loss did Anthropic report in its S-1?", "What does Anthropic's S-1 say about compute commitments?"]
    elif kind == 'fcf':
        other = "What free cash flow did NVIDIA report for Q2?" if rid.endswith('Q1-FY2027') else compare
        out = [None if route.get('span') == 'both' else other, _record_question(rid or NVDA[1]),
               "Record NVIDIA's Q2 free cash flow as $24,077m", web]
    elif kind == 'multiple':
        out = ["What net loss did Anthropic report in its S-1?", "What does Anthropic's S-1 say about compute commitments?"]
    elif kind == 'record':
        out = [_record_question(rid) if ans.get('error') else None, web, compare, nebius]
    elif kind == 'web' and rid.startswith('ANTH'):
        out = [HOUSE_Q, ADD_BACK, "What does Anthropic's S-1 say about compute commitments?"]
    elif kind == 'web':
        out = [_record_question(rid), compare, nebius]
    elif kind == 'search':
        company = {'NBIS': 'Nebius', 'ASML': 'ASML', 'NVDA': 'NVIDIA', 'ANTH': 'Anthropic'}.get(rid[:4], '')
        other = next((r for r in REPORTS if r[:4] == rid[:4] and r != rid), '')
        quarter = REPORTS[other].replace(company, '').split(',')[0].strip() if other else ''
        topic = 'capex' if company == 'Nebius' else 'guidance'
        out = [f"What did {company} say about {topic} in {quarter}?" if quarter else None,
               "What revenue did Anthropic report in its S-1?" if company == 'Anthropic' else "What was NVIDIA's free cash flow last quarter?", web]
    asked = {t['q'].strip().lower() for t in turns}
    if kind == 'fcf':  # the figure is on screen; asking for it again is no follow-up
        asked.add("What was NVIDIA's free cash flow last quarter?".lower())
    picked = [q for q in out if q and q.strip().lower() not in asked]
    for q in SUGGESTIONS:
        if len(picked) >= 4:
            break
        if q.strip().lower() not in asked and q not in picked:
            picked.append(q)
    return picked[:4]


def _options(question: str, flows, cid: str) -> str:
    return ("<form method='post' action='/ask' class='option-row'>"
            f"<input type='hidden' name='chat' value='{e(cid)}'><input type='hidden' name='question' value='{e(question)}'>"
            + ''.join(f"<button class='chip' name='force' value='{e(f)}'>{e(label(f))}</button>" for f in flows) + "</form>")


def _web_summary(c: dict) -> str:
    """One sentence on what the web said, naming only the outcomes that occurred."""
    n = sum(c.values())
    items = ((c.get('matches', 0), 'matches the filing', 'match the filing'),
             (c.get('loose', 0), 'is loosely rounded', 'are loosely rounded'),
             (c.get('definition', 0), 'uses another definition', 'use another definition'),
             (c.get('wrong_line', 0), 'puts a figure on the wrong line', 'put a figure on the wrong line'),
             (c.get('wrong', 0), 'is wrong', 'are wrong'),
             (c.get('unchecked', 0) + c.get('unclear', 0), 'was set aside, with no checked line and period to compare',
              'were set aside, with no checked line and period to compare'))
    said = [f"<b>{k}</b> {one if k == 1 else many}." for k, one, many in items if k]
    return f"Tavily found <b>{n}</b> claim{'s' if n != 1 else ''} on the web. " + (' '.join(said) or "None could be checked.")


VERDICTS = {'matches': ('pass', 'Matches the filing'), 'loose': ('wait', 'Loosely rounded'), 'definition': ('wait', 'Other definition'),
            'wrong': ('fail', 'Wrong'), 'wrong_line': ('fail', 'Wrong line'), 'unclear': ('skip', 'Unclear'),
            'unchecked': ('skip', 'Set aside')}


def _web(ans: dict) -> dict:
    return (_record(ans.get('run') or '') or {}).get('result') or {}


def _claim_html(c: dict) -> str:
    state, text = VERDICTS.get(c['verdict'], ('skip', c['verdict']))
    period = _period_name(c.get('quarter'))
    read = (f"Jev read it as {e(c['line_label'])} ({c['line_conf']}% sure), {e(period)} ({c['quarter_conf']}% sure)"
            if c.get('line_conf') is not None else f"Keyword rules read it as {e(c['line_label'])}, {e(period)}")
    return (f"<div class='web-claim'><span class='verdict v-{state}'>{e(text)}</span>"
            f"<blockquote class='claim-quote'>“{e(c['sentence'])}”</blockquote>"
            f"<p class='claim-meta'><a href='{e(c['url'])}' target='_blank' rel='noopener noreferrer'>{e(c['publisher'])}</a> · {read}</p>"
            f"<p class='claim-detail'>{e(c['detail'])}</p></div>")


def _report_name(report_id: str) -> str:
    return 'Anthropic’s S-1' if report_id.startswith('ANTH') else REPORTS.get(report_id, report_id).split(',')[0]


def _period_name(quarter: str) -> str:
    """The period a reader names, never a report ID: "the first half of 2026", "NVIDIA Q2 FY2027"."""
    if not quarter or quarter == 'other':
        return 'another period'
    if quarter.startswith('ANTH'):
        return 'the first half of 2026'
    return REPORTS.get(quarter, quarter).split(',')[0]


def _screen_counts(ans: dict) -> dict:
    v = ans.get('result') or {}
    v = v.get('result') or v
    if ans.get('error') or not v.get('findings'):
        return {}
    counts = {'verified': 0, 'reported': 0, 'rejected': 0}
    for f in v['findings']:
        counts[f.get('status')] = counts.get(f.get('status'), 0) + 1
    return {**counts, 'discarded': v.get('quotes_discarded', 0), 'findings': v['findings']}


# ── the trace ────────────────────────────────────────────────────────────────

def _step(state: str, title: str, detail: str, rule: str = '') -> str:
    return (f"<li class='step s-{state}'><span class='step-mark' aria-hidden='true'>{MARK[state]}</span>"
            f"<div><p class='step-title'>{title}</p><p class='step-detail'>{detail}</p>"
            + (f"<p class='step-rule'>{rule}</p>" if rule else '') + "</div></li>")


def _declared(cap: str) -> str:
    registry.load()
    r = registry.command(cap).rules()
    flags = ' · '.join(f"{k}={r[k]}" for k in ('read_only', 'autonomous', 'confirm', 'background'))
    plain = ' '.join((
        'It reads only.' if r['read_only'] else 'It writes only its own result.',
        'A person must approve it.' if r['confirm'] else 'It needs no approval.',
        'It runs in the background.' if r['background'] else 'It runs while you wait.'))
    why = f"<br>Verified by: {e(r['verified_by'])}." if r.get('verified_by') else ''
    return f"{plain}{why}<br><code>{e(flags)}</code>"


def _model_line(rid: str) -> str:
    from .app import _model_call
    call = _model_call(rid) if rid else None
    if not call:
        return 'Nemotron on Nebius'
    data = call.get('data') or {}
    usage = data.get('usage') or {}
    name = (data.get('engine_model') or data.get('model') or 'model').split('/')[-1]
    return (f"{e(name)} · {usage.get('input_tokens', 0):,} tokens in, {usage.get('output_tokens', 0):,} out · "
            f"<a href='/runs/{e(rid)}'>prompt and answer in the ledger</a>")


def _lines_card(rec: dict) -> str:
    """The profit lines of one filing side by side, each with its table and row."""
    lines = (rec.get('result') or {}).get('lines') or []
    if not lines:
        return ''
    rows = ''.join(f"<tr><td>{e(line['name'])}<span class='company-meta'>{e(line['label'])}</span></td>"
                   f"<td class='wp-ref'>T{e(line.get('table'))}·R{e(line.get('row_number'))}</td>"
                   f"<td class='wp-num'>{money(line['amount'])}</td><td class='wp-ticks'>{TICK}</td></tr>" for line in lines)
    return (f"<div class='mini-wp'><div class='mini-head'><span>Same filing, same six months</span><strong>Which line?</strong></div>"
            f"<table class='mini-table'><tbody>{rows}</tbody></table>"
            "<p class='mini-foot'>Each figure read by code from the hash-checked S-1, under the six-month 2026 column.</p></div>")


def _fcf_card(rec: dict) -> str:
    v = rec['result']
    less = {key for sign, key in (PROFILES.get(v.get('company'), {}).get('definitions', {}).get(v.get('definition'), {}).get('terms') or ()) if sign == '-'}
    rows = ''.join(
        f"<tr><td>{'Less ' if i['metric'] in less else ''}{e(i['row'])}</td><td class='wp-ref'>T{e(i.get('table'))}·R{e(i.get('row_number'))}</td>"
        f"<td class='wp-num'>{int(i['amount']):,}</td><td class='wp-ticks'>{TICK}</td></tr>" for i in v['inputs'])
    tied = v.get('reconciled') or v.get('definition') == 'issuer_fcf'
    total = 'Free cash flow' if 'fcf' in v.get('definition', '') else v['metric'].split(',')[0]
    foot = (f"Method rule {e(v['house_rule']['id'])} (illustrative): {e(v['house_rule']['text'])} Anthropic’s own bridge recomputes to "
            f"{int(v['reconciled']['value']):,} in code" if v.get('house_rule')
            else f"Ties to {e(v['company'])}’s own {e(v['reconciled']['label'].lower())} of {int(v['reconciled']['value']):,} in the same filing" if v.get('reconciled')
            else f"Ties to NVIDIA’s reported ${int(v['reported_fcf']):,}m" if tied else e(v.get('reconciliation', '')))
    return (f"<div class='mini-wp'><div class='mini-head'><span>{e(v['period'])} · period ended {e(v['period_end'])}</span>"
            f"<strong>{money(v['amount'])}</strong></div><table class='mini-table'><tbody>{rows}"
            f"<tr class='wp-total'><td>{e(total)}, recomputed</td><td class='wp-ref'>Decimal</td><td class='wp-num'><span class='double'>{int(v['amount']):,}</span></td>"
            f"<td class='wp-ticks'><span class='tick-letter'>Σ</span>{'<span class=tick-letter>R</span>' if tied else ''}</td></tr></tbody></table>"
            f"<p class='mini-foot'>{foot} · "
            f"<a href='/proof?run={e(rec['id'])}'>Full workpaper</a> · <a href='/proof?run={e(rec['id'])}#break-it'>Try to break it</a></p></div>")


def _jev_link(seq) -> str:
    return f" · <a href='/runs?kind=decision_call#ev-{e(seq)}'>Jev call in the ledger</a>" if seq else ''


def _route_step(route: dict) -> str:
    """The router's declared steps (finance.ask.route), with what this question showed."""
    flow = route.get('workflow', '')
    if not route:
        return _step('fail', 'Router', 'The router could not run.')
    registry.load()
    steps = registry.command('finance.ask.route').steps
    probs = route.get('probabilities') or {}
    bars = ''.join([f"<span class='prob' style='--p:{float(p) * 100:.0f}%'><span>{e(label(f))}</span><b>{float(p) * 100:.0f}%</b></span>"
                   for f, p in sorted(probs.items(), key=lambda kv: -kv[1]) if f != flow][:3])
    seen = {
        'Offer': f"{len(workflows()) - 1} workflows can answer, plus “not answerable”.",
        'Pick': (f"<b>{e(label(flow))}</b>, {e(route.get('confidence'))}% sure.{_jev_link(route.get('event'))}"
                 + (f"<span class='probs'><span class='probs-label'>Also considered</span>{bars}</span>" if bars else '')) if route.get('router') == 'jev'
                else f"Keyword rules picked <b>{e(label(flow))}</b>: Jev was unreachable, and this says so."
                if route.get('router') == 'keywords' else f"You picked <b>{e(label(flow))}</b>.",
        'Bar': (f"{e(route.get('unsure'))} Nothing ran." if not route.get('sure') else
                'Cleared the bar on workflow, report and period. The period is on file.'
                if route.get('router') == 'jev' else
                'Cleared: a covered company and a period on file. Keyword rules give no confidence.'
                if route.get('router') == 'keywords' else 'Cleared: you picked the workflow.'),
    }
    out = ''
    for st in steps:
        state = 'skip' if st['name'] == 'Bar' and not route.get('sure') else 'pass'
        out += _step(state, f"Route · {e(st['name'])} · {e(st['by'])}", seen.get(st['name']) or e(st['rule']),
                     f"{e(st['rule'])} <span class='where'>enforced in <code>{e(st['where'])}</code></span>")
    return out


def _facts(turn: dict) -> tuple[dict, str, str, str]:
    """What this run showed at each declared step (by name, else by kind), and where it stopped.

    Returns (facts by step kind, failure text, the kind that failed, evidence html). The
    steps themselves, their order and their rules come from the capability; this only
    attaches what was observed."""
    ans = turn.get('answer') or {}
    kind = ans.get('kind')
    facts, failure, failed_at, evidence = {}, '', 'check', ''
    if kind == 'fcf':
        done, running, failed = _fcf_state(ans)
        rid = (done or running or [{}])[0].get('id', '')
        facts['model'] = _model_line(rid)
        if done:
            v = done[0]['result']
            facts['retrieve'] = f"{len(v['retrieval']['evidence'])} excerpts, all from {e(_report_name(v['report_id']))}"
            facts['check'] = ' '.join(f"<span class='chk'>{TICK}{e(c)}</span>" for c in v['checks'])
            facts['Bind'] = f"Attached the exact retrieved text of {len(v['inputs'])} rows"
            tie = (f", equal to the {e(v['reconciled']['label'])} the filing reports ({int(v['reconciled']['value']):,})" if v.get('reconciled')
                   else f", equal to NVIDIA’s reported ${int(v['reported_fcf']):,}m" if v.get('reported_fcf') and v.get('definition') == 'issuer_fcf'
                   else f"; {e(v.get('reconciliation', ''))}")
            if v.get('house_rule'):
                tie = (f". Anthropic’s adjusted figure recomputes from its own bridge to {money(v['reconciled']['value'])}. "
                       f"The {money(v['difference'][0]['amount'])} of stock compensation stays a cost")
                facts['Method rule'] = f"{e(v['house_rule']['id'])} applied before anything ran."
            facts['Compute'] = f"{money(v['amount'])}{tie}."
        if failed:
            failure = e(failed[0])
            recs = [_record(r) for r in ans.get('runs', []) if not r.startswith('error:')]
            refused = any(str(x).startswith('ContractViolation') for r in recs if r for x in r.get('errors') or [])
            failed_at = 'Method rule' if failure.startswith('Method rule') else 'Check' if refused else 'retrieve'
        evidence = ''.join((_lines_card(r) if (turn.get('route') or {}).get('figure') == 'made_money' else '') + _fcf_card(r) for r in done)
    elif kind == 'record':
        if ans.get('stated'):
            facts['Read figure'] = f"{money(ans['stated'])}, read from your sentence as typed"
        prop = (_record(ans.get('run') or '') or {}).get('proposal') or {}
        if prop.get('line'):
            who = 'Jev' if prop.get('reader') == 'jev' else 'Keyword rules (Jev unreachable)'
            conf = f", {prop['line_conf']}% sure" if prop.get('line_conf') is not None else ''
            facts['Read claim'] = f"{who}: <b>{e(prop['line_label'])}</b> for {e(prop['period'])}{conf}{_jev_link(prop.get('jev_event'))}"
        if ans.get('error'):
            failure = e(_record_error(ans))
            failed_at = 'check' if prop.get('line') else 'model' if 'could not tell' in failure else 'code'
        else:
            facts['Claim check'] = 'Equal to that line of a completed source-checked run.'
            facts['approve'] = f"Waiting. Approve only from a terminal: <code>finrail approve {e(_approve_ref(ans))}</code>"
    elif kind == 'screen':
        c = _screen_counts(ans)
        if c:
            facts['check'] = f"{c['verified']} verified · {c['reported']} reported · {c['rejected']} rejected · {c['discarded']} quotes discarded"
            evidence = ("<ul class='finding-list'>" + ''.join(f"<li>{badge(f.get('status'))} <span>{e((f.get('candidate') or {}).get('claim', ''))}</span></li>"
                                                            for f in c['findings']) + "</ul><p class='synthetic-note'>Northwind is a labelled synthetic company. <a href='/screen'>Full screen</a></p>")
        else:
            failure = e(ans.get('error') or 'No findings.')
    elif kind == 'web':
        v = _web(ans)
        if ans.get('error') or not v:
            failure = e(_plain_error(ans.get('error') or 'Unavailable.'))
            failed_at = 'retrieve' if 'tavily' in failure.lower() else 'check'
        else:
            n = len(v['claims'])
            facts['Find'] = f"{v['pages']} page{'s' if v['pages'] != 1 else ''} for “{e(v['query'])}”"
            facts['Pick sentences'] = f"Kept {n} sentence{'s' if n != 1 else ''}, word for word, that state a figure on a checked line"
            facts['Read claims'] = (f"Read {n} sentence{'s' if n != 1 else ''} in one call{_jev_link(v.get('jev_event'))}" if v['reader'] == 'jev'
                                    else f"Jev was unreachable, so keyword rules read {n} sentence{'s' if n != 1 else ''}. This says so")
            c = v['counts']
            facts['Check'] = (f"{c['matches']} match · {c.get('loose', 0)} loosely rounded · {c['wrong_line']} wrong line · "
                              f"{c['wrong']} wrong · {c['definition']} other definition · "
                              f"{c['unchecked'] + c['unclear']} set aside")
            order = ('wrong', 'wrong_line', 'loose', 'definition', 'matches', 'unclear', 'unchecked')
            ranked = sorted(v['claims'], key=lambda x: order.index(x['verdict']) if x['verdict'] in order else 9)
            evidence = ''.join(_claim_html(x) for x in ranked) or '<p>No sentence on those pages stated a cash-flow figure.</p>'
    elif kind == 'multiple':
        v = ans.get('result') or {}
        facts['Read input'] = f"${int(Decimal(ans['valuation'])):,}m, read from your sentence as typed" if ans.get('valuation') else ''
        if ans.get('error'):
            failure = e(ans['error'])
            failed_at = 'code' if not ans.get('valuation') else 'check'
        else:
            facts['Checked revenue'] = f"${int(Decimal(v['revenue_usd_m'])):,}m from run <a href='/proof?run={e(v['revenue_run'])}'>{e(v['revenue_run'])}</a>"
            facts['Divide'] = f"{e(v['valuation_usd_m'])} / {e(v['revenue_usd_m'])} = <b>{e(v['price_to_sales'])}×</b> in Decimal · {e(v['label'])}"
    elif kind == 'search':
        found = (_record(ans.get('run') or '') or {}).get('result') or {}
        if ans.get('error'):
            failure, failed_at = e(ans['error']), 'retrieve'
        else:
            facts['Retrieve'] = f"{len(found.get('evidence') or [])} excerpts"
            facts['Allowlist'] = f"Every excerpt came from {e(found.get('report_id', 'the selected report'))}’s files."
            quoted = found.get('answer') or {}
            if quoted:
                facts['Number'] = f"Sentences numbered across {len(found.get('evidence') or [])} excerpts."
            if quoted.get('quotes'):
                facts['Pick'] = (f"Picked {', '.join(e(q['id']) for q in quoted['quotes'])} and wrote the lead-in. "
                                 "No quote text came from the model.")
                facts['Attach'] = (f"Every ID was in this retrieval; code attached {len(quoted['quotes'])} exact "
                                   f"sentence{'s' if len(quoted['quotes']) != 1 else ''}. The lead-in has no digit.")
            elif quoted.get('refused'):
                failure = e(quoted['refused'])
                failed_at = quoted.get('step') or 'Attach'
            evidence = ''.join(f"<blockquote class='excerpt'>{e(x['text'][:520])}{'…' if len(x['text']) > 520 else ''}"
                               f"<cite>{e(x.get('source_name') or x['source_uri'])}</cite></blockquote>" for x in (found.get('evidence') or [])[:3])
    return facts, failure, failed_at, evidence


def _declared_steps(cap: str, verdict_state: str, facts: dict, failure: str, failed_at: str) -> str:
    registry.load()
    steps = registry.command(cap).steps
    # A failure names the step that stopped the run, by name where it can, else by kind.
    stop = (next((i for i, st in enumerate(steps) if st['name'] == failed_at), None)
            if verdict_state == 'fail' else None)
    if verdict_state == 'fail' and stop is None:
        stop = next((i for i, st in enumerate(steps) if st['kind'] == failed_at), 0)
    first_model = next((i for i, st in enumerate(steps) if st['kind'] == 'model'), 0)
    out = ''
    for i, st in enumerate(steps):
        if verdict_state == 'fail':
            state = 'pass' if i < stop else 'fail' if i == stop else 'skip'
        elif verdict_state == 'run':
            state = 'pass' if i < first_model else 'run'
        elif verdict_state == 'wait':
            state = 'wait' if st['kind'] == 'approve' else 'pass'
        else:
            state = 'pass'
        seen = facts.get(st['name']) or facts.get(st['kind'])
        detail = failure if state == 'fail' and failure else seen or ('Not reached.' if state == 'skip' else e(st['rule']))
        out += _step(state, f"{e(st['name'])} · {e(st['by'])}", detail,
                     f"{e(st['rule'])} <span class='where'>enforced in <code>{e(st['where'])}</code></span>")
    return out


def trace(turn: dict) -> str:
    ans, route = turn.get('answer') or {}, turn.get('route') or {}
    kind = ans.get('kind')
    state, verdict_label = verdict(turn)
    steps, evidence = [_route_step(route)], ''
    cap = CAPABILITY.get(kind)
    if cap:
        registry.load()
        n = len(registry.command(cap).steps)
        steps.append(_step('pass', f"Plugin · <code>{e(cap)}</code>",
                           _declared(cap) + f"<br>The {n} step{'s' if n != 1 else ''} below are its own declaration, filled in with what this run did."))
        facts, failure, failed_at, evidence = _facts(turn)
        steps.append(_declared_steps(cap, state, facts, failure, failed_at))
    else:
        steps.append(_step('skip', 'Plugin', 'None ran. Below the bar or out of scope, nothing is attempted.'))
    steps.append(_step(state, 'Verdict', e(verdict_label)))
    return (f"<div class='trace-head'><p class='trace-q'>“{e(turn['q'])}”</p><span class='verdict v-{state}'>{e(verdict_label)}</span></div>"
            f"<ol class='trace'>{''.join(steps)}</ol>" + (f"<div class='trace-evidence'><h3>Evidence</h3>{evidence}</div>" if evidence else ''))


def page(cid: str, turns: list, engine: str, selected: int | None = None, prefill: str = '') -> str:
    if turns:
        selected = selected if selected is not None and 0 <= selected < len(turns) else len(turns) - 1
    thread, running = '', False
    for i, t in enumerate(turns):
        state, label = verdict(t)
        running = running or state == 'run'
        chosen = i == selected
        thread += (f"<div class='turn{' is-selected' if chosen else ''}' id='t{i}'><p class='bubble'>{e(t['q'])}</p>"
                   f"<div class='reply'><div class='answer-text'>{_short(t, cid)}</div>"
                   f"<a class='trace-link' href='/?chat={e(cid)}&amp;t={i}#t{i}'{' aria-current=true' if chosen else ''}>"
                   f"<span class='verdict v-{state}'>{e(label)}</span><span>{'Showing trace' if chosen else 'Show trace'}</span></a></div></div>")
    chips = ''.join(f"<button class='chip' name='question' value='{e(s)}'>{e(s)}</button>" for s in followups(turns))
    starter = ("<form method='post' action='/ask' class='suggestions'>"
               f"<input type='hidden' name='chat' value='{e(cid)}'><p>{'Ask next' if turns else 'Start here'}</p>{chips}</form>") if chips else ''
    refresh = f" data-run-refresh data-refresh-url='/?chat={e(cid)}#latest'" if running else ''
    composer = ("<form method='post' action='/ask' class='composer'>"
                f"<input type='hidden' name='chat' value='{e(cid)}'><label class='sr-only' for='question'>Ask a finance question</label>"
                "<textarea id='question' name='question' rows='2' required minlength='3' maxlength='1500' "
                f"placeholder='Ask about the NVIDIA, Nebius, ASML or Anthropic filings'>{e(prefill)}</textarea>"
                f"<button class='btn' type='submit'>Ask{icon('arrow', 16)}</button></form>")
    panel = (trace(turns[selected]) if turns else
             "<div class='trace-empty'><p class='trace-q'>How each answer is made</p><p>Every answer shows its steps here: who routed "
             "the question, which rules applied, what the model was allowed to touch, each check code ran, and the verdict.</p>"
             "<ol class='trace'>" + _step('skip', 'Router', 'Jev picks a workflow. It cannot write.') + _step('skip', 'Plugin', 'Runs under rules declared in code.')
             + _step('skip', 'Model', 'Picks rows and values in the filing. Nothing else.') + _step('skip', 'Checks', 'Code checks every figure against the filing.')
             + _step('skip', 'Verdict', 'An answer, a refusal, or a wait for a person.') + "</ol></div>")
    how = (("Route", "Jev sends each question to the right workflow, filing and period."),
           ("Read", f"{e(engine)} is the core model. It reads the filing and picks the figures."),
           ("Rules", "Plugins hold your fund’s method as rules in code."),
           ("Check", "Code checks every figure against the filing, or refuses. The trace shows each step."))
    return ("<header class='ask-hero'><p class='kicker'>A research harness for funds, inspired by DeepSeek Harness</p>"
            "<h1>Evaluate investment opportunities</h1>"
            "<p class='proof-lede'><b>In the corpus, ingested for RAG:</b> Anthropic’s S\u20111, and quarterly results from NVIDIA, Nebius and ASML.</p>"
            "<dl class='hero-how' aria-label='How it works'>"
            + ''.join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in how) + "</dl></header>"
            f"<div class='ask-layout'><div class='ask-chat'><section class='thread'{refresh}>{thread}<span id='latest'></span></section>"
            f"{starter}{composer}" + ("<p class='new-chat'><a href='/'>Start a new conversation</a></p>" if turns else '')
            + f"</div><aside class='trace-panel' id='trace' aria-label='Harness trace'>{panel}</aside></div>")
