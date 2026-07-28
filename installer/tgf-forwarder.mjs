#!/usr/bin/env node
// npx -y tgf-forwarder  ->  installs the `tgf` CLI via uv (global).
// This is a thin installer: it shells out to `uv tool install` from the GitHub repo.
import { execFileSync } from "node:child_process";
import { which } from "node:process";

const REPO = "git+https://github.com/dbillion/tgforwarder.git";

function have(cmd) {
  try {
    execFileSync(cmd, ["--version"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function run(cmd, args) {
  try {
    execFileSync(cmd, args, { stdio: "inherit" });
    return true;
  } catch (e) {
    console.error(`\n✗ Failed to run: ${cmd} ${args.join(" ")}`);
    console.error(String(e.stderr || e.message || e));
    return false;
  }
}

console.log("tgf-forwarder installer (uv-based)");

if (!have("uv")) {
  console.error("\n✗ `uv` is not installed. Install it first:");
  console.error("    curl -LsSf https://astral.sh/uv/install.sh | sh");
  process.exit(1);
}

if (run("uv", ["tool", "install", REPO])) {
  console.log("\n✓ `tgf` installed globally. Try:  tgf --help");
  console.log("  Configure a local .env with TELEGRAM_API_ID / TELEGRAM_API_HASH,");
  console.log("  then run:  tgf forward --source <CHANNEL> --dest <YOUR_USER_ID> --all");
} else {
  process.exit(1);
}
