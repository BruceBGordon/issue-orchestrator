import * as path from "path";

export function defaultConfigPath(repoRoot: string): string {
  return path.join(
    repoRoot,
    ".issue-orchestrator",
    "config",
    "modes",
    "default",
    "default.yaml"
  );
}
