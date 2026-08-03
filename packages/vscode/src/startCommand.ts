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
 * The server is authoritative about *why* a start failed: `orchestrator.start`
 * normalises every failure — a thrown exception *and* an ordinary
 * `LaunchResult` with `status` of `doctor_error`/`launch_error` — onto the
 * top-level `error` object. Deciding failure here by re-reading
 * `launch.status` would duplicate that policy on both sides of the wire, so we
 * never inspect it. `launch` is detail for the operator, not a signal.
 *
 * Validating the *envelope* is a different question, and it belongs here. A
 * response that carries neither `supervisor` nor `launch` is not a start result
 * the server produced under this contract — `McpClient.callTool` returns `{}`
 * for empty MCP content, and a dropped or malformed reply arrives as
 * `null`/`undefined`. Refreshing on those treats absence of evidence as
 * evidence the orchestrator started, which is the one reading that leaves the
 * operator with a green tree and a dead orchestrator. So the envelope check
 * fails closed: anything that is not a recognisable success opens the doctor.
 */
export type StartOutcome =
  | { kind: "refresh" }
  | { kind: "doctor"; errorMessage: string; doctorUrl?: string };

const INVALID_RESPONSE_MESSAGE =
  "the MCP server returned no recognisable start result";

export function decideStartOutcome(
  result: StartResponse | null | undefined
): StartOutcome {
  const doctor = (reason: string): StartOutcome => ({
    kind: "doctor",
    errorMessage: `Orchestrator failed to start: ${reason}`,
    doctorUrl: result?.ui_hint?.url,
  });

  if (!result || typeof result !== "object") {
    return doctor(INVALID_RESPONSE_MESSAGE);
  }

  const message = result.error?.message;
  if (message) {
    return doctor(message);
  }
  if (result.error !== undefined) {
    // An error object the server could not describe is still an error. Only
    // the explanation is missing, not the failure.
    return doctor("the MCP server reported an error with no message");
  }
  if (result.supervisor === undefined && result.launch === undefined) {
    return doctor(INVALID_RESPONSE_MESSAGE);
  }
  return { kind: "refresh" };
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
