# Demo the investment workspace

The public console uses synthetic companies, prepared examples, and ephemeral instance state. `FINRAIL_DEMO_WORKSPACE=1` seeds six labelled activity records exactly once per state directory. No model calls, token counts, approvals, or releases are invented. Live model-backed tasks still use the configured engine and its normal validations.

## Five-minute walkthrough

1. **Overview:** open Meridian Components. Its four source documents form the investment case. Northwind is a separate screening dataset.
2. **Data room:** search `contract`, read the customer agreement, and inspect its dates, concentration, and termination clause. Compare the checked information memorandum with the unverified draft note.
3. **Analysis:** show the 15.98% EBITDA margin, source workbook cells, formula, and raw value. Switch to revenue growth, leverage, covenant headroom, and cash conversion. Arithmetic is computed and checked by code.
4. **IC memos:** read the prepared memo, follow its citations back to documents, and review the open diligence questions. Its 24.5% margin claim is expressly excluded. This is a manually prepared synthetic example, not a historical model result.
5. **Approvals:** inspect the full memo text and SHA-256 hash in the pending release proposal. Browser users can inspect or discard; only the operator's terminal can approve execution.
6. **Screening:** review Northwind's reported leadership change and rejected unsupported margin claim. These are fixture verdicts from the deterministic gate, not live public-company research.
7. **Activity & ledger:** inspect the blocked Meridian claim. The Model calls filter contains only actual provider calls; seeded examples never populate it.

For an optional live demonstration, use the Data room's example buttons. They fill a task for review and require a separate submission. The unsupported 24.5% margin must fail its claim check rather than enter the investment case.

## Operation

Set `FINRAIL_DEMO_ENTITY=04123456` to pre-confirm the Northwind synthetic identity under the existing demo operator mechanism. Set `FINRAIL_DEMO_POST_LIMIT=25` to bound mutating console actions per process. The seed does not consume that allowance.

A restart with fresh ephemeral state recreates the examples. Refreshing the same state does not duplicate them or restore a proposal that was discarded. To run against real or persistent state, leave `FINRAIL_DEMO_WORKSPACE` unset; this opt-in seeder is intended only for the public synthetic demo.
