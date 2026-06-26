#!/usr/bin/env node
// oc (Windows): routes a task to cheap or real Claude. See ../README.md.
"use strict";

const { spawnSync } = require("child_process");
const crypto = require("crypto");
const fs     = require("fs");
const path   = require("path");
const os     = require("os");

const REPO_DIR      = path.resolve(__dirname, "..");
const STATE_DIR     = path.join(os.homedir(), ".config", "opclaude");
const ENV_FILE      = path.join(STATE_DIR, ".env");
const ROUTER_CONFIG = path.join(STATE_DIR, "router.yaml");
const LAST_SESSION  = path.join(STATE_DIR, "last_session");

if (!fs.existsSync(ENV_FILE)) {
  console.error("opclaude is not set up yet. Run install.js first."); process.exit(1);
}

const env = {};
fs.readFileSync(ENV_FILE, "utf8").split(/\r?\n/).forEach((line) => {
  const m = line.match(/^([^=]+)=(.*)$/);
  if (m) env[m[1]] = m[2];
});
Object.assign(process.env, env);

function classify(task) {
  const r = spawnSync("uv", [
    "run", "python", path.join(__dirname, "oc-classify"),
    "--task", task,
    "--router-config", ROUTER_CONFIG,
  ], { stdio: "pipe", encoding: "utf8", env: process.env });
  if (r.status !== 0) {
    console.error(r.stderr || "oc-classify failed"); process.exit(1);
  }
  return JSON.parse(r.stdout.trim());
}

function proxyEnv() {
  return {
    ...process.env,
    ANTHROPIC_BASE_URL: "http://127.0.0.1:4000",
    ANTHROPIC_AUTH_TOKEN: env.LITELLM_MASTER_KEY,
    CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY: "1",
  };
}

function ensureProxy() {
  spawnSync("node", [path.join(__dirname, "opclaude-proxy.js"), "ensure"],
    { stdio: "inherit", env: process.env });
}

const [,, subcmd, ...rest] = process.argv;

// --- classify -------------------------------------------------------------
if (subcmd === "classify") {
  const task = rest.join(" ");
  if (!task) { console.error('Usage: oc classify "<task>"'); process.exit(1); }
  const r = spawnSync("uv", [
    "run", "python", path.join(__dirname, "oc-classify"),
    "--task", task, "--router-config", ROUTER_CONFIG,
  ], { stdio: "inherit", env: process.env });
  process.exit(r.status ?? 0);
}

// --- --escalate -----------------------------------------------------------
if (subcmd === "--escalate") {
  if (!fs.existsSync(LAST_SESSION)) {
    console.error("No previous oc session found. Run 'oc \"<task>\"' first."); process.exit(1);
  }
  const vars = {};
  fs.readFileSync(LAST_SESSION, "utf8").split(/\r?\n/).forEach((l) => {
    const m = l.match(/^([^=]+)=(.*)$/); if (m) vars[m[1]] = m[2];
  });
  const extra = rest.join(" ");
  process.stderr.write(`oc: escalating session ${vars.SESSION_ID} to real Claude (Pro).\n`);
  const args = extra
    ? ["--resume", vars.SESSION_ID, "--fork-session", extra]
    : ["--resume", vars.SESSION_ID, "--fork-session"];
  const r = spawnSync("claude", args, { stdio: "inherit", shell: process.platform === "win32", env: process.env });
  process.exit(r.status ?? 0);
}

// --- --cheap --------------------------------------------------------------
if (subcmd === "--cheap" || subcmd === "--downgrade") {
  ensureProxy();
  const extra = rest.join(" ");
  process.stderr.write("oc: continuing most recent session on the cheap proxy (auto-routed).\n");
  const args = extra
    ? ["--continue", "--fork-session", "--model", "claude-auto", extra]
    : ["--continue", "--fork-session", "--model", "claude-auto"];
  const r = spawnSync("claude", args, { stdio: "inherit", shell: process.platform === "win32", env: proxyEnv() });
  process.exit(r.status ?? 0);
}

// --- main routing ---------------------------------------------------------
const TASK = [subcmd, ...rest].filter(Boolean).join(" ");
if (!TASK) {
  console.log('Usage: oc "<task description>"');
  console.log('       oc classify "<task description>"');
  console.log("       oc --escalate [extra context]");
  console.log("       oc --cheap [extra context]");
  process.exit(1);
}

const decision = classify(TASK);
const { tier, model } = decision;

if (tier === "critical") {
  process.stderr.write(`oc: routing to real Claude --model ${model} (critical reasoning task).\n`);
  const r = spawnSync("claude", ["--model", model, ...process.argv.slice(2)],
    { stdio: "inherit", shell: process.platform === "win32", env: process.env });
  process.exit(r.status ?? 0);
}

ensureProxy();

const SESSION_ID = crypto.randomUUID().toUpperCase();
fs.writeFileSync(LAST_SESSION,
  `SESSION_ID=${SESSION_ID}\nTASK=${TASK}\nMODEL=${model}\nTIER=${tier}\nCWD=${process.cwd()}\nTIMESTAMP=${new Date().toISOString()}\n`);

process.stderr.write(`oc: auto-routing this session via opclaude proxy (first turn: ${tier} -> ${model}).\n`);
const r = spawnSync("claude", ["--model", "claude-auto", "--session-id", SESSION_ID, ...process.argv.slice(2)],
  { stdio: "inherit", shell: process.platform === "win32", env: proxyEnv() });
process.exit(r.status ?? 0);
