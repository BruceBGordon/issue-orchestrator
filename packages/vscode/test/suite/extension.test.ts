import * as assert from "assert";
import { createRequire } from "module";
import { normalizeClientCapabilities, sessionActionMode } from "../../src/clientCapabilities.js";
import { decideStartOutcome, runStartCommand } from "../../src/startCommand.js";
import type { StartResponse } from "../../src/types.js";
import { defaultConfigPath } from "../../src/configPaths.js";

const require = createRequire(import.meta.url);
const vscode = require("vscode") as typeof import("vscode");

interface RecordedStartRun {
  refreshed: number;
  doctorCalls: { errorMessage: string; doctorUrl?: string }[];
  logged: string[];
}

async function runStart(
  result: StartResponse | null | undefined
): Promise<RecordedStartRun> {
  const recorded: RecordedStartRun = { refreshed: 0, doctorCalls: [], logged: [] };
  await runStartCommand({
    start: async () => result,
    refresh: async () => {
      recorded.refreshed += 1;
    },
    openDoctor: async (options) => {
      recorded.doctorCalls.push(options);
    },
    log: (message) => recorded.logged.push(message),
  });
  return recorded;
}

suite("Issue Orchestrator Extension", () => {
  test("extension activates", async () => {
    const extension = vscode.extensions.getExtension("issue-orchestrator.issue-orchestrator");
    assert.ok(extension, "Extension not found");
    await extension.activate();
    assert.ok(extension.isActive);
  });

  test("normalizeClientCapabilities defaults missing fields", () => {
    const capabilities = normalizeClientCapabilities({ focus_session: true });
    assert.strictEqual(capabilities.focus_session, true);
    assert.strictEqual(capabilities.open_path, false);
    assert.strictEqual(capabilities.reveal_worktree, false);
    assert.strictEqual(capabilities.local_server_paths_only, true);
    assert.strictEqual(capabilities.host_platform, "unknown");
  });

  test("sessionActionMode falls back to console when focus unsupported", () => {
    assert.strictEqual(sessionActionMode({ focus_session: false }), "console");
    assert.strictEqual(sessionActionMode({ focus_session: true }), "focus");
  });

  test("default config path uses the default mode directory", () => {
    assert.strictEqual(
      defaultConfigPath("/work/repo"),
      "/work/repo/.issue-orchestrator/config/modes/default/default.yaml"
    );
  });
});

