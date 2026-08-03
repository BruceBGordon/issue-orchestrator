import type { StartResponse } from "./types.js";

/**
 * Collaborators the start command drives.
 *
 * Kept as an explicit interface so the command body is testable without a
 * webview, an extension host, or a live MCP server.
 */
export interface StartCommandDeps {
  start(): Promise<StartResponse | null | undefined>;
  refresh(): Promise<void>;
  openDoctor(options: { errorMessage: string; doctorUrl?: string }): Promise<void>;
  log(message: string): void;
}

/**
 * What the start command should do with a result.
 *
 * The server is authoritative about whether a start failed: `orchestrator.start`
 * normalises every failure — a thrown exception *and* an ordinary
 * `LaunchResult` with `status` of `doctor_error`/`launch_error` — onto the
 * top-level `error` object. Deciding failure here by re-reading
 * `launch.status` would duplicate that policy on both sides of the wire, so we
 * key off `error` alone. `launch` is detail for the operator, not a signal.
 */
export type StartOutcome =
  | { kind: "refresh" }
  | { kind: "doctor"; errorMessage: string; doctorUrl?: string };

export function decideStartOutcome(
  result: StartResponse | null | undefined
): StartOutcome {
  const message = result?.error?.message;
  if (!message) {
    return { kind: "refresh" };
  }
  return {
    kind: "doctor",
    errorMessage: `Orchestrator failed to start: ${message}`,
    doctorUrl: result?.ui_hint?.url,
  };
}

/**
 * Start the orchestrator, opening the doctor panel when it fails.
 *
 * A failed start must NOT refresh the tree: refreshing reads as success to the
 * operator and hides the reason the launch failed.
 */
export async function runStartCommand(deps: StartCommandDeps): Promise<void> {
  const outcome = decideStartOutcome(await deps.start());
  if (outcome.kind === "doctor") {
    deps.log(outcome.errorMessage);
    await deps.openDoctor({
      errorMessage: outcome.errorMessage,
      doctorUrl: outcome.doctorUrl,
    });
    return;
  }
  await deps.refresh();
}
