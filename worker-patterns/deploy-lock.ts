// Database-level deploy lock helpers. The CI pipeline sets _deploy_lock.locked
// = 1 before capturing a D1 Time Travel bookmark, closing the race window
// where a stale worker isolate (still running old code without
// MAINTENANCE_MODE set) could write to D1 after the bookmark was taken.
//
// RECOMMENDED PATTERN: call assertNotLocked() ONCE at the router, right after
// auth and before dispatch - not in every handler. Individual handlers inherit
// the router gate's guarantee. The drain safely covers requests admitted before
// the lock flipped only when DEPLOY_DRAIN_SECONDS exceeds the consumer's
// enforced worst-case total request runtime. Outbound fetch timeouts contribute
// to that bound; limits.cpu_ms is defense in depth because Workers can allow
// infrequent overruns. Bound longer work or raise the drain to match it; see
// worker-patterns/README.md.
// Drain-window writes are captured in the bookmark taken after the drain, so
// they persist on deploy success and on failure alike. A writer that can
// outlive the drain - a request, a scheduled() run, a waitUntil() task - needs
// the lock check inside its writes as well - see worker-patterns/README.md
// "Guard each write in its own SQL". This is a
// bounded-in-flight drain gate, not a strict per-operation gate - adding
// assertNotLocked() inside every D1 helper re-pays a round-trip on every call
// without tightening the in-flight window the drain already handles. See
// worker-patterns/README.md.
//
// PAIR WITH:
//   - schema/schema.sql (the _deploy_lock table DDL)
//   - maintenance-gate.ts (the env-var check at the top of fetch())
//   - the deploy pipeline at ../deploy-lib/safe_deploy_pipeline.py, which
//     flips locked 0<->1 via `wrangler d1 execute "UPDATE _deploy_lock SET
//     locked = N"` from its lock and unlock steps
//   - the unlock workflow at ../workflow-examples/unlock-db.yml, used by
//     operators to manually flip the lock back to 0 after a catastrophic
//     deploy failure where the recovery path couldn't reach unlock
//
// CONSUMER NOTE: replace the `DbHandle` import with whatever your code uses
// for the D1 prepared-statement type. Most consumers use `D1Database`
// directly; some prefer a thin wrapper that adds prepared-statement
// caching or telemetry.

import type { DbHandle } from "./db"; // CONSUMER: replace with your D1 type

export class DeployLockError extends Error {
  // CONSUMER NOTE: this message is HTTP-client-facing - your fetch handler
  // wraps DeployLockError to a 503 with a Retry-After header. Keep it short
  // and end-user friendly. Operator-facing recovery instructions ("how do I
  // manually unlock?") belong in your runbook, not in the user-visible
  // error body. The unlock procedure is: run the workflow at
  // workflow-examples/unlock-db.yml (or your equivalent), which calls
  // `wrangler d1 execute --command "UPDATE _deploy_lock SET locked = 0
  // WHERE id = 1"` against the affected database.
  constructor() {
    super(
      "Database is locked for deployment. Please try again in a few minutes.",
    );
    this.name = "DeployLockError";
  }
}

// Throws DeployLockError if the lock row says locked = 1, OR if the table
// is missing entirely (which means the schema isn't applied yet - treat
// as locked because business queries would fail anyway).
//
// Call this ONCE at the router (right after auth, before dispatch) and, for
// scheduled handlers, once at the top of scheduled(). Do NOT re-check inside
// individual handlers or D1 helpers - see the file header for the
// bounded-in-flight drain gate rationale.
export async function assertNotLocked(db: DbHandle): Promise<void> {
  try {
    const row = await db
      .prepare("SELECT locked FROM _deploy_lock WHERE id = 1")
      .first<{ locked: number }>();
    if (row?.locked === 1) throw new DeployLockError();
  } catch (err: unknown) {
    if (err instanceof Error && err.message.includes("no such table")) {
      // Missing _deploy_lock means the schema hasn't been applied yet
      // (e.g. DB was reset). Treat as locked - business logic queries
      // would fail anyway on missing tables.
      throw new DeployLockError();
    }
    throw err;
  }
}

// Same query as assertNotLocked but returns boolean instead of throwing.
// Use this in your /v1/health endpoint so monitoring can see the deploy
// state without burning a 503.
export async function isLocked(db: DbHandle): Promise<boolean> {
  try {
    const row = await db
      .prepare("SELECT locked FROM _deploy_lock WHERE id = 1")
      .first<{ locked: number }>();
    return row?.locked === 1;
  } catch (err: unknown) {
    if (err instanceof Error && err.message.includes("no such table")) {
      return true;
    }
    throw err;
  }
}
