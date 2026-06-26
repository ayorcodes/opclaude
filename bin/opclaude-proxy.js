#!/usr/bin/env node
// Manages the local litellm proxy on Windows.
// Usage: node opclaude-proxy.js start|stop|restart|status|ensure
"use strict";

const { spawn } = require("child_process");
const http  = require("http");
const fs    = require("fs");
const path  = require("path");
const os    = require("os");

const REPO_DIR  = path.resolve(__dirname, "..");
const STATE_DIR = path.join(os.homedir(), ".config", "opclaude");
const ENV_FILE  = path.join(STATE_DIR, ".env");
const PID_FILE  = path.join(STATE_DIR, "proxy.pid");
const LOG_FILE  = path.join(STATE_DIR, "proxy.log");
const PORT      = 4000;
const HOST      = "127.0.0.1";

fs.mkdirSync(STATE_DIR, { recursive: true });

if (!fs.existsSync(ENV_FILE)) {
  console.error(`opclaude is not set up yet. Run install.js from ${REPO_DIR} first.`);
  process.exit(1);
}

// Load env into process.env
fs.readFileSync(ENV_FILE, "utf8").split(/\r?\n/).forEach((line) => {
  const m = line.match(/^([^=]+)=(.*)$/);
  if (m) process.env[m[1]] = m[2];
});

function healthy() {
  return new Promise((resolve) => {
    const req = http.get(
      `http://${HOST}:${PORT}/health/liveliness`,
      { timeout: 2000 },
      (res) => resolve(res.statusCode === 200)
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

function pidAlive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

async function isRunning() {
  if (!fs.existsSync(PID_FILE)) return false;
  const pid = parseInt(fs.readFileSync(PID_FILE, "utf8").trim(), 10);
  if (!pid || !pidAlive(pid)) return false;
  return await healthy();
}

async function start() {
  if (await isRunning()) {
    const pid = fs.readFileSync(PID_FILE, "utf8").trim();
    console.log(`opclaude proxy already running (pid ${pid}).`);
    return;
  }
  fs.rmSync(PID_FILE, { force: true });
  console.log(`Starting opclaude proxy on http://${HOST}:${PORT} ...`);

  const logFd = fs.openSync(LOG_FILE, "a");
  const proc  = spawn("litellm", [
    "--config", path.join(REPO_DIR, "config.yaml"),
    "--host", HOST,
    "--port", String(PORT),
  ], {
    cwd: REPO_DIR,
    detached: true,
    stdio: ["ignore", logFd, logFd],
    env: process.env,
  });
  proc.unref();
  fs.writeFileSync(PID_FILE, String(proc.pid));

  // wait up to 30 s for health check
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    if (await healthy()) {
      console.log(`opclaude proxy is up (pid ${proc.pid}).`);
      return;
    }
  }
  console.error(`opclaude proxy did not come up within 30s. Check ${LOG_FILE} for details.`);
  process.exit(1);
}

function stop() {
  if (!fs.existsSync(PID_FILE)) { console.log("opclaude proxy is not running."); return; }
  const pid = parseInt(fs.readFileSync(PID_FILE, "utf8").trim(), 10);
  if (pid && pidAlive(pid)) {
    try { process.kill(pid); console.log(`Stopped opclaude proxy (pid ${pid}).`); } catch { /* already gone */ }
  }
  fs.rmSync(PID_FILE, { force: true });
}

async function status() {
  if (await isRunning()) {
    const pid = fs.readFileSync(PID_FILE, "utf8").trim();
    console.log(`opclaude proxy is running (pid ${pid}) on http://${HOST}:${PORT}`);
  } else {
    console.log("opclaude proxy is not running.");
  }
}

(async () => {
  switch (process.argv[2]) {
    case "start":   await start(); break;
    case "stop":    stop(); break;
    case "restart": stop(); await new Promise((r) => setTimeout(r, 1000)); await start(); break;
    case "status":  await status(); break;
    case "ensure":  if (!(await isRunning())) await start(); break;
    default:
      console.error("Usage: opclaude-proxy start|stop|restart|status|ensure");
      process.exit(1);
  }
})();
