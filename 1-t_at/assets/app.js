/* Phone UI logic. Polls the board's REST API with fetch; no frameworks, no external files. */
(function () {
  "use strict";

  var POLL_ACTIVE_MS = 300;     // while a run is in progress
  var POLL_IDLE_MS = 1000;      // on the home screen
  var RING = 326.73;            // circumference of the ring (r = 52)

  var state = null;             // last snapshot from /api/state
  var recipes = [];             // from /api/recipes
  var settings = null;          // from /api/settings (while the sheet is open)
  var portions = {};            // stepper value per recipe tile
  var pending = null;           // parsed order waiting for "Start"
  var lastView = null;
  var lastAnnounced = null;
  var appName = "";
  var failures = 0;
  var inFlight = false;
  var pollTimer = null;
  var toastTimer = null;
  var requestSeq = 0;           // newer answers win over older ones
  var appliedSeq = 0;
  var speakOn = store("speak") === "1";

  // ---- small helpers -----------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function all(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function show(el, on) { el.hidden = !on; }

  function text(el, value) {
    value = value === null || value === undefined ? "" : String(value);
    if (el.textContent !== value) { el.textContent = value; }
  }

  function make(tag, className, content) {
    var el = document.createElement(tag);
    if (className) { el.className = className; }
    if (content !== undefined) { el.textContent = content; }
    return el;
  }

  function store(key, value) {
    try {
      if (value === undefined) { return window.localStorage.getItem(key); }
      window.localStorage.setItem(key, value);
    } catch (e) { /* private mode: live without it */ }
    return null;
  }

  function num(n) { return Math.round(Number(n) || 0).toLocaleString("en-US"); }
  function fmtMl(ml) { return num(ml) + " ml"; }
  function fmtWater(ml) { return ml >= 1000 ? (ml / 1000).toFixed(1) + " L" : Math.round(ml) + " ml"; }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : (many || one + "s")); }

  function whParts(wh) {
    wh = Number(wh) || 0;
    if (wh >= 1000) { return [(wh / 1000).toFixed(2), "kWh"]; }
    return [wh >= 100 ? String(Math.round(wh)) : String(Math.round(wh * 10) / 10), "Wh"];
  }
  function fmtWh(wh) { var p = whParts(wh); return p[0] + " " + p[1]; }

  function fmtEur(eur) {
    eur = Number(eur) || 0;
    return eur < 0.095 ? (Math.round(eur * 1000) / 10) + " ct" : "€" + eur.toFixed(2);
  }

  function timeParts(seconds) {
    seconds = Math.round(Number(seconds) || 0);
    if (seconds < 60) { return [String(seconds), "s less boiling"]; }
    var rest = seconds % 60;
    return [Math.floor(seconds / 60) + ":" + (rest < 10 ? "0" : "") + rest, "min less boiling"];
  }

  function toast(message) {
    var el = $("toast");
    text(el, message);
    show(el, true);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { show(el, false); }, 2600);
  }

  // ---- talking to the board ---------------------------------------------

  function api(method, path, body, timeoutMs) {
    var controller = window.AbortController ? new AbortController() : null;
    var timer = controller ? setTimeout(function () { controller.abort(); }, timeoutMs || 5000) : null;
    var options = { method: method, cache: "no-store" };
    if (controller) { options.signal = controller.signal; }
    if (body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(function (response) {
      if (!response.ok) { throw new Error("HTTP " + response.status); }
      return response.json();
    }).then(function (data) {
      clearTimeout(timer);
      return data;
    }, function (error) {
      clearTimeout(timer);
      throw error;
    });
  }

  function schedule(ms) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, ms);
  }

  function poll() {
    if (inFlight) { return; }
    inFlight = true;
    var mine = ++requestSeq;
    api("GET", "/api/state", undefined, 4000).then(function (snap) {
      if (!snap || !snap.flow) { throw new Error("no state"); }
      failures = 0;
      show($("banner"), false);
      if (mine > appliedSeq) { appliedSeq = mine; render(snap); }
    }).catch(function () {
      failures += 1;
      if (failures >= 3) {                       // keep retrying, but say so
        show($("banner"), true);
        $("boardStatus").dataset.state = "offline";
        text($("boardStatusText"), "No connection");
        show($("scaleBadge"), false);
      }
    }).then(function () {
      inFlight = false;
      var idle = !state || state.flow.state === "idle";
      schedule(idle ? POLL_IDLE_MS : POLL_ACTIVE_MS);
    });
  }

  /* A button was pressed: POST, draw the snapshot that comes back, poll again soon. */
  function act(path, body) {
    var mine = ++requestSeq;
    return api("POST", path, body === undefined ? {} : body, 6000).then(function (snap) {
      if (snap && snap.flow && mine > appliedSeq) { appliedSeq = mine; render(snap); }
      if (snap && snap.note) { toast(snap.note); }
      if (!inFlight) { schedule(150); }
      return snap;
    }).catch(function () {
      toast("The board did not answer.");
      if (!inFlight) { schedule(150); }
    });
  }

  function loadRecipes() {
    return api("GET", "/api/recipes").then(function (data) {
      recipes = data.recipes || [];
      buildTiles();
    });
  }

  // ---- read aloud --------------------------------------------------------

  function speak(message) {
    if (!speakOn || !message || !("speechSynthesis" in window)) { return; }
    try {
      window.speechSynthesis.cancel();
      var utterance = new SpeechSynthesisUtterance(String(message).replace(/…/g, ""));
      utterance.lang = "en-US";
      window.speechSynthesis.speak(utterance);
    } catch (e) { /* no voices on this phone */ }
  }

  function announce(flow) {
    var key = flow.state + "|" + flow.message;
    if (key === lastAnnounced) { return; }
    var first = lastAnnounced === null;
    lastAnnounced = key;
    if (first && flow.state === "idle") { return; }       // stay quiet when the page opens
    text($("live"), flow.message);
    speak(flow.message);
    if (!first && navigator.vibrate && (flow.state === "done" || flow.state === "error")) {
      navigator.vibrate(flow.state === "done" ? 80 : [80, 60, 80]);
    }
  }

  // ---- drawing -----------------------------------------------------------

  function render(snap) {
    state = snap;
    var view = snap.flow.state;
    renderHeader(snap);
    if (view !== lastView) { switchView(view); }
    renderContext(snap);
    if (view === "idle") { renderHome(snap); }
    else if (view === "vessel") { renderVessel(snap); }
    else if (view === "ingredient") { renderIngredient(snap); }
    else if (view === "ready") { renderReady(snap); }
    else if (view === "dispensing") { renderDispensing(snap); }
    else if (view === "done") { renderDone(snap); }
    else if (view === "error") { renderError(snap); }
    renderDemo(snap);
    announce(snap.flow);
  }

  function renderHeader(snap) {
    var name = snap.app && snap.app.name;
    if (name && name !== appName) {
      appName = name;
      text($("appName"), name);
      document.title = name;
      store("appName", name);
    }
    var online = !!snap.board.online;
    $("boardStatus").dataset.state = online ? "online" : "offline";
    text($("boardStatusText"), online ? "Online" : "Board offline");
    $("boardStatus").setAttribute("aria-label", online ? "Board online" : "Board offline");
    show($("scaleBadge"), online && !!snap.board.virtual_scale);
  }

  function resetRing(arc, offset) {
    arc.style.transition = "none";
    arc.style.strokeDashoffset = String(offset);
    arc.getBoundingClientRect();                   // apply before the next transition starts
    arc.style.transition = "";
  }

  function switchView(view) {
    all(".view").forEach(function (el) { el.hidden = el.dataset.view !== view; });
    if (view === "ready") { resetRing($("readyArc"), 0); text($("readyCount"), ""); }
    if (view === "dispensing") { resetRing($("dispArc"), RING); }
    if (view !== "idle") {
      $("askInput").blur();
      show($("confirm"), false);
      show($("sorry"), false);
      show($("thinking"), false);
      pending = null;
    }
    window.scrollTo(0, 0);
    if (lastView !== null) {
      var heading = document.querySelector('.view[data-view="' + view + '"] h1');
      if (heading) { try { heading.focus({ preventScroll: true }); } catch (e) { heading.focus(); } }
    }
    lastView = view;
  }

  function renderContext(snap) {
    var flow = snap.flow;
    var el = $("context");
    if (flow.state === "idle" || !flow.name) { show(el, false); return; }
    var parts = [flow.emoji + " " + flow.name, flow.amount];
    if (flow.target_ml && flow.state !== "ingredient") { parts.push(fmtMl(flow.target_ml)); }
    text(el, parts.join(" · "));
    show(el, true);
  }

  function renderHome(snap) {
    var today = snap.stats.today;
    var total = snap.stats.total;
    if (total.runs > 0) {
      text($("statsToday"), today.runs
        ? "Today: " + fmtWh(today.saved_wh) + " saved in " + plural(today.runs, "run")
        : "Nothing saved yet today");
      text($("statsTotal"), "All time: " + fmtWh(total.saved_wh) + " · " + fmtWater(total.saved_ml) +
        " · " + fmtEur(total.saved_eur));
    } else {
      text($("statsToday"), "No runs yet");
      text($("statsTotal"), "Your savings will show up here.");
    }
  }

  function renderVessel(snap) {
    var flow = snap.flow;
    var taring = flow.phase === "tare";
    text($("vesselMsg"), flow.message);
    text($("vesselHint"), flow.hint);
    text($("vesselWeight"), num(snap.scale.net_g));
    text($("vesselWait"), taring ? "zeroing…" : (snap.scale.stable ? "waiting for the " + flow.vessel + "…" : "settling…"));
    document.querySelector('.view[data-view="vessel"] .art').classList.toggle("settled", taring);
    $("vesselNext").disabled = taring;
    text($("vesselSim"), "Place " + flow.vessel + " (virtual)");
    show($("vesselSim"), !!snap.board.virtual_scale && !taring);
  }

  function renderIngredient(snap) {
    var flow = snap.flow;
    text($("ingMsg"), flow.message);
    text($("ingHint"), flow.hint);
    text($("ingGrams"), num(flow.ingredient_g));
    text($("ingWater"), flow.target_ml ? fmtMl(flow.target_ml) : "–");
    $("ingDone").disabled = !flow.can_next;
    show($("ingSim"), !!snap.board.virtual_scale);
  }

  function renderReady(snap) {
    var flow = snap.flow;
    var count = flow.countdown || 1;
    var el = $("readyCount");
    text($("readyMsg"), flow.message);
    text($("readyHint"), flow.hint);
    if (el.textContent !== String(count)) {
      text(el, count);
      el.classList.remove("tick");
      el.getBoundingClientRect();
      el.classList.add("tick");
      // drain the ring smoothly through this second
      var arc = $("readyArc");
      arc.style.transition = "stroke-dashoffset 1s linear";
      arc.style.strokeDashoffset = String(RING * (1 - (count - 1) / 3));
    }
  }

  function renderDispensing(snap) {
    var flow = snap.flow;
    var progress = Math.max(0, Math.min(1, Number(flow.progress) || 0));
    text($("dispMsg"), flow.message);
    text($("dispValue"), num(flow.dispensed_ml));
    text($("dispOf"), "of " + fmtMl(flow.target_ml || 0));
    $("dispArc").style.strokeDashoffset = String(RING * (1 - progress));
    $("dispRing").setAttribute("aria-valuenow", String(Math.round(progress * 100)));
    if (snap.valve.open) {
      text($("dispHint"), flow.hint + (snap.scale.flow_gps > 0 ? " " + Math.round(snap.scale.flow_gps) + " g/s." : ""));
    } else if (progress > 0.5) {
      text($("dispHint"), "Valve closed. Letting the last drops settle…");
    } else {
      text($("dispHint"), flow.hint);
    }
  }

  function renderDone(snap) {
    var flow = snap.flow;
    var run = snap.last_run || {};
    var today = snap.stats.today;
    var saved = Number(run.saved_ml) || 0;
    var wh = whParts(run.saved_wh);
    text($("doneMsg"), flow.message);
    if (saved >= 10) {
      text($("doneSavedMl"), fmtMl(saved));
      text($("doneSavedMl").nextSibling, " less than usual");
    } else {
      text($("doneSavedMl"), "Just right");
      text($("doneSavedMl").nextSibling, " exactly what the dish needs");
    }
    show(document.querySelector(".saved-grid"), saved >= 10);
    text($("doneWh"), wh[0]);
    text($("doneWh").nextElementSibling, wh[1] + " saved");
    text($("doneEur"), fmtEur(run.saved_eur));
    show($("doneTimeBox"), run.saved_seconds !== null && run.saved_seconds !== undefined);
    var time = timeParts(run.saved_seconds);
    text($("doneTime"), time[0]);
    text($("doneTime").nextElementSibling, time[1]);
    text($("doneBaseline"), "Usual: " + fmtMl(run.baseline_ml || 0) + ". " + (run.baseline_note || ""));
    text($("doneTip"), flow.hint || run.tip || "");
    text($("doneToday"), "Today: " + plural(today.runs, "run") + " · " + fmtWater(today.saved_ml) +
      " not heated · " + fmtWh(today.saved_wh) + " · " + fmtEur(today.saved_eur));
  }

  function renderError(snap) {
    text($("errMsg"), snap.flow.message);
    text($("errHint"), snap.flow.hint);
  }

  function renderDemo(snap) {
    var virtual = !!snap.board.virtual_scale && !!snap.board.online;
    show($("demo"), virtual);
    if (!virtual) { return; }
    text($("demoWeight"), num(snap.scale.net_g) + " g");
    text($("demoStable"), snap.scale.stable ? "(stable)" : "(moving)");
    text($("demoPlace"), "Place " + (snap.flow.vessel || "kettle"));
    var sim = snap.sim;
    show($("vboard"), !!sim);
    if (sim) {
      text($("vbScreen"), sim.screen);
      $("vbLed").dataset.rgb = sim.rgb;
      text($("vbRgb"), sim.rgb);
      text($("vbValve"), sim.valve_open ? "open" : "closed");
      text($("vbBeep"), sim.beep || "quiet");
    }
  }

  // ---- home: recipe tiles and free text ----------------------------------

  function buildTiles() {
    var root = $("tiles");
    root.textContent = "";
    recipes.forEach(function (recipe) {
      if (!(recipe.id in portions)) { portions[recipe.id] = recipe.default_portions; }
      portions[recipe.id] = Math.max(1, Math.min(recipe.max_portions, portions[recipe.id]));

      var tile = make("article", "card tile");
      var main = make("button", "tile-main");
      main.type = "button";
      var emoji = make("span", "tile-emoji", recipe.emoji);
      emoji.setAttribute("aria-hidden", "true");
      var amount = make("span", "tile-amount");
      main.appendChild(emoji);
      main.appendChild(make("span", "tile-name", recipe.name));
      main.appendChild(amount);

      var stepper = make("div", "stepper");
      stepper.setAttribute("role", "group");
      stepper.setAttribute("aria-label", "How many " + recipe.unit_many);
      var minus = make("button", "step", "−");
      minus.type = "button";
      minus.setAttribute("aria-label", "One " + recipe.unit_one + " less");
      var plus = make("button", "step", "+");
      plus.type = "button";
      plus.setAttribute("aria-label", "One " + recipe.unit_one + " more");
      var value = make("span", "step-value");
      var count = make("b");
      var unit = make("small");
      value.appendChild(count);
      value.appendChild(unit);
      stepper.appendChild(minus);
      stepper.appendChild(value);
      stepper.appendChild(plus);

      function refresh() {
        var n = portions[recipe.id];
        var unitWord = n === 1 ? recipe.unit_one : recipe.unit_many;
        var water = (recipe.weigh_in_pot ? "about " : "") + fmtWater(recipe.amounts_ml[n - 1]);
        text(count, n);
        text(unit, unitWord);
        text(amount, water + " ›");
        minus.disabled = n <= 1;
        plus.disabled = n >= recipe.max_portions;
        main.setAttribute("aria-label", "Start " + recipe.name + " for " + n + " " + unitWord + ", " + water + " of water");
      }
      minus.addEventListener("click", function () { portions[recipe.id] -= 1; refresh(); });
      plus.addEventListener("click", function () { portions[recipe.id] += 1; refresh(); });
      main.addEventListener("click", function () {
        act("/api/select", { recipe: recipe.id, portions: portions[recipe.id], grams: null });
      });
      refresh();

      tile.appendChild(main);
      tile.appendChild(stepper);
      root.appendChild(tile);
    });
  }

  function ask(value) {
    value = String(value || "").trim();
    if (!value) { $("askInput").focus(); return; }
    $("askInput").blur();
    pending = null;
    show($("confirm"), false);
    show($("sorry"), false);
    var llm = state && state.llm;
    text($("thinkingText"), llm && llm.enabled && llm.status === "ready"
      ? llm.label + " is reading it on the board…"
      : "Reading your order…");
    show($("thinking"), true);
    $("askGo").disabled = true;
    api("POST", "/api/parse", { text: value }, 30000).then(function (result) {
      show($("thinking"), false);
      $("askGo").disabled = false;
      if (result && result.ok) {
        pending = { recipe: result.recipe, portions: result.portions, grams: result.grams };
        text($("confirmEmoji"), result.emoji || "");
        text($("confirmLine"), result.message);
        text($("confirmBadge"), result.badge || "");
        show($("confirm"), true);
        text($("live"), result.message + ". " + (result.badge || ""));
      } else {
        text($("sorryText"), (result && result.message) || "Sorry, I did not get that.");
        show($("sorry"), true);
        text($("live"), $("sorryText").textContent);
      }
    }).catch(function () {
      show($("thinking"), false);
      $("askGo").disabled = false;
      toast("The board did not answer.");
    });
  }

  // ---- sheets ------------------------------------------------------------

  function openSheet(sheet) {
    if (sheet.showModal) { if (!sheet.open) { sheet.showModal(); } } else { sheet.setAttribute("open", ""); }
  }

  function closeSheet(sheet) {
    if (sheet.close) { sheet.close(); } else { sheet.removeAttribute("open"); }
  }

  var NUMBER_KEYS = ["t_start_c", "eff_kettle", "eff_stove", "price_eur_per_kwh", "kettle_kw",
                     "kettle_min_ml", "flow_gps"];

  function llmLine() {
    var llm = state && state.llm;
    if (!llm) { return ""; }
    if (!llm.available) { return "No local LLM here: the rules do the reading."; }
    var line = llm.label + ": " + llm.status;
    if (llm.status === "ready" && llm.last_ms !== null) { line += ", last answer " + (llm.last_ms / 1000).toFixed(1) + " s"; }
    if (llm.status === "failed") { line += " (the rules are used)" + (llm.note ? ". " + llm.note : ""); }
    return line;
  }

  function fillSettings() {
    NUMBER_KEYS.forEach(function (key) { $("set_" + key).value = settings[key]; });
    var llmAvailable = !!(state && state.llm && state.llm.available);
    $("setLlm").checked = llmAvailable && !!settings.llm_enabled;
    $("setLlm").disabled = !llmAvailable;
    text($("llmInfo"), llmLine());
    $("setSpeak").checked = speakOn;
    $("setSpeak").disabled = !("speechSynthesis" in window);

    var root = $("baselineFields");
    root.textContent = "";
    recipes.forEach(function (recipe) {
      var label = make("label", "field");
      var name = make("span", "", recipe.name);
      name.appendChild(make("small", "", recipe.baseline_note));
      var input = make("input");
      input.type = "number";
      input.inputMode = "decimal";
      input.step = recipe.baseline.type === "factor" ? "0.1" : "1";
      input.min = "0.5";
      input.max = "30";
      input.value = recipe.baseline.value;
      input.dataset.baseline = recipe.id;
      input.dataset.standard = recipe.baseline["default"];
      label.appendChild(name);
      label.appendChild(input);
      label.appendChild(make("em", "", recipe.baseline.type === "factor" ? "× need" : "ml/g"));
      root.appendChild(label);
    });

    var board = state ? state.board : {};
    show($("virtualSection"), !!board.virtual_scale);
    show($("calSection"), !!board.online && !board.virtual_scale);
    text($("calResult"), settings.cal ? "Current factor: " + settings.cal + " counts per gram." : "Not calibrated yet.");
    var about = [appName + " " + (state ? state.app.version : "")];
    if (board.online) {
      about.push("firmware " + (board.fw || "?"));
      about.push(board.virtual_scale ? "virtual scale" : "HX711 scale");
      about.push("Knob " + (board.knob ? "yes" : "no"));
      about.push("Buzzer " + (board.buzzer ? "yes" : "no"));
      if (board.tilt) { about.push("tilt switch"); }
      about.push("valve lead " + Number(settings.lead_s).toFixed(2) + " s");
    } else {
      about.push("board offline");
    }
    text($("aboutLine"), about.join(" · "));
  }

  function openSettings() {
    Promise.all([api("GET", "/api/settings"), api("GET", "/api/recipes")]).then(function (results) {
      settings = results[0];
      recipes = results[1].recipes || recipes;
      fillSettings();
      openSheet($("settingsSheet"));
    }).catch(function () { toast("The board did not answer."); });
  }

  function saveSettings(payload, message) {
    return api("POST", "/api/settings", payload).then(function (saved) {
      settings = saved;
      return loadRecipes();
    }).then(function () {
      fillSettings();
      if (message) { toast(message); }
      if (!inFlight) { schedule(150); }
    }).catch(function () { toast("Could not save. The board did not answer."); });
  }

  function collectSettings() {
    var payload = { baselines: {} };
    NUMBER_KEYS.forEach(function (key) {
      var value = parseFloat(String($("set_" + key).value).replace(",", "."));
      if (!isNaN(value)) { payload[key] = value; }
    });
    all("[data-baseline]").forEach(function (input) {
      var value = parseFloat(String(input.value).replace(",", "."));
      if (isNaN(value)) { return; }
      var same = Math.abs(value - Number(input.dataset.standard)) < 1e-9;      // as in recipes.json
      payload.baselines[input.dataset.baseline] = same ? null : value;
    });
    return payload;
  }

  function calibrate(grams) {
    text($("calResult"), "Working…");
    api("POST", "/api/calibrate", { known_g: grams }, 8000).then(function (result) {
      text($("calResult"), result.message + (result.factor ? " Factor: " + result.factor + " counts per gram." : ""));
    }).catch(function () { text($("calResult"), "The board did not answer."); });
  }

  function openHistory() {
    api("GET", "/api/history").then(function (history) {
      var today = history.totals.today;
      var total = history.totals.total;
      text($("histTodayWh"), fmtWh(today.saved_wh));
      text($("histTodayMore"), plural(today.runs, "run") + " · " + fmtWater(today.saved_ml) + " · " + fmtEur(today.saved_eur));
      text($("histTotalWh"), fmtWh(total.saved_wh));
      text($("histTotalMore"), plural(total.runs, "run") + " · " + fmtWater(total.saved_ml) + " · " + fmtEur(total.saved_eur));
      var list = $("histList");
      list.textContent = "";
      if (!history.runs.length) {
        list.appendChild(make("li", "empty", "No runs yet. Make a cup of tea."));
      }
      var todayDate = new Date().toDateString();
      history.runs.forEach(function (run) {
        var when = new Date((run.ts || 0) * 1000);
        var clock = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        var day = when.toDateString() === todayDate ? "" : when.toLocaleDateString([], { day: "numeric", month: "short" }) + ", ";
        var item = make("li");
        var emoji = make("span", "run-emoji", run.emoji || "💧");
        emoji.setAttribute("aria-hidden", "true");
        var body = make("span", "run-text");
        body.appendChild(make("b", "", (run.name || run.recipe) + " · " + (run.amount || "")));
        body.appendChild(make("span", "", fmtMl(run.dispensed_ml || 0) + " · " + day + clock));
        item.appendChild(emoji);
        item.appendChild(body);
        item.appendChild(make("span", "run-saved", "−" + fmtWh(run.saved_wh)));
        list.appendChild(item);
      });
      openSheet($("historySheet"));
    }).catch(function () { toast("The board did not answer."); });
  }

  // ---- wiring ------------------------------------------------------------

  function on(id, event, handler) { $(id).addEventListener(event, handler); }

  function wire() {
    on("askForm", "submit", function (event) {
      event.preventDefault();
      ask($("askInput").value);
    });
    all(".chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        $("askInput").value = chip.dataset.example;
        ask(chip.dataset.example);
      });
    });
    on("confirmStart", "click", function () {
      if (pending) { act("/api/select", pending); }
    });
    on("confirmDismiss", "click", function () {
      pending = null;
      show($("confirm"), false);
      $("askInput").focus();
    });

    all('[data-action="cancel"]').forEach(function (button) {
      button.addEventListener("click", function () { act("/api/cancel"); });
    });
    on("vesselNext", "click", function () { act("/api/next"); });
    on("ingDone", "click", function () { act("/api/next"); });
    on("readyNow", "click", function () { act("/api/next"); });
    on("stopBtn", "click", function () { act("/api/stop"); });
    on("doneNew", "click", function () { act("/api/cancel"); });
    on("errRetry", "click", function () { act("/api/cancel"); });

    // virtual scale
    on("vesselSim", "click", function () { act("/api/sim", { action: "place_vessel" }); });
    on("demoPlace", "click", function () { act("/api/sim", { action: "place_vessel" }); });
    on("demoRemove", "click", function () { act("/api/sim", { action: "remove_vessel" }); });
    all("[data-sim-add]").forEach(function (button) {
      button.addEventListener("click", function () {
        act("/api/sim", { action: "add", grams: Number(button.dataset.simAdd) });
      });
    });
    on("demoSetForm", "submit", function (event) {
      event.preventDefault();
      var grams = parseFloat($("demoSet").value);
      if (!isNaN(grams)) { act("/api/sim", { action: "set", grams: grams }); }
    });
    on("vbKnobMinus", "click", function () { act("/api/sim", { action: "knob_turn", grams: -1 }); });
    on("vbKnobPlus", "click", function () { act("/api/sim", { action: "knob_turn", grams: 1 }); });
    on("vbKnobPress", "click", function () { act("/api/sim", { action: "knob_press" }); });
    if (store("demoOpen") === "1") { $("demo").open = true; }
    on("demo", "toggle", function () { store("demoOpen", $("demo").open ? "1" : "0"); });

    // sheets
    on("settingsBtn", "click", openSettings);
    on("statsCard", "click", openHistory);
    all(".sheet").forEach(function (sheet) {
      all("[data-close]", sheet).forEach(function (button) {
        button.addEventListener("click", function () { closeSheet(sheet); });
      });
      sheet.addEventListener("click", function (event) {
        if (event.target === sheet) { closeSheet(sheet); }      // tap on the backdrop
      });
    });
    on("setLlm", "change", function () {
      saveSettings({ llm_enabled: $("setLlm").checked }, $("setLlm").checked ? "Local LLM on" : "Local LLM off: rules only");
    });
    on("setSpeak", "change", function () {
      speakOn = $("setSpeak").checked;
      store("speak", speakOn ? "1" : "0");
      if (speakOn) { speak("Read aloud is on."); }
      else if ("speechSynthesis" in window) { window.speechSynthesis.cancel(); }
    });
    on("settingsSave", "click", function () { saveSettings(collectSettings(), "Saved"); });
    on("settingsReset", "click", function () { saveSettings({ reset: true }, "Back to the defaults"); });
    on("calZero", "click", function () { calibrate(0); });
    on("calGo", "click", function () {
      var grams = parseFloat($("calGrams").value);
      if (isNaN(grams) || grams <= 0) { text($("calResult"), "Enter the known weight in grams first."); return; }
      calibrate(grams);
    });

    // iOS only lets a page speak after a tap: unlock it with the first one
    document.addEventListener("pointerdown", function unlock() {
      document.removeEventListener("pointerdown", unlock);
      if (speakOn && "speechSynthesis" in window) {
        try {
          var quiet = new SpeechSynthesisUtterance(" ");
          quiet.volume = 0;
          window.speechSynthesis.speak(quiet);
        } catch (e) { /* ignore */ }
      }
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !inFlight) { schedule(50); }
    });
  }

  function start() {
    appName = store("appName") || "";
    text($("appName"), appName);
    if (appName) { document.title = appName; }
    wire();
    (function retryRecipes() {
      loadRecipes().catch(function () { setTimeout(retryRecipes, 2000); });
    })();
    poll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
