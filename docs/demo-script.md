# Demo script: 2 minutes 30 seconds

Screen recording with voiceover. One Anthropic story: the S-1 contradicts itself, the
fund's method resolves it in code, and the method cannot be bent. One stock-compensation
reconciliation, one illustrative house rule.

| time | on screen | voiceover |
|---|---|---|
| 0:00 to 0:15 | Ask: "Did Anthropic make money, according to its S-1?" The reply shows three lines, each with its table and row: net loss −$80,748m, operating loss −$693m, Anthropic's adjusted operating income +$1,618m. | "Same company. Same six months. Same filing. It lost $80 billion, and it reports $1.6 billion of adjusted operating income. All three numbers are true. An AI that picks one for your memo has just made your investment decision." |
| 0:15 to 1:15 | Click the chip "What is Anthropic's operating result under our house rules?" The trace shows Google retrieving only from the S-1, Nemotron picking two row IDs, code checking the file hash and the 2026 six-month column. Answer: −$567m. Anthropic's +$1,618m recomputes from its own bridge; the $2,185m gap is stock compensation. Open the workpaper. | "Which number goes in our memo is our method's call, and our method is code. One house rule: stock compensation is a cost. Same cells, different rule, and the difference explained to the dollar." |
| 1:15 to 1:45 | Click the chip "Add back stock compensation the way Anthropic does." Refused by house rule HR-1 before anything runs. Then on the workpaper, "Swap in a figure from another column": refused by the checker. | "The model can fetch. It cannot change our rules, and it cannot slip in last year's column." |
| 1:45 to 2:15 | Rules page: every capability's declared steps, and the house rules panel. The same command in a terminal. Approvals run only from the operator's terminal. The one model call in the ledger. | "Every rule is declared once, in code. The browser and the terminal obey the same declaration." |
| 2:15 to 2:30 | The workpaper. | "The fund's method is code. The model fetches, code verifies and calculates, a person decides." |

## Before recording

1. On the demo instance, run the house check once. The opener and the house figure
   both answer instantly from it.

   ```sh
   finrail do finance.reports.analyze report_id=ANTH-S1-2026 definition=house_operating_income
   ```

   From a browser the same run starts with a POST to `/reports/analyze` with
   `report_id=ANTH-S1-2026` and `definition=house_operating_income`.
2. Start a fresh conversation for the take. Rehearse once with the live router; if Jev
   is under its bar, click the offered choice and the trace shows that the person picked.
3. Keep the workpaper for the house run open in a second tab for the tamper click.
4. A restart or redeploy clears the instance's runs. Re-run step 1 after either.

## Say on the submission page

HR-1 is illustrative: the fund's real rules replace it. Anthropic is checked for one
half-year. The 36-case benchmark has not been run.
