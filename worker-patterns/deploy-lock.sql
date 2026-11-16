-- _deploy_lock: single-row table that the orchestration script flips during
-- a deploy to block writes from stale Worker isolates.
--
-- HOW IT WORKS:
--   - The deploy script runs `UPDATE _deploy_lock SET locked = 1 WHERE id = 1`
--     after promoting the maintenance version of the Worker, BEFORE applying
--     migrations.
--   - The Worker calls `assertNotLocked(db)` once at the router (see
--     deploy-lock.ts), which selects the row and throws DeployLockError when
--     locked = 1. Individual handlers inherit that gate - do not re-check
--     per-helper. deploy-lock.ts owns the request bound the drain requires.
--   - The deploy pipeline flips locked = 0 on its exit path, which runs on
--     success and on failure, so an ordinary failed deploy releases the lock.
--     A deploy that dies without reaching it - the runner is killed, the
--     network drops, the exit path itself errors - leaves locked = 1, which is
--     what ../workflow-examples/unlock-db.yml exists to clear by hand.
--
-- WHY A SEPARATE TABLE (and not just an env var):
--   The env var protects new isolates that boot during the deploy window.
--   The table protects OLD isolates that were already running when the
--   deploy started - they have the previous env binding values and don't
--   know the new var was set. The table check happens at request time
--   against the live D1 row, so old isolates see the lock immediately.
--
-- STRICT mode + integer CHECK is the consistent convention with the rest
-- of the schema. Single row enforced via `id INTEGER PRIMARY KEY CHECK (id = 1)`.

CREATE TABLE IF NOT EXISTS _deploy_lock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  locked INTEGER NOT NULL DEFAULT 0
) STRICT;

-- INSERT OR IGNORE so re-applying the migration is a no-op once the row exists.
INSERT OR IGNORE INTO _deploy_lock (id, locked) VALUES (1, 0);
