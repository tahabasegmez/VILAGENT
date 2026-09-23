// `pnpm dev`: start the desktop app from source. Editors built on Electron (VS Code)
// leak ELECTRON_RUN_AS_NODE into their terminals, which would make Electron run as
// plain Node, so it is removed here.
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const electronBinary = createRequire(import.meta.url)("electron");
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const child = spawn(electronBinary, ["electron/main.cjs"], { stdio: "inherit", env });
child.on("exit", (code) => process.exit(code ?? 0));
