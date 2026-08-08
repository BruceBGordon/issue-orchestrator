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
 * response is only a success when it actually carries a start result the server
 * could have produced — not merely when a `supervisor` or `launch` key exists.
 * Both absence and malformation are reachable at runtime: `McpClient.callTool`
 * returns `{}` for empty MCP content, a dropped reply arrives as
 * `null`/`undefined`, and because `callTool<T>()` casts an unvalidated
 * JSON parse to `T`, a version-skewed server can deliver `{launch: null}` or
 * `{supervisor: "running"}` typed as a `StartResponse`. Refreshing on any of
 * those treats absence of evidence as evidence the orchestrator started, which
 * is the one reading that leaves the operator with a green tree and a dead
 * orchestrator. So the check fails closed: anything that is not a recognisable
 * success opens the doctor.
 */
export type StartOutcome =
  | { kind: "refresh" }
  | { kind: "doctor"; errorMessage: string; doctorUrl?: string };

const INVALID_RESPONSE_MESSAGE =
  "the MCP server returned no recognisable start result";
const NO_MESSAGE = "the MCP server reported an error with no message";

/**
 * Nothing here can trust its input's declared type.
 *
 * `McpClient.callTool<T>()` JSON-parses whatever came back over stdio and
 * casts it to `T`. The cast is a compile-time assertion, not a runtime one, so
 * a malformed or version-skewed payload arrives typed as `StartResponse` while
 * being any shape at all. These checks are therefore written against `unknown`.
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Whether the response carries a start result the server could have produced.
 *
 * Shape only. `launch.status` is checked for being a string, never for *which*
 * string: the server owns what a status means, and re-deriving that here is
 * exactly the duplication this command avoids. The point is narrower — that a
 * member is actually there and actually a start result, so `{launch: null}` and
 * `{supervisor: "running"}` cannot pass for one.
 */
function hasRecognisableStartResult(envelope: Record<string, unknown>): boolean {
  const supervisor = envelope.supervisor;
  if (isRecord(supervisor) && typeof supervisor.state === "string") {
    return true;
  }
  const launch = envelope.launch;
  return (
    isRecord(launch) &&
    typeof launch.status === "string" &&
    typeof launch.launched === "boolean"
  );
}

function readDoctorUrl(envelope: Record<string, unknown> | null): string | undefined {
  const hint = envelope?.ui_hint;
  if (isRecord(hint) && typeof hint.url === "string") {
    return hint.url;
  }
  return undefined;
}

export function decideStartOutcome(
  result: StartResponse | null | undefined
): StartOutcome {
  const envelope = isRecord(result) ? result : null;
  const doctor = (reason: string): StartOutcome => ({
    kind: "doctor",
    errorMessage: `Orchestrator failed to start: ${reason}`,
    doctorUrl: readDoctorUrl(envelope),
  });

  if (envelope === null) {
    return doctor(INVALID_RESPONSE_MESSAGE);
  }

  const error = envelope.error;
  if (error !== undefined) {
    // An error the server could not describe is still an error: only the
    // explanation is missing, not the failure.
    const message = isRecord(error) ? error.message : undefined;
    return doctor(
      typeof message === "string" && message.trim() ? message : NO_MESSAGE
    );
  }

  if (!hasRecognisableStartResult(envelope)) {
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
