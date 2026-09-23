// `pnpm dist`: static UI → frozen Python gateway → Windows installer (dist-electron/).
import { spawnSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

function run(command, args, options = {}) {
  console.log(`> ${command} ${args.join(" ")}`);
  const result = spawnSync(command, args, { stdio: "inherit", shell: process.platform === "win32", env, ...options });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

// electron-builder shells out to `pnpm`; when pnpm only exists through corepack,
// expose temporary corepack shims on PATH.
if (spawnSync("pnpm", ["--version"], { shell: true, env }).status !== 0) {
  const shimDir = mkdtempSync(join(tmpdir(), "vilagent-pnpm-"));
  run("corepack", ["enable", "--install-directory", shimDir, "pnpm"]);
  env.PATH = `${shimDir}${delimiter}${env.PATH}`;
}

run("corepack", ["pnpm", "build"]);
run("node", ["scripts/build-server.mjs"]);
run("corepack", ["pnpm", "exec", "electron-builder", ...process.argv.slice(2)]);
