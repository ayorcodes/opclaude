#!/usr/bin/env node
// Windows installer for opclaude — no PowerShell execution policy required.
// Requires: Node.js (for this script), git (already used to get here).
// Usage: node install.js
"use strict";

const { execSync, spawnSync } = require("child_process");
const crypto  = require("crypto");
const fs      = require("fs");
const path    = require("path");
const os      = require("os");
const readline = require("readline");

if (process.platform !== "win32") {
  console.error("install.js is for Windows. On macOS/Linux, run ./install.sh instead.");
  process.exit(1);
}

const REPO_DIR        = __dirname;
const STATE_DIR       = path.join(os.homedir(), ".config", "opclaude");
const ENV_FILE        = path.join(STATE_DIR, ".env");
const BIN_DIR         = path.join(os.homedir(), ".local", "bin");
const LITELLM_VERSION = "1.89.3";

fs.mkdirSync(STATE_DIR, { recursive: true });
fs.mkdirSync(BIN_DIR,   { recursive: true });

console.log("== opclaude install (Windows) ==");

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function run(cmd, opts = {}) {
  return execSync(cmd, { stdio: "inherit", ...opts });
}

function which(name) {
  const r = spawnSync(process.platform === "win32" ? "where" : "which", [name],
    { stdio: "pipe" });
  return r.status === 0;
}

function ask(prompt, secret = false) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    if (secret) {
      // hide input by suppressing echo
      const rl2 = readline.createInterface({
        input: process.stdin,
        output: process.stdout,
        terminal: true,
      });
      process.stdout.write(prompt);
      process.stdin.setRawMode(true);
      let input = "";
      process.stdin.on("data", function handler(ch) {
        ch = ch.toString();
        if (ch === "\r" || ch === "\n") {
          process.stdin.setRawMode(false);
          process.stdin.removeListener("data", handler);
          process.stdout.write("\n");
          rl2.close();
          resolve(input);
        } else if (ch === "") {
          process.exit();
        } else if (ch === "") {
          if (input.length) input = input.slice(0, -1);
        } else {
          input += ch;
        }
      });
    } else {
      rl.question(prompt, (answer) => { rl.close(); resolve(answer); });
    }
  });
}

async function confirm(prompt) {
  const reply = await ask(prompt + " [Y/n] ");
  return !reply.match(/^[Nn]/);
}

// refresh PATH from registry for this process
function refreshPath() {
  try {
    const r = spawnSync("powershell", ["-Command",
      '[Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [Environment]::GetEnvironmentVariable("PATH","User")'
    ], { stdio: "pipe", encoding: "utf8" });
    if (r.status === 0 && r.stdout.trim()) {
      process.env.PATH = r.stdout.trim();
    }
  } catch { /* ignore */ }
}

function readEnv() {
  const env = {};
  try {
    fs.readFileSync(ENV_FILE, "utf8").split(/\r?\n/).forEach((line) => {
      const m = line.match(/^([^=]+)=(.*)$/);
      if (m) env[m[1]] = m[2];
    });
  } catch { /* file doesn't exist yet */ }
  return env;
}

// ---------------------------------------------------------------------------
// uv
// ---------------------------------------------------------------------------
if (!which("uv")) {
  console.log("\nuv (the Python tool installer litellm runs under) is not installed.");
  const yes = await confirm("Install it now via winget?");
  if (!yes) {
    console.error("Install uv yourself (https://docs.astral.sh/uv/) and re-run.");
    process.exit(1);
  }
  run("winget install --id astral-sh.uv -e --accept-package-agreements --accept-source-agreements");
  refreshPath();
  if (!which("uv")) {
    console.error("uv installed but not found on PATH yet. Open a new terminal and re-run.");
    process.exit(1);
  }
}

// ---------------------------------------------------------------------------
// Claude Code (requires npm / Node.js — already running, so npm must be near)
// ---------------------------------------------------------------------------
if (!which("claude")) {
  console.log("\nClaude Code CLI ('claude') is not installed.");
  if (!which("npm")) {
    console.error("npm not found. Install Node.js from https://nodejs.org or via winget install OpenJS.NodeJS, then re-run.");
    process.exit(1);
  }
  const yes = await confirm("Install it now via npm?");
  if (!yes) {
    console.error("Install Claude Code yourself (npm install -g @anthropic-ai/claude-code) and re-run.");
    process.exit(1);
  }
  run("npm install -g @anthropic-ai/claude-code");
  refreshPath();
  if (!which("claude")) {
    console.error("claude installed but not found on PATH yet. Open a new terminal and re-run.");
    process.exit(1);
  }
}