suite("Start command", () => {
  test("a successful start refreshes the tree and never opens doctor", async () => {
    const recorded = await runStart({
      launch: { status: "ok", launched: true },
    });

    assert.strictEqual(recorded.refreshed, 1);
    assert.deepStrictEqual(recorded.doctorCalls, []);
  });

  test("a doctor_error opens doctor with the hint URL and does not refresh", async () => {
    const recorded = await runStart({
      launch: { status: "doctor_error", launched: false },
      error: { message: "Doctor checks failed — github_auth: token expired", type: "DoctorError" },
      ui_hint: { kind: "doctor", url: "http://127.0.0.1:19080/api/doctor" },
    });

    assert.strictEqual(recorded.refreshed, 0, "a failed start must not refresh");
    assert.strictEqual(recorded.doctorCalls.length, 1);
    assert.strictEqual(
      recorded.doctorCalls[0].doctorUrl,
      "http://127.0.0.1:19080/api/doctor"
    );
    assert.strictEqual(
      recorded.doctorCalls[0].errorMessage,
      "Orchestrator failed to start: Doctor checks failed — github_auth: token expired"
    );
    assert.deepStrictEqual(recorded.logged, [recorded.doctorCalls[0].errorMessage]);
  });

  test("a launch_error opens doctor and does not refresh", async () => {
    const recorded = await runStart({
      launch: { status: "launch_error", launched: false, error: "port already bound" },
      error: { message: "port already bound", type: "LaunchError" },
      ui_hint: { kind: "doctor", url: "http://127.0.0.1:19080/api/doctor" },
    });

    assert.strictEqual(recorded.refreshed, 0);
    assert.strictEqual(recorded.doctorCalls.length, 1);
    assert.strictEqual(
      recorded.doctorCalls[0].doctorUrl,
      "http://127.0.0.1:19080/api/doctor"
    );
  });

  test("doctor opens even when no hint URL is available", async () => {
    const recorded = await runStart({
      error: { message: "boom", type: "RuntimeError" },
    });

    assert.strictEqual(recorded.refreshed, 0);
    assert.strictEqual(recorded.doctorCalls.length, 1);
    assert.strictEqual(recorded.doctorCalls[0].doctorUrl, undefined);
  });

  test("a missing response fails closed and never refreshes", async () => {
    // Refreshing here would show a green tree for an orchestrator that may
    // not be running: absence of evidence is not evidence of a start.
    for (const empty of [null, undefined]) {
      const recorded = await runStart(empty);

      assert.strictEqual(recorded.refreshed, 0);
      assert.strictEqual(recorded.doctorCalls.length, 1);
    }
  });

  test("an unrecognisable envelope fails closed and never refreshes", async () => {
    // `McpClient.callTool` returns `{}` for empty MCP content, so this is a
    // reachable response, not a hypothetical one.
    const recorded = await runStart({} as StartResponse);

    assert.strictEqual(recorded.refreshed, 0);
    assert.strictEqual(recorded.doctorCalls.length, 1);
  });

  test("a malformed success member fails closed and never refreshes", async () => {
    // `callTool<T>()` casts an unvalidated JSON parse to `T`, so a
    // version-skewed server can deliver any of these typed as a StartResponse.
    const malformed = [
      { launch: null },
      { supervisor: null },
      { launch: false },
      { supervisor: "running" },
      { launch: {} },
      { supervisor: {} },
      { launch: { status: "ok" } },
      { launch: { status: "ok", launched: "yes" } },
      { supervisor: { state: 1 } },
    ];

    for (const result of malformed) {
      // `as unknown as` on purpose: the compiler rejects a direct cast, which
      // is the whole point — these shapes are unreachable statically and
      // reachable at runtime only because `callTool<T>()` casts an unvalidated
      // parse. That is the gap this check closes.
      const recorded = await runStart(result as unknown as StartResponse);

      assert.strictEqual(
        recorded.refreshed,
        0,
        `refreshed on ${JSON.stringify(result)}`
      );
      assert.strictEqual(
        recorded.doctorCalls.length,
        1,
        `did not open doctor on ${JSON.stringify(result)}`
      );
    }
  });

  test("a well-formed success member still refreshes", async () => {
    // Non-vacuity for the check above: the shapes the server really sends
    // must keep working.
    for (const result of [
      { supervisor: { state: "running" } },
      { launch: { status: "ok", launched: true } },
    ]) {
      const recorded = await runStart(result as StartResponse);

      assert.strictEqual(recorded.refreshed, 1, `did not refresh on ${JSON.stringify(result)}`);
      assert.deepStrictEqual(recorded.doctorCalls, []);
    }
  });

  test("decideStartOutcome ignores launch.status and keys off the top-level error", () => {
    // The server owns the mapping; a nested failure status without a
    // normalised top-level error must not be re-derived on the client.
    assert.deepStrictEqual(
      decideStartOutcome({ supervisor: { state: "running" } } as StartResponse),
      { kind: "refresh" }
    );
    assert.deepStrictEqual(
      decideStartOutcome({
        launch: { status: "ok", launched: true },
      } as StartResponse),
      { kind: "refresh" }
    );
    // The structural check must not become a back door into classification: a
    // failure status the server did not normalise onto `error` is still the
    // server's call, not ours.
    assert.deepStrictEqual(
      decideStartOutcome({
        launch: { status: "doctor_error", launched: false },
      } as StartResponse),
      { kind: "refresh" }
    );
  });

  test("decideStartOutcome rejects an error object with no message", () => {
    // The failure is real even when the server could not describe it; only
    // the explanation is missing.
    const outcome = decideStartOutcome({
      error: { message: "" },
    } as StartResponse);

    assert.strictEqual(outcome.kind, "doctor");
  });

  test("decideStartOutcome still surfaces the doctor URL for an invalid envelope", () => {
    const outcome = decideStartOutcome({
      ui_hint: { kind: "doctor", url: "http://127.0.0.1:19080/api/doctor" },
    } as StartResponse);

    assert.strictEqual(outcome.kind, "doctor");
    assert.strictEqual(
      outcome.kind === "doctor" ? outcome.doctorUrl : undefined,
      "http://127.0.0.1:19080/api/doctor"
    );
  });
});
