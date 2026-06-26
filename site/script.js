(() => {
  "use strict";

  const MODELS = ["kimi", "qwen", "deepseek", "glm", "minimax"];

  const PROMPTS = [
    "fix the off-by-one in the paginator",
    "write tests for the auth middleware",
    "explain this stack trace",
    "refactor src/router.ts to use named exports",
    "summarize the diff on this branch",
    "why is this query so slow?",
  ];

  const HOOK_LOGS = {
    kimi: "routing → moonshot ai",
    qwen: "clamping max_tokens → 8192",
    deepseek: "routing → default model",
    glm: "stripping thinking_blocks",
    minimax: "clamping max_tokens (204800 ctx)",
  };

  const wires = {};
  MODELS.forEach((m, i) => {
    wires[m] = document.getElementById(`wire-${i}`);
  });
  const wireIn = document.getElementById("wire-in");
  const packet = document.getElementById("packet");
  const pipeline = document.getElementById("pipeline");
  const nodeSource = document.getElementById("node-source");
  const nodeProxy = document.getElementById("node-proxy");
  const logLine = document.getElementById("log-line");
  const typedPrompt = document.getElementById("typed-prompt");
  const cycleState = document.getElementById("cycle-state");
  const targets = MODELS.map((m, i) => document.getElementById(`target-${i}`));

  let auto = true;
  let cycleIndex = 0;
  let pinTimer = null;
  let running = false;
  let queuedModel = null;

  function pointAlong(path, t) {
    const len = path.getTotalLength();
    return path.getPointAtLength(len * t);
  }

  function pxFromViewBox(pt) {
    const rect = pipeline.getBoundingClientRect();
    return {
      x: (pt.x / 1000) * rect.width,
      y: (pt.y / 600) * rect.height,
    };
  }

  function movePacketAlong(path, duration) {
    return new Promise((resolve) => {
      const start = performance.now();
      packet.style.opacity = "1";

      function frame(now) {
        const t = Math.min((now - start) / duration, 1);
        const pt = pxFromViewBox(pointAlong(path, t));
        packet.style.transform = `translate(${pt.x - 4.5}px, ${pt.y - 4.5}px)`;
        if (t < 1) {
          requestAnimationFrame(frame);
        } else {
          resolve();
        }
      }
      requestAnimationFrame(frame);
    });
  }

  function typePrompt(text) {
    return new Promise((resolve) => {
      typedPrompt.textContent = "";
      let i = 0;
      const step = () => {
        if (i <= text.length) {
          typedPrompt.textContent = text.slice(0, i);
          i++;
          setTimeout(step, 14);
        } else {
          resolve();
        }
      };
      step();
    });
  }

  function setLog(text, flash) {
    logLine.textContent = text;
    logLine.classList.toggle("flash", !!flash);
  }

  function clearWireStates() {
    Object.values(wires).forEach((w) => w.classList.remove("live"));
    wireIn.classList.remove("live");
  }

  function clearTargetStates() {
    targets.forEach((t) => t.classList.remove("active"));
  }

  async function runCycle(modelName) {
    if (running) return;
    running = true;

    const model = modelName || MODELS[cycleIndex % MODELS.length];
    const targetIndex = MODELS.indexOf(model);
    const targetWire = wires[model];
    const targetBtn = targets[targetIndex];
    const prompt = PROMPTS[Math.floor(Math.random() * PROMPTS.length)];

    clearWireStates();
    clearTargetStates();
    setLog("awaiting request…", false);
    packet.style.opacity = "0";

    nodeSource.classList.add("active");
    await typePrompt(prompt);
    await sleep(120);

    wireIn.classList.add("live");
    await movePacketAlong(wireIn, 650);
    packet.style.opacity = "0";

    nodeSource.classList.remove("active");
    nodeProxy.classList.add("active");
    setLog(HOOK_LOGS[model], true);
    await sleep(550);

    targetWire.classList.add("live");
    await movePacketAlong(targetWire, 750);
    packet.style.opacity = "0";

    nodeProxy.classList.remove("active");
    targetBtn.classList.add("active");
    setLog("response streaming…", false);

    await sleep(1100);
    clearWireStates();
    setLog("awaiting request…", false);

    running = false;
    if (!modelName) cycleIndex++;
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  async function loop() {
    while (true) {
      if (queuedModel) {
        const model = queuedModel;
        queuedModel = null;
        await runCycle(model);
      } else if (auto) {
        await runCycle(null);
      }
      await sleep(900);
    }
  }

  targets.forEach((btn, i) => {
    btn.addEventListener("click", () => {
      auto = false;
      queuedModel = MODELS[i];
      cycleState.textContent = `pinned: ${MODELS[i]}`;
      clearTimeout(pinTimer);
      pinTimer = setTimeout(() => {
        auto = true;
        cycleState.textContent = "auto-cycling";
      }, 6000);
    });
  });

  // --- OS tab switcher ---------------------------------------------------

  const INSTALL = {
    mac: {
      title: "zsh",
      code: "curl -fsSL https://raw.githubusercontent.com/ayorcodes/opclaude/main/get.sh | bash",
      note: `Pulls the repo into <code>~/.opclaude-src</code> and runs the installer — prompts to install <code>uv</code> and Claude Code if missing, then asks for your opencode API key. Needs an opencode <strong>Go</strong> subscription. MIT&nbsp;licensed.`,
    },
    win: {
      title: "cmd",
      code: `curl -L https://raw.githubusercontent.com/ayorcodes/opclaude/main/get.cmd -o "%TEMP%\\opclaude-get.cmd" && "%TEMP%\\opclaude-get.cmd"`,
      note: `Uses <code>get.cmd</code> — a plain batch file, no PowerShell execution policy required. Installs <code>uv</code> and Node.js via <code>winget</code> if missing, then Claude Code and the proxy. Requires Node.js and git. Needs an opencode <strong>Go</strong> subscription. MIT&nbsp;licensed.`,
    },
  };

  const tabMac     = document.getElementById("tab-mac");
  const tabWin     = document.getElementById("tab-win");
  const termTitle  = document.getElementById("term-title");
  const installCode = document.getElementById("install-code");
  const installNote = document.getElementById("install-note");

  function switchOS(os) {
    const cfg = INSTALL[os];
    installCode.textContent = cfg.code;
    termTitle.textContent   = cfg.title;
    installNote.innerHTML   = cfg.note;

    const isWin = os === "win";
    tabMac.classList.toggle("os-tab--active", !isWin);
    tabWin.classList.toggle("os-tab--active",  isWin);
    tabMac.setAttribute("aria-selected", String(!isWin));
    tabWin.setAttribute("aria-selected", String(isWin));
  }

  tabMac.addEventListener("click", () => switchOS("mac"));
  tabWin.addEventListener("click", () => switchOS("win"));

  // auto-select based on visitor's OS
  if (navigator.userAgent.toLowerCase().includes("windows")) {
    switchOS("win");
  }

  // --- copy button -------------------------------------------------------

  const copyBtn = document.getElementById("copy-btn");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const text = installCode.textContent;
      try {
        await navigator.clipboard.writeText(text);
        const original = copyBtn.textContent;
        copyBtn.textContent = "copied";
        setTimeout(() => (copyBtn.textContent = original), 1400);
      } catch (e) {
        /* clipboard unavailable, ignore */
      }
    });
  }

  // kick things off once layout has settled
  window.addEventListener("load", () => {
    setTimeout(loop, 400);
  });
})();
