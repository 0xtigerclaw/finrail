# SEC XBRL companion facts

`finrail/extensions/finance/xbrl.py` reads the machine-readable copy of the numbers in an
SEC filing. Every figure in a filing's statements is tagged in XBRL with a concept
(`us-gaap:Revenues`), a context (the period, plus any segment) and a unit. The HTML
checker in `public_reports.py` reads the printed cell; this module reads the tag. Put
together, a row-ordinal or row-label mistake in a profile can no longer pass as a
verified figure, because the cell has to agree with the tag.

The module is standard library only, like the rest of the runtime. Everything it
downloads is untrusted data: parsed, size-capped, never executed, never read as an
instruction.

## Three sources, one `Fact`

| Source | Function | Carries | Notes |
|---|---|---|---|
| `companyfacts` JSON API | `load_companyfacts(cik)` then `facts_from_companyfacts(data, accession=...)` | every non-segment, standard-taxonomy fact the issuer has ever filed | simplest; one request per issuer; no segment facts, no `decimals`, no issuer-custom (`nvda:`) concepts |
| inline XBRL in the primary `.htm` | `facts_from_inline(raw_bytes, accession)` | everything, including segments and `decimals` | works on the bytes Finrail already archives and hash-checks, so no second download |
| extracted instance `*_htm.xml` | `facts_from_instance(raw_bytes, accession)` | everything | the plain XML EDGAR puts next to the primary document |

`filing_urls(cik, accession, primary_document)` gives the EDGAR addresses for a filing;
`load_filing_facts(cik, accession, primary_document)` fetches the primary document and
parses its inline XBRL.

All three produce a flat list of `Fact`:

| Field | Meaning |
|---|---|
| `concept` | `namespace:LocalName`, for example `us-gaap:NetIncomeLoss` |
| `value` | `Decimal` in base units with `scale` and `sign` applied: 4,500 million is `4500000000` |
| `unit` | `USD`, `shares`, `USD/shares`, `pure` |
| `start`, `end` | the period; `start` is `None` for an instant. `months` and `instant` are derived |
| `context` | the context id in the instance (synthetic for companyfacts) |
| `dimensions` | `((axis, member), ...)`; empty means consolidated |
| `source`, `accession` | `companyfacts`, `inline` or `instance`, and the filing |
| `decimals` | the XBRL `decimals` attribute when the source has it (`-6` means rounded to millions) |

## Network

All HTTP goes through `http_get`, and every loader takes `fetch=` so tests pass a stub.
Set `FINRAIL_SEC_USER_AGENT` to your organisation and a contact email; the SEC's
fair-access policy asks for it and EDGAR may refuse the default. Only HTTPS is allowed
and responses are capped at 60 MB.

## Lookup: exactly one fact or a refusal

```python
fact = xbrl.lookup(facts, ["us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"],
                   end="2026-06-30", months=6, unit="USD", accession="0001628280-26-067168")
```

`months` is 3, 6, 9 or 12 for a duration, or `None` for an instant. The candidates are a
preference order: the first concept that has any consolidated fact for the period
decides, and later candidates are not consulted. Segment facts are excluded. Two facts
with the same value count as one (the same number tagged on the statement and in a
note). Two with different values are refused as ambiguous; that is a restatement or a
tagging error, and the refusal names both values and their contexts. A missing fact is
refused with the nearest thing found (other periods, or the segment facts that were
excluded). Every refusal is a `ContractViolation` from `finrail.core.errors`.

`find(facts, "net_loss", end=..., months=...)` is the same lookup driven by the
concept map below.

## Reconcile: the HTML cell against the tag

```python
verdict = xbrl.reconcile(fact, Decimal("-3000"), "millions", Decimal("0.5"), presented="as_tagged")
```

`value` is the table figure as printed, signed: parentheses mean negative. `unit_scale`
is the table's unit line (`millions`, `thousands`, `units`), and `tolerance` is in the
same units, so `0.5` on a table in millions allows half a million of rounding. The
verdict is a dict with `matched`, `difference` (XBRL minus table, in base units and in
table units), both values, the concept, context, period, source and accession.

Sign is explicit because XBRL and the table disagree for some lines:

