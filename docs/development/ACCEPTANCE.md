# DGA-001 acceptance evidence

Status: BLOCKED on real BUILD/product/platform acceptance. Code is ready for PR review; local automated tests are not real platform acceptance.

## Exact baseline and reproducible local checks

Working branch: codex/dga-001-closure, from PR #2 head 869a800c9aee5c06793b1b91d097346ac00dcaf5. PR #1 is its ancestor.

Windows Python 3.12 isolated virtual environment; CI additionally uses Python 3.11 on Ubuntu.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app scripts
node --check web/app.js
```

- Original baseline: 206 passed.
- Final local full regression: **261 passed**, one upstream Starlette/AnyIO deprecation warning; no skipped tests.
- Compileall, Node frontend syntax and native PowerShell parse: PASS.
- Focused integrity/product/publish/order/script regression: 64 passed before the last numeric TOP-contract adjustment; the full 261-test run includes that adjustment.
- New checks include positive ZIP/XLSX/CSV open/read, workbook formulas, unsafe/duplicate/empty deliverables, tampered/deleted bytes, market/policy/time snapshot changes, paid failed JSON accounting, concurrency/idempotence, exact TOP parameters, unpaid/mismatched orders, unknown remote outcomes, protected reads and a populated V1.1.3.3 schema migrated twice.
- Tests use isolated fixtures and fake external clients where necessary. In particular `test_real_end_to_end_publish.py` constructs actual local files but MOCKS the LLM and platform clients. Its name does not establish real upstream or buyer delivery success.
- Core API method/path pairs are checked for duplicate registration. No pytest skip, constant-true assertion or swallowed test exception was found in the test scan.

## Native Windows script

```powershell
.\scripts\real_rebuild_acceptance.ps1 -BaseUrl http://127.0.0.1:18080 -PreflightOnly
# API_AUTH_TOKEN can be supplied through the environment; never put it in a report.
.\scripts\real_rebuild_acceptance.ps1 -BaseUrl http://127.0.0.1:18081 -Limit 1 -RefreshQueriesPerProbe 2
```

- Parser check: PASS using the installed PowerShell parser.
- Native preflight ran against an isolated real-data-only server with auto collection disabled, version 1.4.1-dga001.
- The read-only preflight reports NOT_ACCEPTED by design. No collection, model or publication is implied.
- OpenCLI daemon/extension connected: 1.8.8 / 1.0.24. Per-platform login/write permission has not been proved by the doctor alone.
- Initial real refresh: 4 probes / 3 successful; PARTIAL; 8 recent retained records; 0 direct demand; 1 context candidate; 0 Gate A passes; 0 model calls.
- Original effective database backup: sources 40, evidence 668, opportunities 97, products 0, deliveries 0, ai_runs 237. Twice-upgraded copy preserved every count, integrity_check=ok, foreign_key_check empty.
- Broader refresh and bounded-Qiniu outcomes are recorded below once complete. Raw URLs, original evidence, runtime DBs, keys, profile identifiers and reports stay in ignored local runtime storage; sanitized aggregate evidence belongs in Git.

## Capability audit

| Capability | Audit result | Practical limit |
| --- | --- | --- |
| Xiaohongshu / Reddit demand collection | Actual recent records returned | Some queries fail; supply/discussion does not count as independent demand |
| GitHub collection | Existing code and regression coverage | Current on-site collection not yet demonstrated in this run |
| OpenCLI / Agent-Reach | Actual doctor CONNECTED | Does not prove marketplace publishing authorization |
| Intent / dedup / clustering / trend | Real refresh plus deterministic regressions | Initial run yielded no direct independent demand |
| Commercial validation / Gate A | Actual failure/empty candidate state retained | No fabricated pass to allow a product |
| Six Agents / schema / card / evidence | Persistent implementation and regressions | Bounded live result recorded separately; code presence is not acceptance |
| Qiniu / SenseNova / budget | Qiniu configured; failed usage/claims hardened | SenseNova not configured; held unknown usage requires reconciliation |
| Etsy / Creative Market / Xianyu intelligence | Etsy fetch succeeds; CM returns HTTP 403 | Preserve failed/inconclusive status and metric scopes; no bypass |
| Product ZIP/XLSX/CSV/scripts | Actual fixture files opened and checksummed | A real current BUILD product is still required for business acceptance |
| SourceAsset / license | Explicit permissions, active status and snapshot gates | Generated metadata is not independent proof that all content is original |
| Gate B / human approval | File/policy/license/market changes revoke approval | No actual product was manually approved or published in this audit |
| Etsy publishing | Corrected documented contract and failure tests | Missing Etsy credentials and explicit live listing execution |
| Xianyu publishing | Browser bridge contract plus idempotence guard | No live publication authorized/executed |
| Xianyu TOP orders / virtual shipment | Corrected request, order/item/seller validation and persistent claim | Missing partner credentials/permissions/paid order; VIRTUAL_SHIPPED is NOT file transfer |
| Workbench / status | Existing layout retained; authenticated downloads fixed | Dedicated buyer receipt/reconciliation UX remains future work |

## Contract references checked on 2026-10-21

- Etsy digital listing / upload file name / PATCH activation: https://developer.etsy.com/documentation/tutorials/listings/ and https://developer.etsy.com/documentation/reference
- Official TOP `param_appraise_isv_order_query`, `item.item_id`, stable `encryption_seller_id` and status 2 (paid): https://developer.alibaba.com/docs/api.htm?apiId=50672&source=search

## Remaining acceptance boundary

Etsy and Xianyu TOP credentials/permissions are absent. No platform writes, real paid order, buyer transmission or transfer receipt are claimed. Generic resource registration remains PENDING; official no-logistics shipment remains VIRTUAL_SHIPPED with file_transferred=false. Product publish/ship UNKNOWN states and unresolved AI reservations require review rather than blind replay. Keep DGA-001 open until these conditions and a useful real BUILD package are actually verified.

## Completed live run — 2026-10-21

The broader native script ran task 22 against a read-only-backup-derived isolated database, using actual OpenCLI collection and live marketplace responses. It exited **2**, correctly refusing full acceptance:

- Demand: all 4 probes returned successfully across 8 queries; 18 retained recent records; 2 direct signals.
- Clustering over existing plus newly collected real evidence: 28 demand records and 21 context records considered; 38 candidate groups; 0 cross-platform groups.
- Recent Gate A: 2 candidates evaluated; both REVIEW_REQUIRED (one independent signal each); 0 PASS. The automatic paid BUILD path ran no Agents and created no product.
- Market: 6 sources attempted. Etsy returned 12 verified digital items and inserted 12. Two Creative Market requests returned HTTP 403. All 3 Xianyu queries returned AUTH_REQUIRED (logged-in browser session required). The task remains PARTIAL.
- This is evidence that bridge connectivity does not imply Xianyu login or usable publishing rights. No access limitation was bypassed.

A separate single-candidate discovery analysis was explicitly run on actual opportunity 69 after reloading the updated branch, preserving failed-Gate semantics:

- Signal / Pain / Market / Product / Risk / Judge: all 6 Agent stages SUCCESS using configured Qiniu qwen/qwen3.5-plus; card 33; final decision **DROP**; Gate A **FAILED**; confidence 82 (confidence in this DROP assessment, not a build or sales probability).
- Actual provider-reported aggregate usage: prompt 46,360 + completion 3,871 = **50,231 tokens**. Six logical AI-run records include all recorded retry usage; these are not a claim of six HTTP requests.
- Cost estimate from provider usage and the configured/public price calculation: **CNY 0.055668**. This is an estimate, not a verified account invoice. Process budget ceiling: CNY 0.25. Outstanding reservations after completion: 0.
- Agent duration sum: 76,465 ms (includes request pacing/retry time).
- Concurrent duplicate POST: HTTP 400, workflow already RUNNING. Subsequent identical POST reused card 33; AI-run count and Agent-run count both stayed at 6. No duplicate paid workflow was created.
- Actual products=0, publish_runs=0, deliveries=0. No simulated product, approval, order, shipment or buyer transmission was inserted to turn this into a pass.

Sanitized summaries are in this document. Full local reports and the business-data backup remain under ignored `data/dga001-acceptance/`; the native report is `digital-goods-real-rebuild-20261021-181417.json` in the user's temporary directory.

## GitHub review and CI

- Draft PR: https://github.com/HelloMrDuan/digital-goods-agent-engine/pull/3
- Base: feat/v1.2-opportunity-agent-engine (#2); #1 and #2 remain open. No merge/force push.
- Initial implementation commit: 4a955322f80fbe9545699fbed551dbce39311686.
- Initial PR CI run: https://github.com/HelloMrDuan/digital-goods-agent-engine/actions/runs/39584268222 — SUCCESS. Job 119169154886 logs verified 258 passed, compile/frontend/tracked-only archive/upload all successful.
- Final review additionally guards changed prices/approval snapshots, missing historical proof and changed Etsy shop identities from reusing an old successful listing. Three new regression cases raise the final local suite to 261 passed. Updated-head CI is checked separately before handoff.
