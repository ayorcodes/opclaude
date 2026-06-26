#!/usr/bin/env node
// Run Claude Code through opencode Zen models via the local litellm proxy.
// Usage: node opclaude.js [models | set-key | enable-ide | disable-ide | <claude args>]
"use strict";

const { execFileSync, spawnSync } = require("child_process");
const crypto = require("crypto");
const fs     = require("fs");
const path   = require("path");
const os     = require("os");
const readline = require("readline");

const REPO_DIR  = path.resolve(__dirname, "..");
const STATE_DIR = path.join(os.homedir(), ".config", "opclaude");
const ENV_FILE  = path.join(STATE_DIR, ".env");
const SETTINGS  = path.join(os.homedir(), ".claude", "settings.json");

function readEnv() {
  const env = {};
  try {
    fs.readFileSync(ENV_FILE, "utf8").split(/\r?\n/).forEach((line) => {
      const m = line.match(/^([^=]+)=(.*)$/);
      if (m) env[m[1]] = m[2];
    });
  } catch { /* not set up yet */ }
  return env;
}

function writeEnv(obj) {
  fs.writeFileSync(ENV_FILE,
    Object.entries(obj).map(([k,v]) => `${k}=${v}`).join("\n") + "\n",
    { mode: 0o600 });
}

const [,, cmd, ...rest] = process.argv;

// --- models ---------------------------------------------------------------
if (cmd === "models") {
  const lines = fs.readFileSync(path.join(REPO_DIR, "config.yaml"), "utf8").split(/\r?\n/);
  let name = "", target = "";
  for (const line of lines) {
    let m;
    if ((m = line.match(/^\s*-\s*model_name:\s*(.+)$/))) {
      if (name) console.log(`  ${name.padEnd(24)} -> ${target || "?"}`);
      name = m[1].trim(); target = "";
    } else if (!target && (m = line.match(/^\s*model:\s*openai\/(.+)$/))) {
      target = m[1].trim();
    }
  }
  if (name) console.log(`  ${name.padEnd(24)} -> ${target || "?"}`);
  console.log("\nRun with: opclaude --model <name>   (default: claude-deepseek-v4-pro)");
  process.exit(0);
}

// --- set-key --------------------------------------------------------------
if (cmd === "set-key") {
  if (!fs.existsSync(ENV_FILE)) {
    console.error("opclaude is not set up yet. Run install.js first."); process.exit(1);
  }
  const env = readEnv();
  const newKey = rest[0] || (() => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    return new Promise((res) => rl.question("Enter your new opencode API key: ", (a) => { rl.close(); res(a); }));
  })();
  Promise.resolve(newKey).then((k) => {
    if (!k) { console.log("No key entered, nothing changed."); process.exit(1); }
    env.OPENCODE_API_KEY = k;
    writeEnv(env);
    console.log(`Updated OPENCODE_API_KEY in ${ENV_FILE}.`);
    spawnSync("node", [path.join(__dirname, "opclaude-proxy.js"), "restart"],
      { stdio: "inherit" });
  });
  return;
}

// --- enable-ide -----------------------------------------------------------
if (cmd === "enable-ide") {
  if (!fs.existsSync(ENV_FILE)) {
    console.error("opclaude is not set up yet. Run install.js first."); process.exit(1);
  }
  const { LITELLM_MASTER_KEY } = readEnv();
  fs.mkdirSync(path.dirname(SETTINGS), { recursive: true });
  let s = {};
  try { s = JSON.parse(fs.readFileSync(SETTINGS, "utf8")); } catch { /* new file */ }
  s.env = s.env || {};
  s.env.ANTHROPIC_BASE_URL   = "http://127.0.0.1:4000";
  s.env.ANTHROPIC_AUTH_TOKEN = LITELLM_MASTER_KEY;
  fs.writeFileSync(SETTINGS, JSON.stringify(s, null, 2) + "\n");
  console.log(`Added ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN to ${SETTINGS}.`);
  spawnSync("node", [path.join(__dirname, "opclaude-proxy.js"), "ensure"], { stdio: "inherit" });
  process.exit(0);
}

// --- disable-ide ----------------------------------------------------------
if (cmd === "disable-ide") {
  if (!fs.existsSync(SETTINGS)) { console.log(`${SETTINGS} does not exist, nothing to do.`); process.exit(0); }
  const s = JSON.parse(fs.readFileSync(SETTINGS, "utf8"));
  if (s.env) {
    delete s.env.ANTHROPIC_BASE_URL;
    delete s.env.ANTHROPIC_AUTH_TOKEN;
    if (!Object.keys(s.env).length) delete s.env;
  }
  fs.writeFileSync(SETTINGS, JSON.stringify(s, null, 2) + "\n");
  console.log(`Removed ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN from ${SETTINGS}.`);
  process.exit(0);
}

// --- guard rails ----------------------------------------------------------
if (!fs.existsSync(ENV_FILE)) {
  console.error("opclaude is not set up yet. Run install.js first."); process.exit(1);
}

const env = readEnv();
Object.assign(process.env, env);

// ensure proxy is running
spawnSync("node", [path.join(__dirname, "opclaude-proxy.js"), "ensure"], { stdio: "inherit" });

const allArgs = [cmd, ...rest].filter(Boolean);
const hasModel = allArgs.some((a) => a === "--model" || a.startsWith("--model="));
const modelArgs = hasModel ? [] : ["--model", "claude-deepseek-v4-pro"];

const result = spawnSync("claude", [...modelArgs, ...allArgs], {
  stdio: "inherit",
  env: {
    ...process.env,
    ANTHROPIC_BASE_URL: "http://127.0.0.1:4000",
    ANTHROPIC_AUTH_TOKEN: env.LITELLM_MASTER_KEY,
    CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY: "1",
  },
});
process.exit(result.status ?? 0);
