# worker-patterns

Copy-paste-and-adapt TypeScript snippets and SQL for the in-Worker side of the
safe-deploy pattern. Pair with the deploy pipeline (`../deploy-lib/`) and the
test harness (`../test-harness/`) to get a deployment story that catches
partial-failure mid-deploy and never leaves the Worker stuck in a half-broken
state.

## What's here

- **`deploy-lock.sql`** - D1 schema: The `_deploy_lock` table DDL - single-row
  table with a `locked` integer flag
- **`deploy-lock.ts`** - Worker code: `assertNotLocked(db)` + `isLocked(db)`
  helpers, plus `DeployLockError`
- **`maintenance-gate.ts`** - Worker code: Top-of-fetch check for
  `env.MAINTENANCE_MODE` returning 503

## How they fit together

There are **layers of defense** against writing to a half-migrated schema during
a deploy:

1. **Maintenance gate (`MAINTENANCE_MODE` env var)** - first line of defense.
   Set in the wrangler `versions upload --var MAINTENANCE_MODE:true` step before
   the maintenance version is promoted. The fetch handler returns 503 for all
   routes (except `/v1/health` for monitoring), and the scheduled handler bails
   on cron ticks. **Why an env var:** wrangler can flip this atomically with the
   version promotion, so there's no window where the new code is live but the
   new env isn't.

2. **Deploy lock (`_deploy_lock` table)** - second line of defense, catches
   stale Worker isolates that were already running an OLD version of the code
   (without the maintenance check) when the deploy started. The orchestration
   script flips `_deploy_lock.locked = 1` after promoting the maintenance
   version and BEFORE applying migrations; stale isolates that try to _admit_ a
   new authenticated request hit `assertNotLocked()` at their router-level gate
   and get a clean `DeployLockError -> 503`.

You need the env var and the lock. The env var alone leaves a window where stale
isolates with the previous binding values are still serving requests. The lock
alone is effective only if the Worker actually calls `assertNotLocked()` on the
request admission path - see placement below.

## Where to put these in your Worker

```ts
// At the top of your fetch handler - IMMEDIATELY after CORS / before
// auth - return 503 if MAINTENANCE_MODE is set. Health is the one
// exception so monitoring can verify the Worker is up.
if (url.pathname !== "/v1/health" && env.MAINTENANCE_MODE === "true") {
  return new Response(
    "Service undergoing maintenance. Please try again in a few minutes.",
    { status: 503, headers: { "Retry-After": "120" } },
  );
}

// In your scheduled() handler - bail before any DB work.
if (env.MAINTENANCE_MODE === "true") {
  console.warn("[maintenance] scheduled cron skipped - deployment in progress");
  return;
}

// In the ROUTER, right after auth succeeds and before dispatch, call
// assertNotLocked ONCE. Individual handlers inherit this guarantee and
// must not re-check - see "Bounded-in-flight drain gate" below for why.
try {
  await assertNotLocked(db);
  return await handler(uid, request, db, env);
} catch (err) {
  if (err instanceof DeployLockError) {
    return new Response(err.message, {
      status: 503,
      headers: { "Retry-After": "120" },
    });
  }
  throw err;
}

// In your scheduled() handler, call assertNotLocked directly (scheduled
// invocations bypass the fetch router, so they don't inherit anything).
// The MAINTENANCE_MODE check above catches new isolates; this catches
// stale isolates running old code.
await assertNotLocked(db);
```

### Bounded-in-flight drain gate

`assertNotLocked` goes at the router level - not inside individual handlers.
When a request is admitted past the router, it's allowed to finish even if the
deploy lock flips to 1 a moment later. The deploy pipeline's drain protects the
bookmark only when `DEPLOY_DRAIN_SECONDS` exceeds the consumer's enforced
worst-case total runtime for an admitted request:

- Every outbound `fetch()` in the Worker should carry `AbortSignal.timeout()`
  with a ceiling that contributes to that total request bound.
