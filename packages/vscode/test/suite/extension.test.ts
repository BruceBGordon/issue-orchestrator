import * as assert from "assert";
import { createRequire } from "module";
import { normalizeClientCapabilities, sessionActionMode } from "../../src/clientCapabilities.js";
import { decideStartOutcome, runStartCommand } from "../../src/startCommand.js";
import type { StartResponse } from "../../src/types.js";

const require = createRequire(import.meta.url);
const vscode = require("vscode") as typeof import("vscode");

interface RecordedStartRun {
  refreshed: number;
  doctorCalls: { errorMessage: string; doctorUrl?: string }[];
  logged: string[];
}

async function runStart(result: StartResponse | null): Promise<RecordedStartRun> {
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

  test("a null result is treated as success, not a silent failure path", async () => {
    const recorded = await runStart(null);

    assert.strictEqual(recorded.refreshed, 1);
    assert.deepStrictEqual(recorded.doctorCalls, []);
  });

  test("decideStartOutcome ignores launch.status and keys off the top-level error", () => {
    // The server owns the mapping; a nested failure status without a
    // normalised top-level error must not be re-derived on the client.
    assert.deepStrictEqual(
      decideStartOutcome({ supervisor: { state: "running" } } as StartResponse),
      { kind: "refresh" }
    );
    assert.deepStrictEqual(
      decideStartOutcome({ error: { message: "" } } as StartResponse),
      { kind: "refresh" }
    );
  });
});