// ---------------------------------------------------------------------------
// litellm + patch
// ---------------------------------------------------------------------------
console.log(`\nInstalling litellm ${LITELLM_VERSION} via uv ...`);
run(`uv tool install "litellm==${LITELLM_VERSION}" --force --with "litellm[proxy,extra-proxy]"`);

console.log("Applying our patch for litellm bug #2 (see FIX.md) ...");
// Use -ExecutionPolicy Bypass for the PS1 patcher — this flag bypasses policy
// for this single invocation without changing any system settings.
const applyPs1 = path.join(REPO_DIR, "patches", "apply.ps1");
run(`powershell -ExecutionPolicy Bypass -File "${applyPs1}"`);

// ---------------------------------------------------------------------------
// secrets
// ---------------------------------------------------------------------------
const saved = readEnv();
let OPENCODE_API_KEY  = saved.OPENCODE_API_KEY  || "";
let LITELLM_MASTER_KEY = saved.LITELLM_MASTER_KEY || "";

if (!OPENCODE_API_KEY) {
  console.log("");
  OPENCODE_API_KEY = await ask("Enter your opencode API key (OPENCODE_API_KEY): ", true);
  if (!OPENCODE_API_KEY) {
    console.error("An opencode API key is required (https://opencode.ai — needs a Go subscription).");
    process.exit(1);
  }
}

if (!LITELLM_MASTER_KEY) {
  LITELLM_MASTER_KEY = "sk-" + crypto.randomBytes(32).toString("hex");
}

const envContent = `OPENCODE_API_KEY=${OPENCODE_API_KEY}\nLITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}\n`;
fs.writeFileSync(ENV_FILE, envContent, { mode: 0o600 });
console.log(`Saved secrets to ${ENV_FILE}`);

// ---------------------------------------------------------------------------
// .cmd shims in BIN_DIR
// ---------------------------------------------------------------------------
const shims = {
  "opclaude":       `@echo off\nnode "${path.join(REPO_DIR, "bin", "opclaude.js")}" %*\n`,
  "opclaude-proxy": `@echo off\nnode "${path.join(REPO_DIR, "bin", "opclaude-proxy.js")}" %*\n`,
  "oc":             `@echo off\nnode "${path.join(REPO_DIR, "bin", "oc.js")}" %*\n`,
  "oc-classify":    `@echo off\nuv run python "${path.join(REPO_DIR, "bin", "oc-classify")}" %*\n`,
};

for (const [name, content] of Object.entries(shims)) {
  fs.writeFileSync(path.join(BIN_DIR, `${name}.cmd`), content, "ascii");
}
console.log(`Wrote opclaude, opclaude-proxy, oc, oc-classify shims into ${BIN_DIR}`);

// ---------------------------------------------------------------------------
// router config
// ---------------------------------------------------------------------------
const routerDst = path.join(STATE_DIR, "router.yaml");
if (!fs.existsSync(routerDst)) {
  fs.copyFileSync(path.join(REPO_DIR, "router.yaml.example"), routerDst);
  console.log(`Seeded ${routerDst} from router.yaml.example.`);
}

// ---------------------------------------------------------------------------
// PATH — use PowerShell inline (no script file = no execution policy issue)
// ---------------------------------------------------------------------------
try {
  const r = spawnSync("powershell", ["-Command",
    `[Environment]::GetEnvironmentVariable("PATH","User")`
  ], { stdio: "pipe", encoding: "utf8" });
  const userPath = (r.stdout || "").trim();
  if (!userPath.toLowerCase().includes(BIN_DIR.toLowerCase())) {
    spawnSync("powershell", ["-Command",
      `[Environment]::SetEnvironmentVariable("PATH", "${BIN_DIR};${userPath}", "User")`
    ], { stdio: "inherit" });
    console.log(`\nAdded ${BIN_DIR} to your user PATH.`);
    console.log("Open a new terminal window for the change to take effect.");
  }
} catch { /* PATH update failed silently — not fatal */ }

console.log("\nDone. Run 'opclaude' to start Claude Code routed through opencode models.");
console.log("Manage the background proxy with: opclaude-proxy start|stop|restart|status");
