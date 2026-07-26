(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const MIN_ORDER_USD = 1.0; // MEXC-style minimum notional per resting order
  const MAX_STEPS_SLIDER = 30; // simple-mode slider only
  const MAX_STEPS_EXPERT = 100000; // soft UI safety; real cap is $1/order budget
  const MAX_DEPTH_SLIDER = 15; // simple-mode slider
  const MAX_DEPTH_EXPERT = 10000; // 10000% ≈ 101× mid on the sell side

  const state = {
    bot_id: "",
    bots: [],
    running: false,
    paper: true,
    setup_complete: false,
    mid: 0,
    desired: [],
    open_orders: [],
    public_depth: {},
    bot_contribution: {},
    dirty: false,
    hasKeys: false,
    wizStep: 0,
    wizMode: "paper", // paper | live
    formSeeded: false, // after first force-apply, never stomp sliders from poll
    public_depth_ladder: [],
    // per-bot UI form seed flags
    formSeededByBot: {},
  };

  const wizTitles = [
    "Welcome",
    "Paper or live?",
    "MEXC API keys",
    "Capital & depth",
    "Confirm setup",
  ];

  // ── API ───────────────────────────────────────────────────────────────
  async function post(url, body) {
    const payload = Object.assign({ bot_id: state.bot_id }, body || {});
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return r.json();
  }

  function withBot(url) {
    if (!state.bot_id) return url;
    const sep = url.includes("?") ? "&" : "?";
    return url + sep + "bot_id=" + encodeURIComponent(state.bot_id);
  }

  function renderBotTabs() {
    const host = $("botTabs");
    if (!host) return;
    const bots = state.bots || [];
    host.innerHTML = bots
      .map((b) => {
        const active = b.id === state.bot_id ? " active" : "";
        const run = b.running ? " run" : "";
        const label = (b.name || "Bot").replace(/</g, "&lt;");
        return (
          '<div class="bot-tab' +
          active +
          '" data-bot="' +
          b.id +
          '" role="tab" title="' +
          label +
          '">' +
          '<span class="tab-run' +
          run +
          '"></span>' +
          '<span class="tab-label">' +
          label +
          "</span>" +
          (bots.length > 1
            ? '<button type="button" class="tab-x" data-close="' +
              b.id +
              '" title="Close bot">×</button>'
            : "") +
          "</div>"
        );
      })
      .join("");

    host.querySelectorAll(".bot-tab").forEach((el) => {
      el.onclick = (e) => {
        if (e.target.closest(".tab-x")) return;
        const id = el.getAttribute("data-bot");
        if (id && id !== state.bot_id) switchBot(id);
      };
    });
    host.querySelectorAll(".tab-x").forEach((el) => {
      el.onclick = async (e) => {
        e.stopPropagation();
        const id = el.getAttribute("data-close");
        if (!id) return;
        if (
          !confirm(
            "Close this bot tab? Its open orders will be canceled. (Cancel the dialog to keep the tab.)"
          )
        )
          return;
        const res = await post("/api/bots/delete", {
          bot_id: id,
          cancel_orders: true,
        });
        if (res.bots) state.bots = res.bots;
        if (state.bot_id === id && state.bots.length) {
          state.bot_id = state.bots[0].id;
          state.formSeeded = false;
          delete state.formSeededByBot[id];
        }
        renderBotTabs();
        await refreshMarket();
      };
    });
  }

  async function switchBot(id) {
    // Save current bot config before switch if dirty
    if (state.dirty && state.bot_id) {
      try {
        await post("/api/config", marketConfigBody());
      } catch (_) {}
      state.dirty = false;
    }
    state.bot_id = id;
    state.formSeeded = !!state.formSeededByBot[id];
    renderBotTabs();
    await refreshMarket(true);
  }

  if ($("btnNewBot")) {
    $("btnNewBot").onclick = async () => {
      if (state.dirty && state.bot_id) {
        try {
          await post("/api/config", marketConfigBody());
        } catch (_) {}
      }
      const res = await post("/api/bots", {
        name: "Bot " + ((state.bots || []).length + 1),
        clone_from: state.bot_id || null,
      });
      if (res.bot_id) {
        state.bots = res.bots || [];
        state.bot_id = res.bot_id;
        state.formSeeded = false;
        renderBotTabs();
        await refreshMarket(true);
      }
    };
  }

  function isExpert() {
    const el = $("expertMode");
    return !!(el && el.checked);
  }

  function currentDepth() {
    if (isExpert() && $("depthCustom")) {
      let d = Number($("depthCustom").value);
      if (!Number.isFinite(d)) d = Number($("depth").value) || 2;
      d = Math.min(MAX_DEPTH_EXPERT, Math.max(0.1, d));
      return d;
    }
    let d = Number($("depth").value) || 2;
    return Math.min(MAX_DEPTH_SLIDER, Math.max(0.5, d));
  }

  /** Max steps funded by ≥$1/order (no hard 30 in expert). */
  function budgetMaxSteps() {
    const u = Number(usdtNum.value) || 0;
    const l = Number(lbcNum.value) || 0;
    const mid = state.mid || 0;
    return Math.max(maxStepsForUsdt(u), maxStepsForLbc(l, mid), 0);
  }

  function currentSteps() {
    if (isExpert() && $("levelsCustom")) {
      let n = parseInt($("levelsCustom").value, 10);
      if (!Number.isFinite(n)) n = Number($("levels").value) || 4;
      n = Math.max(1, n);
      const cap = budgetMaxSteps();
      // Only clamp to budget when we know capital; if both sides $0, leave UI value
      if (cap > 0 && n > cap) n = cap;
      if (n > MAX_STEPS_EXPERT) n = MAX_STEPS_EXPERT;
      return n;
    }
    let n = Number($("levels").value) || 4;
    if (n > MAX_STEPS_SLIDER) n = MAX_STEPS_SLIDER;
    if (n < 1) n = 1;
    return n;
  }

  function marketConfigBody() {
    const d = currentDepth();
    const n = currentSteps();
    return {
      usdt_budget: Number($("usdtNum").value) || 0,
      lbc_budget: Number($("lbcNum").value) || 0,
      bid_depth_pct: d,
      ask_depth_pct: d,
      n_levels: n,
      min_notional_usdt: MIN_ORDER_USD,
      strategy: "depth_provider", // locked until other strategies are tested
      paper: state.paper,
    };
  }

  // ── Views / menu ──────────────────────────────────────────────────────
  function showView(name) {
    ["market", "status", "settings"].forEach((v) => {
      const el = $("view-" + v);
      if (el) el.hidden = v !== name;
    });
    document.querySelectorAll(".menu-item").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.view === name);
    });
  }

  document.querySelectorAll(".menu-item").forEach((btn) => {
    btn.onclick = () => showView(btn.dataset.view);
  });

  // ── Wizard ────────────────────────────────────────────────────────────
  function seedWizardFromState() {
    // One-shot seed when opening the wizard — never re-applied while typing
    $("wizUsdt").value = Number($("usdtNum").value) || 10;
    $("wizLbc").value = Number($("lbcNum").value) || 0;
    const d = Number($("depth").value) || 2;
    $("wizDepth").value = d;
    $("wizDepthOut").textContent = "±" + d.toFixed(1) + "%";
    const n = Number($("levels").value) || 4;
    $("wizLevels").value = n;
    $("wizLevelsOut").textContent = String(n);
  }

  function openWizard(reset) {
    $("wizard").hidden = false;
    $("app").hidden = true;
    if (reset) {
      state.wizStep = 0;
      state.wizMode = state.paper ? "paper" : "live";
      seedWizardFromState();
    }
    renderWizard();
  }

  function closeWizardToApp() {
    $("wizard").hidden = true;
    $("app").hidden = false;
    updateSetupBanner();
    refreshMarket();
  }

  function wizardSteps() {
    // Skip keys step (2) when paper
    const all = [0, 1, 2, 3, 4];
    if (state.wizMode === "paper") return all.filter((s) => s !== 2);
    return all;
  }

  function renderWizard() {
    const steps = wizardSteps();
    if (!steps.includes(state.wizStep)) state.wizStep = steps[0];
    const idx = steps.indexOf(state.wizStep);

    $("wizTitle").textContent = wizTitles[state.wizStep] || "Setup";
    document.querySelectorAll(".wiz-pane").forEach((p) => {
      p.hidden = Number(p.dataset.pane) !== state.wizStep;
    });

    // progress dots map to logical sequence
    document.querySelectorAll(".wiz-step-dot").forEach((dot, i) => {
      dot.classList.toggle("active", i === idx);
      dot.classList.toggle("done", i < idx);
      // hide extra dots when paper (4 steps not 5)
      if (state.wizMode === "paper") {
        dot.hidden = i >= 4;
      } else {
        dot.hidden = false;
      }
    });

    $("wizBack").hidden = idx <= 0;
    const isLast = idx >= steps.length - 1;
    $("wizNext").textContent = isLast ? "Finish setup" : "Continue →";

    // mode cards
    $("wizPaper").classList.toggle("selected", state.wizMode === "paper");
    $("wizLive").classList.toggle("selected", state.wizMode === "live");

    // confirm summary
    if (state.wizStep === 4) {
      const depth = Number($("wizDepth").value);
      const rows = [
        ["Mode", state.wizMode === "paper" ? "Paper (simulated)" : "LIVE (real money)"],
        ["USDT (buy side)", "$" + (Number($("wizUsdt").value) || 0)],
        ["LBC (sell side)", String(Number($("wizLbc").value) || 0)],
        ["Depth %", "±" + depth.toFixed(1) + "%"],
        ["Steps", String(Number($("wizLevels").value) || 4)],
      ];
      if (state.wizMode === "live") {
        rows.push(["Access Key", $("wizAccessKey").value ? "provided" : "env / missing"]);
        rows.push(["Secret Key", $("wizSecretKey").value ? "provided" : "env / missing"]);
      }
      $("wizSummary").innerHTML = rows
        .map(([k, v]) => "<div><dt>" + k + "</dt><dd>" + v + "</dd></div>")
        .join("");
      $("wizLiveConfirmRow").hidden = state.wizMode !== "live";
    }
  }

  $("wizPaper").onclick = () => {
    state.wizMode = "paper";
    renderWizard();
  };
  $("wizLive").onclick = () => {
    state.wizMode = "live";
    renderWizard();
  };

  $("wizDepth").oninput = () => {
    $("wizDepthOut").textContent = "±" + Number($("wizDepth").value).toFixed(1) + "%";
  };
  $("wizLevels").oninput = () => {
    $("wizLevelsOut").textContent = $("wizLevels").value;
  };
  $("wizDepth").dispatchEvent(new Event("input"));
  $("wizLevels").dispatchEvent(new Event("input"));

  $("wizBack").onclick = () => {
    const steps = wizardSteps();
    const idx = steps.indexOf(state.wizStep);
    if (idx > 0) {
      state.wizStep = steps[idx - 1];
      renderWizard();
    }
  };

  $("wizNext").onclick = async () => {
    const steps = wizardSteps();
    const idx = steps.indexOf(state.wizStep);
    // validate keys step
    if (state.wizStep === 2 && state.wizMode === "live") {
      // keys optional if env may have them — warn only
    }
    if (state.wizStep === 4 && state.wizMode === "live" && !$("wizLiveCheck").checked) {
      alert("Please confirm you understand live trading risks.");
      return;
    }
    if (idx >= steps.length - 1) {
      await finishWizard();
      return;
    }
    state.wizStep = steps[idx + 1];
    renderWizard();
  };

  async function finishWizard() {
    const depth = Number($("wizDepth").value);
    const body = {
      setup_complete: true,
      paper: state.wizMode === "paper",
      live_confirm: state.wizMode === "live",
      usdt_budget: Number($("wizUsdt").value) || 0,
      lbc_budget: Number($("wizLbc").value) || 0,
      bid_depth_pct: depth,
      ask_depth_pct: depth,
      n_levels: Number($("wizLevels").value) || 4,
      strategy: "depth_provider", // locked
    };
    if ($("wizAccessKey").value) body.mexc_api_key = $("wizAccessKey").value.trim();
    if ($("wizSecretKey").value) body.mexc_api_secret = $("wizSecretKey").value.trim();

    const res = await post("/api/config", body);
    if (res.config) applyConfigFromServer(res.config, true);
    state.setup_complete = true;
    state.paper = state.wizMode === "paper" || (res.config && res.config.paper !== false);
    closeWizardToApp();
  }

  $("btnOpenSetup").onclick = () => openWizard(true);
  $("btnRerunSetup").onclick = () => openWizard(true);

  // ── Setup banner ──────────────────────────────────────────────────────
  function updateSetupBanner() {
    const need = !state.setup_complete;
    $("setupBanner").hidden = !need;
    $("btnStart").disabled = need;
    if (need) {
      $("ctaNote").textContent = "Finish first-time setup before starting the bot.";
    }
  }

  // ── Market config inputs ──────────────────────────────────────────────
  const usdt = $("usdt");
  const usdtNum = $("usdtNum");
  const lbc = $("lbc");
  const lbcNum = $("lbcNum");
  const depth = $("depth");
  const levels = $("levels");

  function maxStepsForUsdt(usdtAmt) {
    if (usdtAmt < MIN_ORDER_USD) return 0;
    return Math.floor(usdtAmt / MIN_ORDER_USD);
  }
  function maxStepsForLbc(lbcAmt, mid) {
    if (!mid || mid <= 0 || lbcAmt <= 0) return 0;
    const usdtEq = lbcAmt * mid;
    if (usdtEq < MIN_ORDER_USD) return 0;
    return Math.floor(usdtEq / MIN_ORDER_USD);
  }

  /** Cap Steps so each side can fund ≥ $1/order; show a clear hint. */
  function enforceOrderLimits() {
    const u = Number(usdtNum.value) || 0;
    const l = Number(lbcNum.value) || 0;
    const mid = state.mid || 0;
    const maxBuy = maxStepsForUsdt(u);
    const maxSell = maxStepsForLbc(l, mid);
    const budgetCap = Math.max(maxBuy, maxSell, 0);
    const expert = isExpert();

    // Simple slider stays 1–30; expert custom steps free of the 30 hard cap
    levels.max = String(MAX_STEPS_SLIDER);
    levels.min = "1";
    if ($("levelsCustom")) {
      $("levelsCustom").min = "1";
      $("levelsCustom").max = String(
        expert && budgetCap > 0 ? budgetCap : MAX_STEPS_EXPERT
      );
    }

    let n = currentSteps();
    if (!expert && n > MAX_STEPS_SLIDER) n = MAX_STEPS_SLIDER;
    if (expert && budgetCap > 0 && n > budgetCap) n = budgetCap;
    if (n < 1) n = 1;

    // Keep slider display in range even if expert uses higher custom steps
    levels.value = String(Math.min(MAX_STEPS_SLIDER, n));
    if ($("levelsCustom") && expert) $("levelsCustom").value = String(n);
    if ($("levelsCustom") && !expert) $("levelsCustom").value = String(n);
    $("levelsOut").textContent = String(n);

    const hint = $("limitsHint");
    if (!hint) return;
    const parts = [];
    if (expert) {
      parts.push(
        "Expert: steps only limited by ≥$" +
          MIN_ORDER_USD.toFixed(0) +
          "/order (not 30)."
      );
    }
    if (u > 0 && u < MIN_ORDER_USD) {
      parts.push(
        "Buy side: need at least $" +
          MIN_ORDER_USD.toFixed(0) +
          " USDT — $0 / dust places no buy orders."
      );
    } else if (u >= MIN_ORDER_USD && n > maxBuy) {
      parts.push(
        "Buy side: with $" +
          u.toFixed(0) +
          " USDT, max " +
          maxBuy +
          " step(s) at ≥$" +
          MIN_ORDER_USD.toFixed(0) +
          "/order (extra steps ignored on buys)."
      );
    } else if (u >= MIN_ORDER_USD) {
      parts.push(
        "Buy: up to " + maxBuy + " order(s) of ≥$" + MIN_ORDER_USD.toFixed(0) + "."
      );
    } else {
      parts.push("Buy side off ($0 USDT).");
    }

    if (l > 0 && mid > 0 && l * mid < MIN_ORDER_USD) {
      parts.push(
        "Sell side: LBC notional < $" +
          MIN_ORDER_USD.toFixed(0) +
          " at mid — no sell orders."
      );
    } else if (l > 0 && mid > 0 && n > maxSell) {
      parts.push(
        "Sell side: with this LBC, max " +
          maxSell +
          " step(s) at ≥$" +
          MIN_ORDER_USD.toFixed(0) +
          "/order."
      );
    } else if (l > 0 && mid > 0) {
      parts.push(
        "Sell: up to " + maxSell + " order(s) of ≥$" + MIN_ORDER_USD.toFixed(0) + "."
      );
    } else if (l <= 0) {
      parts.push("Sell side off (0 LBC).");
    }

    if (expert) {
      const d = currentDepth();
      const sellMult = 1 + d / 100;
      parts.push(
        "Depth ±" +
          d.toFixed(1) +
          "% → outermost sell ≈ " +
          sellMult.toFixed(2) +
          "× mid" +
          (mid > 0 ? " (~" + (mid * sellMult).toPrecision(4) + ")" : "") +
          "."
      );
    }

    hint.textContent = parts.join(" ");
    const noOrders =
      u < MIN_ORDER_USD && (l <= 0 || !mid || l * mid < MIN_ORDER_USD);
    const waste =
      (u >= MIN_ORDER_USD && n > maxBuy) || (l > 0 && mid > 0 && n > maxSell);
    hint.className = "hint" + (noOrders ? " err" : waste ? " warn" : "");
  }

  function syncUsdt(fromRange) {
    if (fromRange) usdtNum.value = usdt.value;
    else {
      const v = Math.max(0, Number(usdtNum.value) || 0);
      usdtNum.value = v;
      if (v > Number(usdt.max)) usdt.max = Math.ceil(v);
      usdt.value = Math.min(v, Number(usdt.max));
    }
    state.dirty = true;
    enforceOrderLimits();
    schedulePreview();
  }
  function syncLbc(fromRange) {
    if (fromRange) lbcNum.value = lbc.value;
    else {
      const v = Math.max(0, Number(lbcNum.value) || 0);
      lbcNum.value = v;
      if (v > Number(lbc.max)) lbc.max = Math.ceil(v);
      lbc.value = Math.min(v, Number(lbc.max));
    }
    state.dirty = true;
    enforceOrderLimits();
    schedulePreview();
  }
  function applyDepthValue(v, fromCustom) {
    v = Number(v);
    if (!Number.isFinite(v)) v = 2;
    const maxD = isExpert() ? MAX_DEPTH_EXPERT : MAX_DEPTH_SLIDER;
    const minD = isExpert() ? 0.1 : 0.5;
    v = Math.min(maxD, Math.max(minD, v));
    // Slider only goes to 15 — keep full expert value in custom field
    const sliderV = Math.min(MAX_DEPTH_SLIDER, Math.max(0.5, v));
    depth.value = String(sliderV);
    if ($("depthCustom")) $("depthCustom").value = String(v);
    $("depthOut").textContent = "±" + v.toFixed(1) + "%";
    $("bidDepth").value = v;
    $("askDepth").value = v;
    document.querySelectorAll("[data-depth]").forEach((el) => {
      el.classList.toggle("active", Math.abs(Number(el.dataset.depth) - v) < 0.05);
    });
    state.dirty = true;
    enforceOrderLimits();
    schedulePreview();
  }

  function applyStepsValue(n, fromCustom) {
    n = parseInt(n, 10);
    if (!Number.isFinite(n)) n = 1;
    n = Math.max(1, n);
    if (isExpert()) {
      const cap = budgetMaxSteps();
      if (cap > 0 && n > cap) n = cap;
      if (n > MAX_STEPS_EXPERT) n = MAX_STEPS_EXPERT;
    } else if (n > MAX_STEPS_SLIDER) {
      n = MAX_STEPS_SLIDER;
    }
    levels.value = String(Math.min(MAX_STEPS_SLIDER, n));
    if ($("levelsCustom")) $("levelsCustom").value = String(n);
    $("levelsOut").textContent = String(n);
    state.dirty = true;
    enforceOrderLimits();
    schedulePreview();
  }

  function syncDepth() {
    applyDepthValue(depth.value, false);
  }

  function setExpertMode(on) {
    const exp = $("expertMode");
    if (exp) exp.checked = !!on;
    const dWrap = $("depthCustomWrap");
    const lWrap = $("levelsCustomWrap");
    if (dWrap) dWrap.hidden = !on;
    if (lWrap) lWrap.hidden = !on;
    if (on) {
      // Seed custom fields from current slider values
      if ($("depthCustom")) $("depthCustom").value = String(currentDepth());
      if ($("levelsCustom")) $("levelsCustom").value = String(currentSteps());
    }
  }

  // Must be declared before any sync*()/schedulePreview() calls (let TDZ crash → blank page)
  let previewTimer = null;
  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(refreshMarket, 280);
  }

  usdt.addEventListener("input", () => syncUsdt(true));
  usdtNum.addEventListener("input", () => syncUsdt(false));
  lbc.addEventListener("input", () => syncLbc(true));
  lbcNum.addEventListener("input", () => syncLbc(false));
  depth.addEventListener("input", syncDepth);
  levels.addEventListener("input", () => {
    applyStepsValue(levels.value, false);
  });

  if ($("expertMode")) {
    $("expertMode").addEventListener("change", () => {
      setExpertMode($("expertMode").checked);
      state.dirty = true;
      schedulePreview();
    });
  }
  if ($("depthCustom")) {
    $("depthCustom").addEventListener("input", () => {
      if (!isExpert()) return;
      applyDepthValue($("depthCustom").value, true);
    });
    $("depthCustom").addEventListener("change", () => {
      if (!isExpert()) return;
      applyDepthValue($("depthCustom").value, true);
    });
  }
  if ($("levelsCustom")) {
    $("levelsCustom").addEventListener("input", () => {
      if (!isExpert()) return;
      applyStepsValue($("levelsCustom").value, true);
    });
    $("levelsCustom").addEventListener("change", () => {
      if (!isExpert()) return;
      applyStepsValue($("levelsCustom").value, true);
    });
  }

  // Strategy select is locked (disabled) to depth_provider.

  // ── Public depth ladder panel ─────────────────────────────────────────
  function openDepthPanel() {
    const panel = $("depthPanel");
    if (!panel) return;
    panel.hidden = false;
    renderDepthLadder();
  }
  function closeDepthPanel() {
    const panel = $("depthPanel");
    if (panel) panel.hidden = true;
  }
  function renderDepthLadder() {
    const body = $("depthLadderBody");
    const meta = $("depthPanelMeta");
    if (!body) return;
    const mid = state.mid;
    if (meta) {
      meta.textContent = mid
        ? "mid " + mid.toFixed(6) + " · " + (state.symbol || "LBCUSDT")
        : "Waiting for market…";
    }
    const ladder = state.public_depth_ladder || [];
    if (!ladder.length) {
      body.innerHTML =
        '<tr><td colspan="4" class="muted">No ladder data yet — wait for the next market tick.</td></tr>';
      return;
    }
    body.innerHTML = ladder
      .map((row) => {
        const pct = Number(row.pct);
        const bid = Number(row.bid_usd) || 0;
        const ask = Number(row.ask_usd) || 0;
        const total = bid + ask;
        const goal = Math.abs(pct - 2) < 0.01;
        return (
          '<tr class="' +
          (goal ? "is-goal" : "") +
          '">' +
          "<td>" +
          (goal ? "±" + pct + "% (goal)" : "±" + pct + "%") +
          "</td>" +
          '<td class="mono bid">' +
          money(bid) +
          "</td>" +
          '<td class="mono ask">' +
          money(ask) +
          "</td>" +
          '<td class="mono">' +
          money(total) +
          "</td>" +
          "</tr>"
        );
      })
      .join("");
  }
  if ($("btnDepthExpand")) $("btnDepthExpand").onclick = openDepthPanel;
  if ($("btnDepthExpand2")) $("btnDepthExpand2").onclick = openDepthPanel;
  if ($("depthPanelClose")) $("depthPanelClose").onclick = closeDepthPanel;
  if ($("depthPanel")) {
    $("depthPanel").addEventListener("click", (e) => {
      if (e.target === $("depthPanel")) closeDepthPanel();
    });
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDepthPanel();
  });

  document.querySelectorAll("[data-usdt]").forEach((el) => {
    el.onclick = () => {
      if (Number(el.dataset.usdt) > Number(usdt.max)) usdt.max = el.dataset.usdt;
      usdt.value = el.dataset.usdt;
      syncUsdt(true);
    };
  });
  document.querySelectorAll("[data-lbc]").forEach((el) => {
    el.onclick = () => {
      if (Number(el.dataset.lbc) > Number(lbc.max)) lbc.max = el.dataset.lbc;
      lbc.value = el.dataset.lbc;
      syncLbc(true);
    };
  });
  document.querySelectorAll("[data-depth]").forEach((el) => {
    el.onclick = () => {
      applyDepthValue(el.dataset.depth, false);
    };
  });

  levels.max = String(MAX_STEPS_SLIDER);
  setExpertMode(false);
  syncUsdt(true);
  syncLbc(true);
  syncDepth();
  enforceOrderLimits();

  // ── Format / draw ─────────────────────────────────────────────────────
  function money(v) {
    if (!Number.isFinite(Number(v))) return "—";
    const n = Number(v);
    if (n >= 100) return "$" + n.toFixed(0);
    if (n >= 10) return "$" + n.toFixed(1);
    return "$" + n.toFixed(2);
  }
  function pctOfGoal(usd) {
    if (!Number.isFinite(usd)) return 0;
    return Math.min(100, (usd / 100) * 100);
  }

  function draw() {
    const c = $("diagram");
    if (!c) return;
    const ctx = c.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const cssW = c.clientWidth || 720;
    const cssH = 360;
    if (c.width !== Math.floor(cssW * dpr)) {
      c.width = Math.floor(cssW * dpr);
      c.height = Math.floor(cssH * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = cssW;
    const h = cssH;
    ctx.clearRect(0, 0, w, h);

    const mid = state.mid || 0;
    if (!mid) {
      ctx.fillStyle = "#8fa89a";
      ctx.font = "500 14px DM Sans, sans-serif";
      ctx.fillText("Waiting for live MEXC market data…", 28, h / 2);
      return;
    }

    const bidPct = currentDepth() / 100;
    const pad = Math.max(bidPct, 0.02) * 1.35;
    const lo = mid * (1 - pad);
    const hi = mid * (1 + pad);
    const xOf = (p) => ((p - lo) / (hi - lo)) * (w - 48) + 24;
    const midX = xOf(mid);
    const bandL = xOf(mid * (1 - bidPct));
    const bandR = xOf(mid * (1 + bidPct));
    const bandTop = 48;
    const bandH = h - 96;

    ctx.strokeStyle = "rgba(255,255,255,0.04)";
    for (let i = 1; i < 6; i++) {
      const y = (h / 6) * i;
      ctx.beginPath();
      ctx.moveTo(16, y);
      ctx.lineTo(w - 16, y);
      ctx.stroke();
    }

    const bidGrad = ctx.createLinearGradient(bandL, 0, midX, 0);
    bidGrad.addColorStop(0, "rgba(61,219,132,0.05)");
    bidGrad.addColorStop(1, "rgba(61,219,132,0.22)");
    ctx.fillStyle = bidGrad;
    ctx.fillRect(bandL, bandTop, midX - bandL, bandH);

    const askGrad = ctx.createLinearGradient(midX, 0, bandR, 0);
    askGrad.addColorStop(0, "rgba(255,155,106,0.22)");
    askGrad.addColorStop(1, "rgba(255,155,106,0.05)");
    ctx.fillStyle = askGrad;
    ctx.fillRect(midX, bandTop, bandR - midX, bandH);

    ctx.strokeStyle = "rgba(255,255,255,0.55)";
    ctx.setLineDash([5, 5]);
    ctx.beginPath();
    ctx.moveTo(midX, 28);
    ctx.lineTo(midX, h - 28);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = "#f3f7f5";
    ctx.font = "600 12px JetBrains Mono, monospace";
    const midLabel = mid.toFixed(6);
    ctx.fillText(midLabel, Math.min(w - 100, Math.max(20, midX + 8)), 22);

    ctx.fillStyle = "#8fa89a";
    ctx.font = "500 11px DM Sans, sans-serif";
    const dShow = currentDepth();
    ctx.fillText("BUY  −" + dShow.toFixed(1) + "%", bandL, h - 14);
    const askLab = "SELL  +" + dShow.toFixed(1) + "%";
    ctx.fillText(askLab, bandR - ctx.measureText(askLab).width, h - 14);

    const orders =
      state.open_orders && state.open_orders.length
        ? state.open_orders
        : state.desired || [];
    const maxUsdt = Math.max(1, ...orders.map((o) => Number(o.usdt) || 1));
    orders.forEach((o) => {
      const x = xOf(Number(o.price));
      const buy = String(o.side).toUpperCase() === "BUY";
      const size = 6 + 10 * ((Number(o.usdt) || 1) / maxUsdt);
      const y = buy ? h * 0.62 : h * 0.38;
      ctx.beginPath();
      ctx.fillStyle = buy ? "rgba(61,219,132,0.2)" : "rgba(255,155,106,0.2)";
      ctx.arc(x, y, size + 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.fillStyle = buy ? "#3ddb84" : "#ff9b6a";
      ctx.arc(x, y, size, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  function render() {
    const mid = state.mid;
    $("mid").textContent = mid ? mid.toFixed(6) : "—";
    const bb = state.best_bid;
    const ba = state.best_ask;
    if (bb && ba && mid) {
      const spr = ba - bb;
      $("spread").textContent =
        spr.toFixed(6) + "  (" + ((spr / mid) * 10000).toFixed(1) + " bps)";
    } else $("spread").textContent = "—";

    const pub = state.public_depth || {};
    const bot = state.bot_contribution || {};
      $("pubBid").textContent = money(pub.bid_usd);
    $("pubAsk").textContent = money(pub.ask_usd);
    renderDepthLadder();
    $("botBid").textContent = money(bot.bid_usd);
    $("botAsk").textContent = money(bot.ask_usd);
    $("goalBidLbl").textContent = money(pub.bid_usd) + " / $100";
    $("goalAskLbl").textContent = money(pub.ask_usd) + " / $100";
    $("goalBidFill").style.width = pctOfGoal(pub.bid_usd) + "%";
    $("goalAskFill").style.width = pctOfGoal(pub.ask_usd) + "%";

    if (mid) {
      $("connPill").classList.add("live-conn");
      $("connPill").classList.remove("err-conn");
      $("connLabel").textContent = "Live MEXC";
    }

    if (state.paper) {
      $("modePill").className = "pill mode-pill paper";
      $("modeLabel").textContent = "Paper mode";
    } else {
      $("modePill").className = "pill mode-pill live";
      $("modeLabel").textContent = "LIVE trading";
    }
    const ctaMain = $("ctaMain");
    if (ctaMain) ctaMain.textContent = "Start bot";

    const running = state.running;
    $("app").classList.toggle("is-running", running);
    $("btnStart").classList.toggle("hidden", running);
    if ($("stopStack")) $("stopStack").classList.toggle("hidden", !running);
    if ($("btnStop")) $("btnStop").classList.toggle("hidden", !running);
    $("btnStart").disabled = !state.setup_complete || running;

    if (running) {
      $("runBadge").className = "run-badge running";
      $("runLabel").textContent = "Running";
      $("statusLine").textContent = state.status_msg || "Bot is resting maker orders…";
      $("ctaNote").textContent = "Bot is active. Stop anytime to cancel its orders.";
    } else {
      $("runBadge").className = "run-badge stopped";
      $("runLabel").textContent = "Stopped";
      $("statusLine").textContent = !state.setup_complete
        ? "Complete first-time setup to enable Start"
        : mid
          ? "Ready — adjust capital, then Start"
          : "Waiting for market…";
      $("ctaNote").textContent = state.setup_complete
        ? "You can change amounts anytime while stopped."
        : "Finish first-time setup before starting the bot.";
    }

    const orders = state.open_orders || [];
    $("orderCount").textContent = String(orders.length);
    const list = $("orders");
    if (!orders.length) {
      if (running) {
        list.innerHTML =
          '<div class="orders-placing" role="status" aria-live="polite">' +
          '<span class="placing-light" aria-hidden="true"></span>' +
          '<div class="placing-copy">' +
          '<strong class="placing-title">Placing orders<span class="placing-dots" aria-hidden="true"></span></strong>' +
          '<span class="placing-sub">Working the book — maker orders going out</span>' +
          "</div>" +
          "</div>";
      } else {
        list.innerHTML =
          '<div class="orders-empty">No orders yet. Press Start to place maker orders.</div>';
      }
    } else {
      list.innerHTML = orders
        .map((o) => {
          const buy = String(o.side).toUpperCase() === "BUY";
          return (
            '<div class="order-row">' +
            '<span class="order-side ' +
            (buy ? "buy" : "sell") +
            '">' +
            (buy ? "BUY" : "SELL") +
            "</span>" +
            '<div class="order-main"><strong>' +
            Number(o.price).toFixed(6) +
            "</strong><span>" +
            Number(o.qty).toFixed(2) +
            " LBC · " +
            String(o.order_id || "").slice(0, 14) +
            "</span></div>" +
            '<span class="order-usdt">' +
            money(o.usdt) +
            "</span></div>"
          );
        })
        .join("");
    }

    if (state.last_error) {
      $("error").hidden = false;
      $("error").textContent = state.last_error;
    } else $("error").hidden = true;

    // Status view
    $("stConn").textContent = mid ? "Connected (MEXC public)" : "Waiting…";
    $("stMode").textContent = state.paper ? "Paper" : "LIVE";
    $("stRun").textContent = running ? "Running" : "Stopped";
    $("stStrat").textContent = "depth_provider (locked)";
    $("stMid").textContent = mid ? mid.toFixed(6) : "—";
    $("stBook").textContent =
      bb && ba ? Number(bb).toFixed(6) + " / " + Number(ba).toFixed(6) : "—";
    $("stPubBid").textContent = money(pub.bid_usd);
    $("stPubAsk").textContent = money(pub.ask_usd);
    $("stBotBid").textContent = money(bot.bid_usd);
    $("stBotAsk").textContent = money(bot.ask_usd);
    $("stOrders").textContent = String(orders.length);
    $("stUp").textContent = state.uptime_s
      ? Math.floor(state.uptime_s) + "s"
      : "—";
    $("stPnl").textContent = money(state.realized_pnl);
    $("stInv").textContent =
      money(state.free_usdt) +
      " / " +
      (Number.isFinite(state.free_lbc) ? Number(state.free_lbc).toFixed(2) : "—");
    $("stMsg").textContent = state.status_msg || "—";
    $("stErr").textContent = state.last_error || "—";

    // Settings mode cards
    $("setPaper").classList.toggle("selected", state.paper);
    $("setLive").classList.toggle("selected", !state.paper);
    $("keyStatus").textContent = state.hasKeys
      ? "Keys: set (env or config)"
      : "Keys: not set";

    draw();
  }

  function applyConfigFromServer(cfg, force) {
    if (!cfg) return;
    if (cfg.bot_id) state.bot_id = cfg.bot_id;
    state.setup_complete = !!cfg.setup_complete;
    state.paper = cfg.paper !== false;
    state.hasKeys = !!cfg.has_keys;

    // Only seed/overwrite capital·depth·steps on explicit force (boot / wizard finish /
    // settings save / tab switch). Market poll must NEVER stomp the sliders.
    if (force || !state.formSeeded) {
      if (cfg.usdt_budget != null) {
        usdtNum.value = cfg.usdt_budget;
        if (cfg.usdt_budget > Number(usdt.max)) usdt.max = cfg.usdt_budget;
        usdt.value = Math.min(cfg.usdt_budget, Number(usdt.max));
      }
      if (cfg.lbc_budget != null) {
        lbcNum.value = cfg.lbc_budget;
        if (cfg.lbc_budget > Number(lbc.max)) lbc.max = cfg.lbc_budget;
        lbc.value = Math.min(cfg.lbc_budget, Number(lbc.max));
      }
      if (cfg.bid_depth_pct != null) {
        const d = Math.min(MAX_DEPTH_EXPERT, Math.max(0.1, Number(cfg.bid_depth_pct)));
        depth.value = String(Math.min(MAX_DEPTH_SLIDER, Math.max(0.5, d)));
        if ($("depthCustom")) $("depthCustom").value = String(d);
        $("depthOut").textContent = "±" + d.toFixed(1) + "%";
      }
      if (cfg.n_levels != null) {
        levels.max = String(MAX_STEPS_SLIDER);
        const n = Math.max(1, Number(cfg.n_levels) || 1);
        levels.value = String(Math.min(MAX_STEPS_SLIDER, n));
        if ($("levelsCustom")) $("levelsCustom").value = String(n);
        $("levelsOut").textContent = String(n);
      }
      if ($("strategy")) $("strategy").value = "depth_provider";
      if (cfg.min_notional_usdt != null) {
        $("setMinNotional").value = Math.max(MIN_ORDER_USD, Number(cfg.min_notional_usdt));
      }
      if (cfg.reprice_pct != null) $("setReprice").value = cfg.reprice_pct;
      if (cfg.poll_interval_s != null) $("setPoll").value = cfg.poll_interval_s;
      state.formSeeded = true;
      if (state.bot_id) state.formSeededByBot[state.bot_id] = true;
      enforceOrderLimits();
    }

    updateSetupBanner();
  }

  async function refreshMarket(forceForm) {
    try {
      if (state.dirty && state.bot_id) {
        await post("/api/config", marketConfigBody());
        state.dirty = false;
      }
      const r = await fetch(withBot("/api/market"));
      const m = await r.json();
      if (m.bots) {
        state.bots = m.bots;
        if (!state.bot_id && m.bots.length) state.bot_id = m.bots[0].id;
        renderBotTabs();
      }
      if (!m.ok) {
        $("connPill").classList.add("err-conn");
        $("connPill").classList.remove("live-conn");
        $("connLabel").textContent = "Market offline";
      } else {
        state.mid = m.mid;
        state.best_bid = m.best_bid;
        state.best_ask = m.best_ask;
        state.public_depth = m.public_depth || {};
        state.public_depth_ladder = m.public_depth_ladder || [];
        state.bot_contribution = m.bot_contribution || {};
        state.desired = m.desired || [];
        if (m.config) applyConfigFromServer(m.config, !!forceForm);
        enforceOrderLimits();
      }

      const sr = await fetch(withBot("/api/state"));
      const s = await sr.json();
      if (s.bots) {
        state.bots = s.bots;
        renderBotTabs();
      }
      if (s.bot_id) state.bot_id = s.bot_id;
      state.running = !!s.running;
      if (s.config) applyConfigFromServer(s.config, !!forceForm);
      state.open_orders = s.open_orders || [];
      state.status_msg = s.status_msg;
      state.last_error = s.last_error;
      state.realized_pnl = s.realized_pnl;
      state.free_usdt = s.free_usdt;
      state.free_lbc = s.free_lbc;
      state.uptime_s = s.uptime_s;
      if (s.running && s.mid) {
        state.mid = s.mid;
        state.public_depth = s.public_depth || state.public_depth;
        state.bot_contribution = s.bot_contribution || state.bot_contribution;
        state.desired = s.desired || state.desired;
      }
      render();
    } catch (e) {
      $("connPill").classList.add("err-conn");
      $("connLabel").textContent = "Connection error";
      $("error").hidden = false;
      $("error").textContent = String(e.message || e);
    }
  }

  // ── Actions ───────────────────────────────────────────────────────────
  $("btnStart").onclick = async () => {
    if (!state.setup_complete) {
      openWizard(true);
      return;
    }
    $("btnStart").disabled = true;
    try {
      const res = await post("/api/start", marketConfigBody());
      if (res.error) {
        $("error").hidden = false;
        $("error").textContent = res.error;
      }
      state.dirty = false;
      await refreshMarket();
    } finally {
      $("btnStart").disabled = !state.setup_complete;
    }
  };

  $("btnStop").onclick = async () => {
    $("btnStop").disabled = true;
    try {
      await post("/api/stop", { cancel_orders: true });
      await refreshMarket();
    } finally {
      $("btnStop").disabled = false;
    }
  };

  if ($("btnStopKeep")) {
    $("btnStopKeep").onclick = async () => {
      $("btnStopKeep").disabled = true;
      try {
        await post("/api/stop", { cancel_orders: false });
        await refreshMarket();
      } finally {
        $("btnStopKeep").disabled = false;
      }
    };
  }

  // Settings mode
  $("setPaper").onclick = async () => {
    await post("/api/config", { ...marketConfigBody(), paper: true });
    state.paper = true;
    await refreshMarket();
  };
  $("setLive").onclick = () => {
    $("liveModal").hidden = false;
  };
  $("liveCancel").onclick = () => {
    $("liveModal").hidden = true;
  };
  $("liveConfirm").onclick = async () => {
    $("liveModal").hidden = true;
    await post("/api/config", {
      ...marketConfigBody(),
      paper: false,
      live_confirm: true,
    });
    state.paper = false;
    await refreshMarket();
  };

  $("btnSaveSettings").onclick = async () => {
    const body = {
      ...marketConfigBody(),
      strategy: "depth_provider",
      min_notional_usdt: Number($("setMinNotional").value),
      reprice_pct: Number($("setReprice").value),
      poll_interval_s: Number($("setPoll").value),
    };
    if ($("setAccessKey").value.trim()) body.mexc_api_key = $("setAccessKey").value.trim();
    if ($("setSecretKey").value.trim()) body.mexc_api_secret = $("setSecretKey").value.trim();
    const res = await post("/api/config", body);
    $("settingsMsg").textContent = res.ok ? "Settings saved." : "Save failed.";
    $("setAccessKey").value = "";
    $("setSecretKey").value = "";
    state.dirty = false;
    await refreshMarket();
  };

  window.addEventListener("resize", () => draw());

  // ── Boot ──────────────────────────────────────────────────────────────
  async function boot() {
    try {
      const br = await fetch("/api/bots");
      const bj = await br.json();
      if (bj.bots && bj.bots.length) {
        state.bots = bj.bots;
        state.bot_id = bj.bots[0].id;
        renderBotTabs();
      }
      const sr = await fetch(withBot("/api/state"));
      const s = await sr.json();
      if (s.bot_id) state.bot_id = s.bot_id;
      if (s.bots) {
        state.bots = s.bots;
        renderBotTabs();
      }
      if (s.config) applyConfigFromServer(s.config, true);
    } catch (_) {
      /* first paint */
    }
    try {
      if (!state.setup_complete) {
        openWizard(true);
      } else {
        $("wizard").hidden = true;
        $("app").hidden = false;
        updateSetupBanner();
      }
      await refreshMarket(true);
      setInterval(() => refreshMarket(false), 2000);
    } catch (e) {
      console.error(e);
      const w = $("wizard");
      if (w) w.hidden = false;
      const err = document.createElement("pre");
      err.style.cssText =
        "position:fixed;bottom:0;left:0;right:0;background:#300;color:#fcc;padding:12px;z-index:99;white-space:pre-wrap";
      err.textContent = "UI error: " + (e && e.message ? e.message : e);
      document.body.appendChild(err);
    }
  }

  try {
    boot();
  } catch (e) {
    console.error(e);
    const w = document.getElementById("wizard");
    if (w) w.hidden = false;
  }
})();
