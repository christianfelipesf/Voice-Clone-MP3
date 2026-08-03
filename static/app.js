/* Dublador Web - logica do painel */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var els = {
    banner: $("banner-ffmpeg"), start: $("btn-start"), pause: $("btn-pause"),
    stop: $("btn-stop"), download: $("btn-download"), progress: $("progress"),
    progLabel: $("prog-label"), status: $("status"), log: $("log"),
    previewBox: $("preview-box"), video: $("video"), samples: $("samples"),
    modeFile: $("mode-file"), modeYt: $("mode-yt"), weak: $("btn-weak"),
    strong: $("btn-strong"), reset: $("btn-reset"),
    device: $("device"), engine: $("engine"), lang: $("lang"), whisper: $("whisper"),
    res: $("res"), cookies: $("cookies"), volume: $("volume"), temp: $("temp"),
    seed: $("seed"), maxtempo: $("maxtempo"), file: $("file"), path: $("path"),
    srt: $("srt"), url: $("url"), preview: $("preview"), samplesChk: $("samples"),
    dry: $("dry"), pillFfmpeg: $("pill-ffmpeg"), pillPreview: $("pill-preview")
  };

  var jobId = null;
  var es = null;
  var player = null;
  var running = false;

  // ------------------------------------------------------------------
  function apiInfo() {
    return fetch("/api/info").then(function (r) { return r.json(); });
  }

  function fillSelect(sel, values, selected) {
    sel.innerHTML = "";
    values.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v;
      o.textContent = (typeof v === "object") ? v[Object.keys(v)[0]] : v;
      if (typeof v === "object") o.value = Object.keys(v)[0];
      sel.appendChild(o);
    });
    if (selected !== undefined && selected !== "") sel.value = selected;
  }

  function setPill(sel, text, state) {
    if (!sel) return;
    sel.textContent = text;
    sel.className = "pill" + (state ? " " + state : "");
  }

  function init() {
    apiInfo().then(function (info) {
      setPill(els.pillFfmpeg, info.ffmpeg ? "ffmpeg: instalado" : "ffmpeg: nao encontrado",
              info.ffmpeg ? "ok" : "err");
      setPill(els.pillPreview,
              info.preview ? "preview: disponivel" : "preview: indisponivel",
              info.preview ? "ok" : "err");
      if (!info.ffmpeg) els.banner.hidden = false;
      if (!info.preview && els.preview.checked) els.preview.checked = false;
      fillSelect(els.device, info.devices, info.defaults.device);
      fillSelect(els.engine, Object.keys(info.engines), info.defaults.engine);
      fillSelect(els.lang, info.langs, info.defaults.lang);
      fillSelect(els.whisper, info.whisper_models, info.defaults.whisper);
      fillSelect(els.res, info.resolutions, info.defaults.res);
      fillSelect(els.cookies, info.browsers, info.defaults.cookies);
      if (info.defaults.volume) els.volume.value = info.defaults.volume;
      if (info.defaults.temp) els.temp.value = info.defaults.temp;
      if (info.defaults.seed) els.seed.value = info.defaults.seed;
      if (info.defaults.maxtempo) els.maxtempo.value = info.defaults.maxtempo;
    }).catch(function () {
      setStatus("Nao foi possivel carregar /api/info.");
    });

    document.querySelectorAll('input[name="mode"]').forEach(function (r) {
      r.addEventListener("change", function () {
        var yt = r.value === "youtube";
        els.modeYt.hidden = !yt;
        els.modeFile.hidden = yt;
      });
    });

    els.start.addEventListener("click", startJob);
    els.pause.addEventListener("click", togglePause);
    els.stop.addEventListener("click", stopJob);
    els.download.addEventListener("click", function (e) {
      if (jobId) { e.preventDefault(); window.location.href = "/api/jobs/" + jobId + "/output"; }
    });

    els.weak.addEventListener("click", applyWeakMode);
    els.strong.addEventListener("click", applyStrongMode);
    els.reset.addEventListener("click", applyReset);
  }

  // ------------------------------------------------------------------
  function applyWeakMode() {
    els.device.value = "cpu";
    els.engine.value = "edge";
    els.whisper.value = "tiny";
    appendLog("[INFO] Modo PC fraco aplicado: CPU + Edge TTS (leve) + Whisper tiny.\n");
    setStatus("Modo PC fraco aplicado. Clique em Iniciar Dublagem.");
  }

  function applyStrongMode() {
    els.device.value = "auto";
    els.engine.value = "chatterbox";
    els.whisper.value = "small";
    appendLog("[INFO] Modo PC forte aplicado: Chatterbox (clonagem de voz) + Whisper small.\n");
    setStatus("Modo PC forte aplicado. Clique em Iniciar Dublagem.");
  }

  function applyReset() {
    return fetch("/api/prefs/reset", { method: "POST" })
      .then(function () { return apiInfo(); })
      .then(function (info) {
        var d = info.defaults;
        fillSelect(els.device, info.devices, d.device);
        fillSelect(els.engine, Object.keys(info.engines), d.engine);
        fillSelect(els.lang, info.langs, d.lang);
        fillSelect(els.whisper, info.whisper_models, d.whisper);
        fillSelect(els.res, info.resolutions, d.res);
        fillSelect(els.cookies, info.browsers, d.cookies);
        if (d.volume) els.volume.value = d.volume; else els.volume.value = "1.0";
        if (d.temp) els.temp.value = d.temp; else els.temp.value = "";
        if (d.seed) els.seed.value = d.seed; else els.seed.value = "";
        if (d.maxtempo) els.maxtempo.value = d.maxtempo; else els.maxtempo.value = "";
        els.preview.checked = false;
        appendLog("[INFO] Opcoes resetadas para os padroes (config limpo).\n");
        setStatus("Opcoes resetadas.");
      })
      .catch(function () {
        setStatus("Falha ao resetar as opcoes.");
      });
  }

  function currentMode() {
    var r = document.querySelector('input[name="mode"]:checked');
    return r ? r.value : "file";
  }

  function buildForm() {
    var fd = new FormData();
    fd.append("mode", currentMode());
    fd.append("device", els.device.value);
    fd.append("engine", els.engine.value);
    fd.append("lang", els.lang.value);
    fd.append("whisper", els.whisper.value);
    fd.append("volume", els.volume.value || "");
    fd.append("temp", els.temp.value || "");
    fd.append("seed", els.seed.value || "");
    fd.append("maxtempo", els.maxtempo.value || "");
    fd.append("cookies", els.cookies.value || "");
    fd.append("preview", els.preview.checked ? "1" : "0");
    fd.append("samples", els.samplesChk.checked ? "1" : "0");
    fd.append("dry", els.dry.checked ? "1" : "0");
    if (currentMode() === "youtube") {
      fd.append("url", els.url.value.trim());
      fd.append("res", els.res.value);
    } else {
      if (els.file.files && els.file.files[0]) fd.append("file", els.file.files[0]);
      fd.append("path", els.path.value.trim());
      if (els.srt.files && els.srt.files[0]) fd.append("srt", els.srt.files[0]);
    }
    return fd;
  }

  function startJob() {
    if (running) { setStatus("Ja existe uma dublagem em andamento."); return; }
    var fd = buildForm();
    if (currentMode() === "youtube" && !fd.get("url")) {
      setStatus("Cole o link do YouTube."); return;
    }
    if (currentMode() === "file" && !els.file.files[0] && !els.path.value.trim()) {
      setStatus("Selecione um arquivo ou informe um caminho local."); return;
    }

    resetUI();
    setStatus("Enviando...");
    fetch("/api/jobs", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { setStatus("Erro: " + (res.d.error || "falha ao iniciar")); return; }
        jobId = res.d.id;
        running = true;
        els.start.disabled = true;
        els.pause.disabled = false;
        els.stop.disabled = false;
        els.start.textContent = "Dublando...";
        appendLog("[OK] Job iniciado: " + jobId + "\n");
        if (res.d.preview) startPreview("/api/jobs/" + jobId + "/preview");
        connectSSE(jobId);
      })
      .catch(function (err) { setStatus("Erro de rede: " + err); });
  }

  // ------------------------------------------------------------------
  function connectSSE(id) {
    if (es) { es.close(); }
    es = new EventSource("/api/jobs/" + id + "/stream");
    es.onmessage = function (e) {
      try { handle(JSON.parse(e.data)); }
      catch (err) { /* ignora */ }
    };
    es.onerror = function () { /* EventSource fecha sozinho ao final */ };
  }

  function handle(d) {
    switch (d.type) {
      case "log": appendLog(d.line); break;
      case "progress":
        els.progress.value = Math.round((d.pct || 0) * 100);
        els.progLabel.textContent = Math.round((d.pct || 0) * 100) + "%  (" + d.done + "/" + d.total + ")";
        break;
      case "phase": setStatus(d.label); break;
      case "seg": addSample(d); break;
      case "preview_start": startPreview(d.url); break;
      case "status": finalize(d.status, d.error); break;
    }
  }

  function finalize(status, error) {
    running = false;
    es && es.close();
    es = null;
    els.start.disabled = false;
    els.pause.disabled = true;
    els.stop.disabled = true;
    els.start.textContent = "Iniciar Dublagem";
    if (status === "done") {
      els.progress.value = 100;
      els.progLabel.textContent = "100%";
      setStatus("Concluido!");
      els.download.hidden = false;
    } else if (status === "cancelled") {
      setStatus("Cancelado.");
    } else {
      setStatus("Erro: " + (error || "falha na dublagem"));
    }
  }

  function togglePause() {
    if (!jobId || !running) return;
    var action = els.pause.textContent === "Pausar" ? "pause" : "resume";
    fetch("/api/jobs/" + jobId + "/pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) {
        els.pause.textContent = action === "pause" ? "Continuar" : "Pausar";
        appendLog("[PAUSA] " + (action === "pause" ? "pausado." : "retomado.") + "\n");
      }
    });
  }

  function stopJob() {
    if (!jobId || !running) return;
    fetch("/api/jobs/" + jobId + "/stop", { method: "POST" })
      .then(function () { appendLog("[PARADA] Cancelando dublagem...\n"); });
  }

  // ------------------------------------------------------------------
  function startPreview(url) {
    if (player) return;
    els.previewBox.hidden = false;
    if (typeof mpegts === "undefined" || !mpegts.getFeatureList().mseLivePlayback) {
      setStatus("Navegador sem suporte a MSE/mpegts.");
      return;
    }
    try {
      player = mpegts.createPlayer({ type: "mse", isLive: true, url: url });
      player.attachMediaElement(els.video);
      player.load();
      player.play();
      player.on(mpegts.Events.ERROR, function () {
        if (player) { try { player.destroy(); } catch (e) {} player = null; }
      });
    } catch (err) {
      setStatus("Erro ao abrir o preview: " + err);
    }
  }

  function addSample(seg) {
    var empty = els.samples.querySelector("p.muted");
    if (empty) empty.remove();
    var row = document.createElement("div");
    row.className = "sample";
    var audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = "/api/jobs/" + jobId + "/samples/" + seg.idx;
    var txt = document.createElement("span");
    txt.className = "s-text";
    txt.textContent = "#" + seg.idx + "  " + seg.text;
    row.appendChild(audio);
    row.appendChild(txt);
    els.samples.appendChild(row);
    els.samples.scrollTop = els.samples.scrollHeight;
  }

  // ------------------------------------------------------------------
  function resetUI() {
    els.log.textContent = "";
    els.progress.value = 0;
    els.progLabel.textContent = "0%";
    setStatus("");
    els.download.hidden = true;
    els.samples.innerHTML = '<p class="muted">Nenhuma amostra ainda.</p>';
    if (player) { try { player.destroy(); } catch (e) {} player = null; }
    els.video.src = "";
    els.previewBox.hidden = true;
  }

  function appendLog(line) {
    els.log.textContent += line;
    els.log.scrollTop = els.log.scrollHeight;
  }

  function setStatus(text) { els.status.textContent = text; }

  document.addEventListener("DOMContentLoaded", init);
})();
