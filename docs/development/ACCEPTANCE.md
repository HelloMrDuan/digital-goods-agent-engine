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

- Demand: all 4 probes returned successfully across 8 queries; 18 retained occurrences / 17 unique recent records; 2 direct signals under the earlier classifier.
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
- Final review additionally guards changed prices/approval snapshots, missing historical proof and changed Etsy shop identities from reusing an old successful listing. Three new regression cases raise the final local suite to 261 passed. Code-head CI is complete for a6f90bbf03947bf6eb2ccfd4df070a30f9110920 (261 passed in both push and PR runs).

- Validated code commit: a6f90bbf03947bf6eb2ccfd4df070a30f9110920.
- PR run https://github.com/HelloMrDuan/digital-goods-agent-engine/actions/runs/39584598333 — SUCCESS; job 119170268169 logs: 261 passed, compile/frontend/source archive/upload successful.
- Push run https://github.com/HelloMrDuan/digital-goods-agent-engine/actions/runs/39584591237 — SUCCESS; job 119170242840 independently confirms the same 261 tests and checks.

## Collection trust follow-up — 2026-10-21

The user authorized the follow-up after reviewing the collection's credibility. This remains part of DGA-001; it does not close the positive BUILD/platform acceptance or start DGA-004.

### Implemented behavior

- Demand discovery enables bounded thread enrichment (default 2 threads / 10 comments per query, configurable). Reddit POST body and Xiaohongshu note fields can update existing evidence. Later search-only observations preserve the cached body and detail provenance.
- Windows npm shims launch their Node entry point directly. Signed URLs containing `&`, percent escapes and query text remain literal arguments; regression executes a real Node process to verify the round trip.
- Posts/comments have separate identities. Comments sharing a thread URL cannot overwrite the post or each other. SQLite reserves the write transaction before identity lookup, so concurrent probes retain one row and all observations.
- Classifier v2 checks buyer direction: `[For Hire]` and self-described product builders are supply; descriptive `terms that need a field` is not a buyer request. Chinese `求多人表格模板` and `帮忙做excel表格有偿` become demand leads. Title-only explicit leads are capped at 0.75 confidence and carry a body-verification reason. This confidence is a rule score, not measured classifier accuracy.
- Versioned deterministic backfill reclassifies previously labeled evidence once and invalidates linked analyses/validation. No AI request is needed.
- Gate A v2 requires body text and stable source account identifiers for eligible signals, counts each source account once, and excludes inherited thread dates from recent-comment proof. Profile IDs/usernames identify source accounts, not verified distinct people or willingness to pay. Cross-platform presence cannot bypass the minimum account count.
- API evidence details and the quality report expose body coverage, identity basis and enrichment outcomes. Discovery totals deduplicate evidence IDs across probes/corroboration. Failed enrichment remains PARTIAL, including when search hits are all stale.

### Real read-only evidence

All mutations below occurred in `data/dga001-acceptance/quality-verification.db`, a fresh SQLite backup of the earlier acceptance copy. No production configuration was loaded and no paid model was invoked.

- Reclassification of the 685 existing rows corrected four inspected cases: 674/679 noise -> explicit title-only leads; 683 discussion -> supply; 684 explicit demand -> supply. The legitimate DVD-template request 685 remains explicit demand. These are case regressions, not a population accuracy estimate.
- Two bounded searches (`求 多人 表格 模板`, `DVD spreadsheet template`) retained no current evidence after the 30-day filter. A separate source revisit successfully read real Reddit evidence 685 and five comments; all five comments classify as discussion, not additional buyer demand. Repeating the revisit reused the same five comment IDs without extra evidence rows. Their missing source timestamps remain inherited_thread.
- Xiaohongshu evidence 679 detail/comments both returned `Navigation rejected`. The corrected Windows argv handling removed the shell's `xsec_source` command error but does not resolve this separate browser navigation failure. No restriction bypass or synthetic body was used.
- Final code's bounded `求 Excel 模板` probe: raw_fetched=3, retained_recent=1 (existing evidence), inserted=0, duplicates=1, enrichment_failures=2, status=PARTIAL. This confirms the actual failure is visible; it does not prove additional independent demand.
- Isolated final totals: evidence=690, body_present=339 (49.13% of this mixed historical corpus), stable source accounts=370. Coverage measures completeness only. No broad claim of high truthfulness or reliable paid demand follows from these figures.
- AI runs remain 243; products/publish_runs/deliveries remain 0. SQLite integrity_check=ok; no foreign-key violations. The original effective database is unchanged at sources=40, evidence=668, opportunities=97, products=0, deliveries=0, ai_runs=237.
- Local reports remain ignored: `quality-verification-report.json`, `quality-source-revisit.json`, `final-quality-probe.json`. They include business provenance and are not part of the source archive.

### Verification

- Final Windows full suite: **281 passed**, no skips, one existing upstream deprecation warning. This adds 20 tests to the preceding 261-test release.
- Python compileall (`app scripts`), Node syntax (`web/app.js`), native PowerShell acceptance-script parser and `git diff --check`: PASS.
- Follow-up code commit `a41e20c08e488249d8ff5e66fe427e01bcd1ae4f`: [PR CI 39708323897](https://github.com/HelloMrDuan/digital-goods-agent-engine/actions/runs/39708323897) SUCCESS on 2026-10-22. Raw job 119593473737 logs confirm **281 passed**, compile/frontend/source archive/upload all successful. Final documentation-only HEAD checks are recorded on PR #3 before handoff.
- The same code commit's [push CI 39708318939](https://github.com/HelloMrDuan/digital-goods-agent-engine/actions/runs/39708318939) also succeeded; both check runs completed/success. Raw push job 119593457381 is independently checked before handoff.
- DGA-001 remains BLOCKED on the previously documented real BUILD/package/platform/buyer-transfer requirements and unresolved source availability. The prior 261-test release's CI references above are historical; they do not substitute for this follow-up's 281-test CI.
- Owned temporary acceptance servers were stopped. Original business DB counts remain 40/668/97/0 products/0 deliveries/237 ai_runs. No credentials or runtime data are tracked.
