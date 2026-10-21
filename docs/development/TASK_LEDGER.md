# Task ledger

## DGA-001 — 2026-10-21 — IN_PROGRESS

### Baseline established

- Repository: HelloMrDuan/digital-goods-agent-engine (private).
- main: bf5e7ffa53b9d9c53bc76df4f3fd7982cf688aac.
- PR #1 head: 8410799f2afed645c3a9f4f8b5b6d0e2eebf659c, open, one commit.
- PR #2 head: 869a800c9aee5c06793b1b91d097346ac00dcaf5, draft/open, 129 commits / 65 files versus main.
- `git merge-base --is-ancestor` confirms PR #1 is contained in PR #2. Symmetric difference is 0 / 128 commits. No independent PR #1 merge is needed to develop this change.
- Original head Actions run 35839329912, job 107110355734: compile/tests/frontend/package/upload all success. Original local baseline: 206 passed.
- Development branch: codex/dga-001-closure, based on PR #2. No main changes, PR closures, force pushes, or production platform writes.

### Root causes and actual fixes

1. Etsy used PUT for updateListing and omitted upload file name. Use the documented PATCH contract, send name, require upload identifiers and matching active listing state before PUBLISHED. Allow draft-to-active progression. Persist ambiguous writes as UNKNOWN and block duplicate writes under a SQLite transaction.
2. Xianyu query used a top-level order ID instead of `param_appraise_isv_order_query`. A missing returned ID was silently filled with the requested ID. Correct the request and require explicit matching order ID, item ID, published platform and official `encryption_seller_id`. Mutable seller nicknames are not ownership proof.
3. Concurrent virtual shipment checked idempotence before the network but claimed nothing until afterward. Atomically persist RUNNING before the write; timeout/uncertain results remain UNKNOWN across restart; other products cannot take over an order. VIRTUAL_SHIPPED continues to mean file_transferred=false.
4. Merely registering any delivery resource previously marked DELIVERED. Resource registration now remains PENDING. Marketplace fulfillment cannot bypass verified platform-order workflows through the generic delivery endpoint.
5. Failed JSON/schema output could discard paid provider usage. Preserve failed usage/costs, include them in daily and total spend, retain unsettled reservations and stop expiring unknown requests after 30 minutes or at midnight. Agent schema failures retain available provider metadata.
6. Paid workflow deduplication covered some asynchronous buttons only. Add persistent per-entity claims and snapshot-result reuse for Agents, drafts and artifacts. Failed work requires an explicit force retry after inspection; interrupted RUNNING work is not silently replayed.
7. Gate B trusted path existence rather than bytes and did not fingerprint the build. Verify SHA-256, ZIP CRC and manifest membership; snapshot build identity, bytes, currency and policy. Apply package requirements to legacy products too. Rebuilding produces a separate immutable path and invalidates approval. Changed published packages require explicit listing reconciliation.
8. Agent market snapshot freshness was checked only in card listing. Check it at product/build/Gate B boundaries. Gate A also fingerprints policy, author/quality/time metadata, activity, and rolling 30-day eligibility, so aging evidence cannot retain a permanent pass.
9. Artifact paths were not consistently Windows-safe; duplicate files, README-only primary content and header-only workbooks could pass. Reject unsafe/reserved paths, duplicates, reserved manifests and empty workbooks; validate CSV/JSON/Python structure and preserve workbook row widths.
10. Sensitive read endpoints exposed buyer/order data or downloadable packages without the write-token guard. Protect those reads, send the token through authenticated downloads and redact configured credentials from error persistence and API failures.
11. PowerShell expected an obsolete version, omitted API auth, swallowed inspection errors and returned success for partial/no-Agent runs. Synchronize service version, add token/HTTP timeouts/disposal/read-only preflight and meaningful non-success exits.
12. Rebuild could return SUCCESS while demand refresh was PARTIAL. Propagate partial upstream failures. Source packaging now uses tracked files via `git archive`; CI also runs for codex branches and stacked feature PRs.

### Data protection and on-site evidence

