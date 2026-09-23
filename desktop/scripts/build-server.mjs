// Freeze the Python gateway into agent/dist/vilagent-server with PyInstaller.
// Uses VILAGENT_PYTHON (environment or the repository .env), else `python`.
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const agent = join(repoRoot, "agent");

function pythonFromDotEnv() {
  const file = join(repoRoot, ".env");
  if (!existsSync(file)) return undefined;
  const match = /^\s*VILAGENT_PYTHON\s*=\s*(.+?)\s*(?:#.*)?$/m.exec(readFileSync(file, "utf8"));
  return match?.[1]?.replace(/^(['"])(.*)\1$/, "$2");
}

const python = process.env.VILAGENT_PYTHON ?? pythonFromDotEnv() ?? "python";
const args = ["-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", "dist", "--workpath", "build", "vilagent-server.spec"];
console.log(`> ${python} ${args.join(" ")}`);
const result = spawnSync(python, args, {
  cwd: agent,
  stdio: "inherit",
  env: { ...process.env, PYTHONNOUSERSITE: "1" },
});
process.exit(result.status ?? 1);