- `as_tagged`: the table sign is the XBRL sign. A net loss is a negative
  `NetIncomeLoss` and sits in parentheses. Revenue, operating income and operating cash
  flow work this way.
- `negated`: the concept measures a payment, so it is positive in XBRL while the cash
  flow statement prints it in parentheses. Capital expenditure
  (`PaymentsToAcquirePropertyPlantAndEquipment`) works this way.

The concept map records which applies to each definition.

## Concept map: the place a fund extends

`xbrl.CONCEPTS` maps Finrail's definition names to ordered candidate concepts, the
period kind, the unit and the sign convention:

| Definition | Candidates, in order | Presented |
|---|---|---|
| `revenue` | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `RevenueFromContractWithCustomerIncludingAssessedTax`, `SalesRevenueNet` | as tagged |
| `net_loss` | `NetIncomeLoss`, `ProfitLoss` | as tagged |
| `operating_income` | `OperatingIncomeLoss` | as tagged |
| `operating_cash_flow` | `NetCashProvidedByUsedInOperatingActivities`, `...ContinuingOperations` | as tagged |
| `cash_purchases` (alias `capex`) | `PaymentsToAcquirePropertyPlantAndEquipment`, `PaymentsToAcquireProductiveAssets`, `PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets` | negated |
| `principal_payments` | `FinanceLeasePrincipalPayments` (absent from NVIDIA's companyfacts; NVIDIA's line is probably a custom `nvda:` concept, so confirm it in the 10-Q's inline XBRL) | negated |
| `stock_comp` | `ShareBasedCompensation`, `AllocatedShareBasedCompensationExpense` | as tagged |

All concepts are `us-gaap:` unless written with another prefix. To add an issuer's
custom concept, add it with its prefix (`nvda:...`) to the candidate list, or add a new
definition with its own `presented` value. The order is a declared preference, so put
the concept the issuer actually uses first.

## Checked against a real filing

On 30 October 2026 the parser was run on a verbatim slice of NVIDIA's live companyfacts
document (CIK 1045810). It found the Q1 FY2027 quarter (13 weeks ended 26 April 2026) as
a 3-month period, returned operating cash flow of 50,344 million and purchases of
productive assets of 1,757 million, which are the two figures the NVIDIA profile reads
from the press release, and collapsed the comparative quarter that two 10-Qs both carry
into one fact. NVIDIA tags capital expenditure as `PaymentsToAcquireProductiveAssets`,
the second candidate, so the fall-through in the candidate order is exercised for real.
The inline XBRL and plain-instance parsers were checked on hand-built fixtures only; the
SEC refuses plain `urllib` requests whose User-Agent has no contact email, so set
`FINRAIL_SEC_USER_AGENT` before the first live fetch.

## Tests

`tests/test_xbrl.py` builds a tiny inline XBRL filing, a tiny companyfacts document and a
tiny plain instance by hand: a segment fact that must be excluded, a `scale="6"` fact, a
negative via `sign="-"`, a nil fact, a zero dash, a fact tagged twice with the same value
and one tagged twice with different values. No test touches the network.

## Wiring this into `check_and_compute`

For the profile session, after the archived cell has passed its own checks:

1. Parse once per report: `facts = xbrl.facts_from_inline(raw, item["edgar_accession"])` on the hash-checked bytes already in hand, or `xbrl.facts_from_companyfacts(xbrl.load_companyfacts(item["edgar_cik"]), accession=item["edgar_accession"])` for issuers whose archived document is not the EDGAR filing.
2. For each term: `fact = xbrl.find(facts, key, end=item["period_end"], months=<profile period in months>, accession=item["edgar_accession"])`; the refusal is already a `ContractViolation`, so let it propagate like the cell checks do.
3. Sign the cell as the table prints it: `printed = -value if prof["rows"][key]["sign"] == "-" else value`.
4. `verdict = xbrl.reconcile(fact, printed, "millions", tolerance, presented=xbrl.CONCEPTS[key]["presented"])`; refuse the run when `verdict["matched"]` is false, and attach the verdict to the checked input.
5. Add "XBRL companion fact" to `checks` only when every term reconciled; a report with no `edgar_accession` skips the step and must not list the check.
