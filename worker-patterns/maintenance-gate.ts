// Maintenance-mode gate: returns 503 to all requests (except a health
// endpoint) when env.MAINTENANCE_MODE is "true".
//
// This is the FIRST line of defense during a deploy. The orchestration
// script promotes a maintenance-mode version of the Worker (uploaded with
// `wrangler versions upload --var MAINTENANCE_MODE:true`) before applying
// migrations, so all NEW isolates serve 503 to write requests while the
// schema is changing. The DEPLOY_LOCK table (see deploy-lock.ts) is the
// SECOND line of defense for OLD isolates that were already running.
//
// CONSUMER NOTES:
//   - Wire the request-time check at the very top of your fetch() handler,
//     immediately after CORS but BEFORE any auth or routing.
//   - Wire the scheduled() check at the very top of your scheduled handler.
//   - The HEALTH_PATH constant is the one exception - your monitoring needs
//     to verify the Worker is up even when in maintenance mode.
//
// HOW THE MAINTENANCE WINDOW LIFTS:
//   - The orchestration script's "promote_final_version" step uploads a NEW
//     version of the Worker WITHOUT --var MAINTENANCE_MODE:true and promotes
//     it to 100%.  At that moment, all new isolates start without the env
//     var set, and the gate stops returning 503.
//   - There's no need to "unset" the env var - promoting a different
//     version atomically replaces the binding set.

const HEALTH_PATH = "/v1/health"; // CONSUMER: change to your health endpoint
const RETRY_AFTER_SECONDS = 120;

export interface EnvWithMaintenance {
  MAINTENANCE_MODE?: string;
}

// Returns a 503 Response if maintenance mode is on AND the request isn't
// for the health endpoint. Returns null otherwise - caller should continue
// processing the request normally.
//
// Usage at the top of your fetch handler:
//
//   const maintResponse = checkMaintenanceMode(url.pathname, env);
//   if (maintResponse) return maintResponse;
//
export function checkMaintenanceMode(
  pathname: string,
  env: EnvWithMaintenance,
): Response | null {
  if (pathname === HEALTH_PATH) return null;
  if (env.MAINTENANCE_MODE !== "true") return null;
  console.warn(`[maintenance] ${pathname} rejected - deployment in progress`);
  return new Response(
    "Service undergoing maintenance. Please try again in a few minutes.",
    {
      status: 503,
      headers: { "Retry-After": String(RETRY_AFTER_SECONDS) },
    },
  );
}

// Returns true if the scheduled handler should bail. Use as:
//
//   if (skipScheduledOnMaintenance(env)) return;
//
export function skipScheduledOnMaintenance(env: EnvWithMaintenance): boolean {
  if (env.MAINTENANCE_MODE !== "true") return false;
  console.warn("[maintenance] scheduled cron skipped - deployment in progress");
  return true;
}