- User supplied existing deployment directory D:\programs\digital-goods-agent-engine.
- Its `.env` resolves the Windows DB_PATH `/app/data/digital_goods.db` to D:\app\data\digital_goods.db, rather than the empty older database under the project directory.
- Backed up the effective database through SQLite backup from a read-only source. Migrated only the backup twice. Preserved sources=40, evidence=668, opportunities=97, products=0, deliveries=0, ai_runs=237; integrity_check=ok and no foreign-key violations.
- Original .env, keys, browser session and database were not copied into tracked files or changed. Real acceptance operates on isolated DB copies; fixtures only enter pytest temporary DBs.
- OpenCLI 1.8.8 / extension 1.0.24 doctor reports CONNECTED. This confirms bridge connectivity, not every platform login or write permission.
- Initial live read-only refresh: 4 probes, 3 successful, PARTIAL; 8 retained recent records; no direct demand; 1 context-only cluster, 0 Gate A passes; no paid AI or platform writes.
- A broader run on the effective DB backup is recorded in ACCEPTANCE.md after completion. Qiniu is configured; the acceptance process budget is capped at CNY 0.25. Etsy and official Xianyu TOP remain unconfigured.

### Scope remaining

Do not close DGA-001 or start DGA-002 merely because tests pass. Actual buyer file transfer is not implemented by the TOP no-logistics API. Positive real platform/order acceptance requires valid permissions, an approved actual product and a paid real order; these cannot be fabricated in the business database.

### Live closeout outcome

- Native task 22: PARTIAL / script exit 2; 18 retained occurrences / 17 unique recent records, 2 direct demand signals under the earlier classifier; 38 candidate groups; recent Gate A 0/2 passes.
- Etsy market: 12 real digital items inserted. Creative Market HTTP 403 and Xianyu AUTH_REQUIRED stay visible.
- Explicit discovery evaluation of real opportunity 69: six Qiniu Agents completed, card 33 = DROP, Gate A FAILED. Usage 50,231 tokens; estimated CNY 0.055668; unsettled reserves 0. Concurrent and sequential duplicate requests incurred no additional workflow executions.
- No product or listing was approved/published; no order or buyer delivery was fabricated. DGA-001 still needs a supported real BUILD package plus authorized platform/order acceptance. DGA-002 has not started.

### Review handoff

Status: BLOCKED on positive real BUILD/package/platform acceptance. Final local full suite 261 passed (one upstream deprecation warning), compile/frontend/PowerShell parser passed. PR/CI references are appended after the real remote runs complete.

- Published initial code commit 4a955322f80fbe9545699fbed551dbce39311686 and created Draft PR #3 against #2: https://github.com/HelloMrDuan/digital-goods-agent-engine/pull/3
- Initial PR CI 39584268222 / job 119169154886 SUCCESS; inspected raw logs (258 passed and compile/frontend/archive/upload success).
- Final review adds three cases preventing old listing success from being reused after price/approval/shop changes or without historical proof; final local suite 261 passed. No further live LLM request is needed for these deterministic publication guards.

### Remote verification completed

- Code commit a6f90bbf03947bf6eb2ccfd4df070a30f9110920: PR CI 39584598333 and push CI 39584591237 both SUCCESS; each raw job log confirms 261 passed plus compile/frontend/package/upload success. All code-SHA checks successful; PR #3 remains Draft with no inline review threads.
- Final documentation-only evidence update does not change the validated business code. Temporary owned servers stopped; original runtime data and configuration preserved.
- DGA-001 remains BLOCKED on actual BUILD/product/platform/buyer-transfer acceptance. Next task DGA-002 stays TODO until DGA-001 is accepted, as required by the task dependency.

### Collection credibility follow-up

- User authorized work on body/comment completeness, false demand classifications and independent account corroboration. Continued on `codex/dga-001-closure` / PR #3.
- Fixed Windows signed-URL argument splitting, enabled bounded demand enrichment, persisted refreshed bodies, separated comment identities and serialized concurrent identity upserts. Added intent-policy migration and source-account/body guards to Gate A.
- Four previously inspected classifier errors corrected on an isolated 685-row backup. Real Reddit revisit reads the actual body plus five discussion comments; repeat reads reuse their IDs. XHS note/comments remain Navigation rejected, explicitly recorded.
- Final live probe: PARTIAL, 3 raw search results, 1 retained existing recent row, 0 inserts, 2 enrichment failures. No new independent buyer evidence is claimed. AI/products/publishing/delivery counts unchanged; original business DB unchanged.
- Local final verification: 281 passed; compileall/frontend/PowerShell parser/diff checks PASS. Full sanitized outcomes and local report names are in ACCEPTANCE.md. Remote follow-up CI is linked on PR #3 after push.
- DGA-001 remains BLOCKED on its real positive acceptance; DGA-002 through DGA-005 remain TODO.
