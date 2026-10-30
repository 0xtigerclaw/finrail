# Demo script: 2 minutes 50 seconds

Screen recording with voiceover. One Anthropic story: the S-1 contradicts itself, the
fund's method resolves it in code, the press gets it wrong, and the method cannot be
bent. One stock-compensation reconciliation, one illustrative method rule. Every click is
a chip the previous answer offers.

| time | on screen | voiceover |
|---|---|---|
| 0:00 to 0:15 | Ask: "Did Anthropic make money, according to its S-1?" The reply shows three lines, each with its table and row: net loss −$80,748m, operating loss −$693m, Anthropic's adjusted operating income +$1,618m. | "Same company. Same six months. Same filing. It lost $80 billion, and it reports $1.6 billion of adjusted operating income. All three numbers are true. An AI that picks one for your memo has just made your investment decision." |
| 0:15 to 1:05 | Click the chip "Under our method, what was Anthropic's operating result in its S-1?" The trace shows Google retrieving only from the S-1, Nemotron picking two row IDs, code checking the file hash and the 2026 six-month column. Answer: −$567m. Anthropic's +$1,618m recomputes from its own bridge; the $2,185m gap is stock compensation. Open the workpaper. | "Which number goes in our memo is our method's call, and our method is code. One method rule: stock compensation is a cost. Same cells, different rule, and the difference explained to the dollar." |
| 1:05 to 1:35 | Click the chip "What does the web say about Anthropic's profit?" The trace shows Jev routing the question to web search, two Tavily searches, Jev naming the line and period of each sentence, and code checking each figure against the S-1 cells. Point at three verdicts: Fortune's "$1.6 billion adjusted operating income" matches; Fortune's "$80 billion loss" is loosely rounded against −$80,748m; a third-quarter revenue figure is set aside because the S-1 covers the first half. | "Now the press. Each sentence is read for which line and which period it claims, then code checks the figure against the filing. The $1.6 billion matches, as the adjusted line. The $80 billion loss is rounded loosely: the filing says $80.7 billion. And a third-quarter number is set aside, because the S-1 does not cover that period." |
| 1:35 to 2:05 | Click the chip "Add back stock compensation the way Anthropic does." Refused by method rule M-1 before anything runs. Then on the workpaper, "Swap in a figure from another column": refused by the checker. | "The model can fetch. It cannot change our rules, and it cannot slip in last year's column." |
| 2:05 to 2:35 | Rules page: every capability's declared steps, and the method rules panel. The same command in a terminal. Approvals run only from the operator's terminal. The one model call in the ledger. | "Every rule is declared once, in code. The browser and the terminal obey the same declaration." |
| 2:35 to 2:50 | The workpaper. | "The fund's method is code. The model fetches, code verifies and calculates, a person decides." |

## Before recording

1. On the demo instance, run the method check once. The opener and the method figure
   both answer instantly from it.

   ```sh
   finrail do finance.reports.analyze report_id=ANTH-S1-2026 definition=house_operating_income
   ```

   From a browser the same run starts with a POST to `/reports/analyze` with
   `report_id=ANTH-S1-2026` and `definition=house_operating_income`.
2. Start a fresh conversation for the take. Rehearse once with the live router; if Jev
   is under its bar, click the offered choice and the trace shows that the person picked.
3. Keep the workpaper for the method run open in a second tab for the tamper click.
4. Rehearse the web question within the hour before recording. Tavily returns live
   articles, so the sentences change between runs. On 30 October it returned eight
   claims: Fortune's two matched as above, two named another period, and four named no
   period. If a "wrong line" verdict comes up, point at it instead: it is the strongest beat.
5. Each web question spends Tavily credits and counts against the 25-action budget. Rehearse
   it once or twice, not more.
6. A restart or redeploy clears the instance's runs. Re-run step 1 after either.

## Say on the submission page

M-1 is illustrative: the fund's real rules replace it. Anthropic is checked for one
half-year. The 36-case benchmark has not been run.