- [Cloudflare Workers CPU limits](https://developers.cloudflare.com/workers/platform/limits/#cpu-time)
  are plan-dependent: 10 ms on Free, while Paid defaults to 30 s and permits a
  higher configured ceiling. Cloudflare also allows infrequent overruns, so
  `limits.cpu_ms` is defense in depth rather than a hard drain boundary.
- If CPU-bound work or another operation can make the request exceed the default
  drain, bound that work independently or raise `DEPLOY_DRAIN_SECONDS` above the
  enforced worst case before adopting this pattern.
- Any write completed during the drain window is captured by the D1 Time Travel
  bookmark taken after the drain, so drain-window writes persist whether the
  deploy succeeds or fails: a failed deploy restores to that bookmark, which
  already holds them. On a relocation the drain runs before the copy, so the
  copy holds them.

### Guard each write in its own SQL when the drain cannot bound your writers

The drain is a wait, not a barrier. Anything admitted before the lock that
outlives it can still write after the drain ends, and that write is lost: after
the bookmark on the windowed body, which a failed deploy restores past, and
after the copy read the source on the relocation body, which the promote leaves
behind. That covers every database writer, not only fetch requests: a
`scheduled()` invocation that passed its lock check before the lock flipped (a
cron can run for many minutes), and any `ctx.waitUntil()` task that writes
after its response has gone.

If you cannot enforce a bound below `DEPLOY_DRAIN_SECONDS` on every one of
those writers, the router and `scheduled()` gates alone are not enough: put the
lock check inside every write, so a write that runs once the lock is set changes
nothing.

```sql
UPDATE accounts SET balance = ?1
WHERE id = ?2 AND (SELECT locked FROM _deploy_lock WHERE id = 1) = 0
```

Run guarded writes in one D1 batch whose first statement reads the lock
(`SELECT locked FROM _deploy_lock WHERE id = 1`), and fail the request when that
read says locked, so the caller sees a retryable 503 rather than a success that
wrote nothing. A guarded write outside such a batch is worse than an unguarded
one: it silently does nothing while the handler reports success.

This is a **bounded-in-flight drain gate, not a strict per-operation gate.**
Adding `assertNotLocked` inside every D1 helper does not tighten the in-flight
window (the drain already handles that) but it does re-pay a D1 round-trip on
every helper call, which is measurable on CPU-sensitive hot paths. If your
Worker's hot path is already close to the CPU limit, the router-gate pattern
alone is the right answer.

Leave a diagnostic `/v1/health` (or equivalent) endpoint _above_ the router gate
so monitoring can observe `deployLocked` state without being rejected by it. The
enforcement path (the router gate, plus any `scheduled()` entrypoint) should
read `_deploy_lock` against a `first-primary` D1 session if you use D1 Sessions
API, because a lagged replica can still report `locked=0` after CI flipped the
row to 1. The diagnostic health read has the opposite cost profile - a primary
hiccup shouldn't knock your CI probe over.

## Migration to add the lock table

`deploy-lock.sql` is a one-shot DDL - apply it as a numbered migration
(`NNNN_deploy_lock.sql`, where `NNNN` is your project's next migration number)
before deploying the Worker code that calls `assertNotLocked()`. The DDL is
`CREATE TABLE IF NOT EXISTS` plus an `INSERT OR IGNORE`, so it's safe to
re-apply.

After the migration is in production, the deploy pipeline flips `locked = 1` /
`locked = 0` via `wrangler d1 execute` calls from its lock and unlock steps - no
further schema changes.

If something goes catastrophically wrong and the deploy pipeline's recovery path
couldn't reach the unlock step (e.g. the runner died mid-run), use the example
workflow at `../workflow-examples/unlock-db.yml` to manually flip the lock back
to 0.

## What the test harness sees

The mock-wrangler binary classifies `wrangler d1 execute ... locked = 1` as the
`lock` step and `wrangler d1 execute ... locked = 0` as `unlock`. Each is an
empty fixture (the deploy pipeline doesn't parse their output) - the mock just
needs the call to be detectable so the test driver can assert "the lock step
happened" or "lock did NOT happen because we failed before that point."

## Adapting the patterns for your Worker

The files in this directory are starting points, not drop-in modules.
Concretely, you'll typically:

1. **Add the lock table.** Copy `deploy-lock.sql` into your migrations directory
   as `NNNN_deploy_lock.sql` and apply it.
2. **Add the lock helpers.** Copy `deploy-lock.ts` into your Worker source tree
   (e.g. as `src/lock.ts`). Replace the `DbHandle` import on line 18 with
   whatever D1 prepared-statement type your code uses (often just `D1Database`
   directly).
3. **Wire the maintenance gate.** Add the `MAINTENANCE_MODE` check from
   `maintenance-gate.ts` at the top of your fetch handler, immediately after
   CORS and before auth.
4. **Call `assertNotLocked` once at the router** - right after auth and before
   dispatching to handlers. Individual handlers inherit the gate's guarantee and
   must not re-check. See "Bounded-in-flight drain gate" above for why
   per-handler rechecks are deliberately avoided (they re-pay a D1 round-trip
   without tightening the in-flight window).
5. **Wrap dispatch in a try/catch** that converts `DeployLockError` to a 503
   response (see the "Where to put these in your Worker" section above).
6. **Call `assertNotLocked` directly in `scheduled()`** because scheduled
   invocations bypass the fetch router.
