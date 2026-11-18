// The `./db` module deploy-lock.ts imports, for the type check only. A consumer
// supplies their own; most use D1's binding type directly, as this does.
export type DbHandle = D1Database;
