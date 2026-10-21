# Development WBS

Status vocabulary: TODO / IN_PROGRESS / BLOCKED / REVIEW / DONE.
A task is DONE only after code, commit, PR/CI and its required real acceptance evidence exist.

| ID | Task | Dependencies | Status | Remaining acceptance |
| --- | --- | --- | --- | --- |
| DGA-001 | V1.2.x closeout and real-loop hardening | PR #2 (includes PR #1) | BLOCKED | Code PR/CI verified; real BUILD/package/Gate B and authorized platform/order acceptance pending |
| DGA-002 | Orders and actual digital file transfer | DGA-001 acceptance | TODO | Durable listing/order/buyer/file receipt model; reconciliation and delivery recovery |
| DGA-003 | Product usefulness and format quality | DGA-001 acceptance | TODO | Buyer usability evaluation and actual generated products, especially workbook formulas |
| DGA-004 | Opportunity precision and cost | DGA-001 acceptance | TODO | Evaluate real independent signals, candidate yield and costs over multiple runs |
| DGA-005 | Operator workbench | DGA-001 acceptance | TODO | Expose orders, delivery receipts, UNKNOWN reconciliation and outstanding reservations |

PR #1 and #2 stay open. This work is stacked on PR #2; the existing frontend layout and platform authorizations are preserved.
