/* Dublador Web - logica do painel */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var els = {
    banner: $("banner-ffmpeg"), start: $("btn-start"), pause: $("btn-pause"),
    stop: $("btn-stop"), download: $("btn-download"), progress: $("progress"),
    progLabel: $("prog-label"), status: $("status"), log: $("log"),
    previewBox: $("preview-box"), video: $("video"),
    previewPlaceholder: $("preview-placeholder"),
    previewStatus: $("preview-status"),
    previewStatusLabel: $("preview-status-label"),
    previewStatusSub: $("preview-status-sub"),
    previewStatusBar: $("preview-status-bar"),
    samples: $("samples"),
    modeFile: $("mode-file"), modeYt: $("mode-yt"),
    reset: $("btn-reset"),
    device: $("device"), engine: $("engine"), lang: $("lang"), whisper: $("whisper"),
    voice: $("voice"),
    parallel: $("parallel"), whisperBeam: $("whisper-beam"),
    res: $("res"), cookies: $("cookies"),
    volume: $("volume"), temp: $("temp"),
    seed: $("seed"), maxtempo: $("maxtempo"), file: $("file"), path: $("path"),
    srt: $("srt"), url: $("url"), preview: $("preview"), samplesChk: $("samples-chk"),
    dry: $("dry"), pillFfmpeg: $("pill-ffmpeg"), pillPreview: $("pill-preview"),
    qsGo: $("qs-go"), qsUrl: $("qs-url"), qsRes: $("qs-res"),
    qsStatus: $("qs-status"),
    previewControls: $("preview-controls"),
    pcMute: $("pc-mute"),
    pcMuteOn: $("pc-mute-icon-on"),
    pcMuteOff: $("pc-mute-icon-off"),
    pcVolume: $("pc-volume")
  };

  var jobId = null;
  var es = null;
  var player = null;
  var running = false;
  var currentPhase = "";
  var lastProg = null;

  var LOG_MAX_CHARS = 5000;
  var SSE_RETRY_MS = 2000;

  var voiceVoices = {};
  var voiceModes = [];

  // ------------------------------------------------------------------
  function apiInfo() {
    return fetch("/api/info").then(function (r) { return r.json(); });
  }

  function fillSelect(sel, values, selected) {
    if (!sel) return;
    var previousValue = sel.value;
    sel.innerHTML = "";
    var list = [];
    if (Array.isArray(values)) {
      list = values;
    } else if (values && typeof values === "object") {
      list = Object.entries(values);
    }
    list.forEach(function (v) {
      var o = document.createElement("option");
      var value, label;
      if (Array.isArray(v) && v.length === 2) {
        value = String(v[0]);
        label = String(v[1]);
      } else if (v && typeof v === "object") {
        var entries = Object.entries(v);
        if (entries.length) {
          value = String(entries[0][0]);
          label = String(entries[0][1]);
        }
      } else {
        value = String(v);
        label = String(v);
      }
      o.value = value;
      o.textContent = label;
      sel.appendChild(o);
    });
    var chosen = (selected !== undefined && selected !== null && selected !== "")
                 ? String(selected) : previousValue;
    if (chosen) {
      var exists = Array.prototype.some.call(sel.options, function (opt) {
        return opt.value === chosen;
      });
      if (exists) sel.value = chosen;
    }
  }

  function setPill(sel, text, state) {
    if (!sel) return;
    sel.textContent = text;
    sel.className = "pill" + (state ? " " + state : "");
  }

  // Preenche o seletor de voz (Edge TTS) com as vozes do idioma escolhido,
  // alem dos modos "auto" (sorteia uma voz p/ o video inteiro), feminina e
  // masculina.
  function fillVoice(info, selected) {
    if (!els.voice) return;
    if (info) {
      voiceVoices = info.voices || {};
      voiceModes = info.voice_modes || [
        ["auto", "Automatica (sorteia uma voz)"],
        ["feminina", "Voz feminina"],
        ["masculina", "Voz masculina"]
      ];
    }
    var lang = (els.lang && els.lang.value) || "pt";
    var list = voiceModes.slice();
    (voiceVoices[lang] || []).forEach(function (v, i) {
      list.push([v, v + (i === 0 ? " (feminina)" : " (masculina)")]);
    });
    var prev = els.voice.value;
    fillSelect(els.voice, list,
               selected !== undefined && selected !== null
                 ? String(selected) : prev);
    var cur = els.voice.value;
    var valid = Array.prototype.some.call(els.voice.options, function (o) {
      return o.value === cur;
    });
    if (!valid) els.voice.value = "auto";
  }

  function updateVoiceEnabled() {
    if (!els.voice) return;
    els.voice.disabled = els.engine && els.engine.value === "chatterbox";
  }

  function setStatus(text) {
    if (els.status) els.status.textContent = text;
    if (els.qsStatus) els.qsStatus.textContent = text;
  }

  function currentMode() {
    var r = document.querySelector('input[name="mode"]:checked');
    return r ? r.value : "file";
  }

  function appendLog(line) {
    if (!els.log) return;
    var next = els.log.textContent + line;
    if (next.length > LOG_MAX_CHARS) {
      next = "..." + next.slice(next.length - LOG_MAX_CHARS + 3);
      var i = next.indexOf("\n");
      if (i >= 0 && i < 80) next = next.slice(i + 1);
    }
    els.log.textContent = next;
    els.log.scrollTop = els.log.scrollHeight;
  }

  function resetUI() {
    if (els.log) els.log.textContent = "";
    if (els.progress) els.progress.value = 0;
    if (els.progLabel) els.progLabel.textContent = "0%";
    setStatus("Pronto para iniciar.");
    currentPhase = "";
    lastProg = null;
    if (els.download) els.download.hidden = true;
    if (els.samples) els.samples.innerHTML = '<p class="muted">Nenhuma amostra ainda.</p>';
    if (player) { try { player.destroy(); } catch (e) {} player = null; }
    if (els.video) {
      try { els.video.removeAttribute("src"); els.video.load(); } catch (e) {}
      els.video.hidden = true;
    }
    if (els.previewPlaceholder) els.previewPlaceholder.style.display = "";
    hidePreviewStatus();
    hidePreviewControls();
    jobId = null;
    try { localStorage.removeItem("dublador_last_job_id"); } catch (e) {}
  }

  function applyReset() {
    return fetch("/api/prefs/reset", { method: "POST" })
      .then(function () { return apiInfo(); })
      .then(function (info) {
        var d = info.defaults;
        fillSelect(els.device, info.devices, d.device);
        fillSelect(els.engine, info.engines, d.engine);
        fillSelect(els.lang, info.langs, d.lang);
        fillVoice(info, d.voice);
        fillSelect(els.whisper, info.whisper_models, d.whisper);
        fillSelect(els.parallel, ["1", "2", "4"], d.parallel || "1");
        fillSelect(els.res, info.resolutions, d.res);
        fillSelect(els.qsRes, info.resolutions, d.res);
        fillSelect(els.cookies, info.browsers, d.cookies);
        if (els.volume) els.volume.value = d.volume || "1.0";
        if (els.temp) els.temp.value = d.temp || "";
        if (els.seed) els.seed.value = d.seed || "";
        if (els.maxtempo) els.maxtempo.value = d.maxtempo || "";
        if (els.preview) els.preview.checked = !!d.preview;
        appendLog("[INFO] Opcoes resetadas para os padroes (config limpo).\n");
        setStatus("Opcoes resetadas.");
      })
      .catch(function () {
        setStatus("Falha ao resetar as opcoes.");
      });
  }

  function buildForm() {
    var mode = currentMode();
    if (mode === "youtube" && els.qsUrl && els.qsUrl.value.trim()) {
      els.url.value = els.qsUrl.value.trim();
    }
    if (mode === "youtube" && els.qsRes && els.qsRes.value) {
      els.res.value = els.qsRes.value;
    }
    var fd = new FormData();
    fd.append("mode", mode);
    if (els.device) fd.append("device", els.device.value);
    if (els.engine) fd.append("engine", els.engine.value);
    if (els.lang) fd.append("lang", els.lang.value);
    if (els.voice) fd.append("voice", els.voice.value || "auto");
    if (els.whisper) fd.append("whisper", els.whisper.value);
    if (els.parallel) fd.append("parallel", els.parallel.value || "1");
    if (els.whisperBeam) fd.append("whisper_beam", els.whisperBeam.value || "");
    if (els.volume) fd.append("volume", els.volume.value || "");
    if (els.temp) fd.append("temp", els.temp.value || "");
    if (els.seed) fd.append("seed", els.seed.value || "");
    if (els.maxtempo) fd.append("maxtempo", els.maxtempo.value || "");
    if (els.cookies) fd.append("cookies", els.cookies.value || "");
    fd.append("preview", els.preview && els.preview.checked ? "1" : "0");
    fd.append("samples", els.samplesChk && els.samplesChk.checked ? "1" : "0");
    fd.append("dry", els.dry && els.dry.checked ? "1" : "0");
    if (mode === "youtube") {
      fd.append("url", els.url.value.trim());
      fd.append("res", els.res.value);
    } else {
      if (els.file && els.file.files && els.file.files[0]) fd.append("file", els.file.files[0]);
      fd.append("path", els.path.value.trim());
      if (els.srt && els.srt.files && els.srt.files[0]) fd.append("srt", els.srt.files[0]);
    }
    return fd;
  }

  function qsStart() {
    var yt = document.querySelector('input[name="mode"][value="youtube"]');
    if (yt) yt.checked = true;
    if (els.modeYt) els.modeYt.hidden = false;
    if (els.modeFile) els.modeFile.hidden = true;
    if (els.url && els.qsUrl && els.qsUrl.value.trim()) {
      els.url.value = els.qsUrl.value.trim();
    }
    if (els.res && els.qsRes && els.qsRes.value) {
      els.res.value = els.qsRes.value;
    }
    startJob();
  }

  function startJob() {
    console.log("[qs] startJob clicado, running=", running);
    if (running) { setStatus("Ja existe uma dublagem em andamento."); return; }
    if (els.qsGo) els.qsGo.disabled = true;
    var fd = buildForm();
    if (currentMode() === "youtube" && !fd.get("url")) {
      setStatus("Cole o link do YouTube no campo acima.");
      if (els.qsGo) els.qsGo.disabled = false;
      return;
    }
    if (currentMode() === "file" && (!els.file || !els.file.files[0]) && !els.path.value.trim()) {
      setStatus("Selecione um arquivo ou informe um caminho local.");
      if (els.qsGo) els.qsGo.disabled = false;
      return;
    }

    resetUI();
    setStatus("Enviando...");
    showPreviewStatus("Enviando...", "Preparando o job no servidor");
    appendLog("[OK] Iniciando dublagem (link=" + (fd.get("url") || "").slice(0, 60) + ")\n");
    fetch("/api/jobs", { method: "POST", body: fd })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) {
          setStatus("Erro: " + (res.d.error || "falha ao iniciar"));
          if (els.qsGo) els.qsGo.disabled = false;
          return;
        }
        jobId = res.d.id;
        try { localStorage.setItem("dublador_last_job_id", jobId); } catch (e) {}
        running = true;
        if (els.start) els.start.disabled = true;
        if (els.pause) els.pause.disabled = false;
        if (els.stop) els.stop.disabled = false;
        if (els.start) els.start.textContent = "Dublando...";
        if (els.qsGo) { els.qsGo.textContent = "Dublando..."; els.qsGo.disabled = true; }
        appendLog("[OK] Job iniciado: " + jobId + "\n");
        if (res.d.preview) startPreview("/api/jobs/" + jobId + "/preview");
        connectSSE(jobId);
      })
      .catch(function (err) {
        console.error("[qs] fetch erro:", err);
        setStatus("Erro de rede: " + err);
        if (els.qsGo) els.qsGo.disabled = false;
      });
  }

  function connectSSE(id) {
    if (es) { try { es.close(); } catch (e) {} es = null; }
    es = new EventSource("/api/jobs/" + id + "/stream");
    es.onmessage = function (e) {
      try { handle(JSON.parse(e.data)); }
      catch (err) { /* ignora */ }
    };
    es.onerror = function () {
      if (!running) return;
      try { es.close(); } catch (e) {}
      es = null;
      setTimeout(function () { if (running && jobId === id) connectSSE(id); }, SSE_RETRY_MS);
    };
  }

  function handle(d) {
    switch (d.type) {
      case "log": appendLog(d.line); break;
      case "progress":
        lastProg = d;
        if (els.progress) els.progress.value = Math.round((d.pct || 0) * 100);
        if (els.progLabel) els.progLabel.textContent = Math.round((d.pct || 0) * 100) + "%  (" + d.done + "/" + d.total + ")";
        if (currentPhase) {
          var progText = currentPhase + " - " + d.done + "/" + d.total +
                         " (" + Math.round((d.pct || 0) * 100) + "%)";
          setStatus(progText);
          showPreviewStatus(currentPhase, d.done + " de " + d.total + " trechos dublados", Math.round((d.pct || 0) * 100));
        }
        break;
      case "phase":
        currentPhase = d.label || "";
        if (lastProg) {
          var pt = currentPhase + " - " + lastProg.done + "/" + lastProg.total +
                   " (" + Math.round((lastProg.pct || 0) * 100) + "%)";
          setStatus(pt);
          showPreviewStatus(currentPhase, lastProg.done + " de " + lastProg.total + " trechos", Math.round((lastProg.pct || 0) * 100));
        } else {
          setStatus(currentPhase);
          showPreviewStatus(currentPhase, "Aguarde enquanto o processo avanca", null);
        }
        break;
      case "seg":
        addSample(d);
        hidePreviewStatus();
        showPreviewActive();
        break;
      case "preview_start": startPreview(d.url); break;
      case "preview_restart": restartPreview(d.url); break;
      case "status": finalize(d.status, d.error); break;
    }
  }

  function finalize(status, error) {
    running = false;
    if (es) { try { es.close(); } catch (e) {} es = null; }
    if (els.start) { els.start.disabled = false; els.start.textContent = "Iniciar Dublagem"; }
    if (els.pause) els.pause.disabled = true;
    if (els.stop) els.stop.disabled = true;
    if (els.qsGo) { els.qsGo.textContent = "Iniciar Dublagem"; els.qsGo.disabled = false; }
    currentPhase = "";
    lastProg = null;
    if (status === "done") {
      if (els.progress) els.progress.value = 100;
      if (els.progLabel) els.progLabel.textContent = "100%";
      setStatus("Concluido!");
      if (els.download) els.download.hidden = false;
      hidePreviewStatus();
      try { localStorage.setItem("dublador_last_job_id", jobId); } catch (e) {}
    } else if (status === "cancelled") {
      setStatus("Cancelado.");
      hidePreviewStatus();
      try { localStorage.removeItem("dublador_last_job_id"); } catch (e) {}
    } else {
      setStatus("Erro: " + (error || "falha na dublagem"));
      showPreviewStatus("Erro", (error || "falha na dublagem"));
      try { localStorage.removeItem("dublador_last_job_id"); } catch (e) {}
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

  function showPreviewActive() {
    if (els.previewPlaceholder) els.previewPlaceholder.style.display = "none";
    if (els.video) els.video.hidden = false;
  }

  function showPreviewStatus(label, sub, pct) {
    if (!els.previewStatus) return;
    if (els.previewStatusLabel) els.previewStatusLabel.textContent = label || "Aguardando...";
    if (els.previewStatusSub) els.previewStatusSub.textContent = sub || "";
    if (els.previewStatusBar) {
      var v = (typeof pct === "number" && !isNaN(pct)) ? Math.max(0, Math.min(100, pct)) : null;
      els.previewStatusBar.style.width = v !== null ? (v + "%") : "0%";
    }
    els.previewStatus.classList.add("active");
  }

  function hidePreviewStatus() {
    if (els.previewStatus) els.previewStatus.classList.remove("active");
  }

  function showPreviewControls() {
    if (els.previewControls) els.previewControls.hidden = false;
    updateMuteIcon();
  }

  function hidePreviewControls() {
    if (els.previewControls) els.previewControls.hidden = true;
  }

  function updateMuteIcon() {
    if (!els.video || !els.pcMuteOn || !els.pcMuteOff) return;
    var muted = els.video.muted || (els.video.volume === 0);
    els.pcMuteOn.hidden = !muted;
    els.pcMuteOff.hidden = muted;
  }

  function startPreview(url, seekTo) {
    if (player) return;
    showPreviewActive();
    showPreviewControls();
    if (typeof mpegts === "undefined" || !mpegts.getFeatureList().mseLivePlayback) {
      setStatus("Navegador sem suporte a MSE/mpegts.");
      return;
    }
    try {
      player = mpegts.createPlayer({ type: "mse", isLive: true, url: url });
      player.attachMediaElement(els.video);
      player.load();
      player.play();
      if (seekTo && seekTo > 0) {
        var trySeek = function () {
          try {
            var d = els.video.duration;
            if (!isNaN(d) && isFinite(d) && d > 0) {
              els.video.currentTime = Math.min(seekTo, Math.max(0, d - 0.5));
            }
          } catch (e) {}
        };
        els.video.addEventListener("loadedmetadata", trySeek, { once: true });
        setTimeout(trySeek, 800);
      }
      player.on(mpegts.Events.ERROR, function () {
        if (player) { try { player.destroy(); } catch (e) {} player = null; }
      });
    } catch (err) {
      setStatus("Erro ao abrir o preview: " + err);
    }
  }

  // Reinicia o player quando o backend troca da fase 1 (video original)
  // para a fase 2 (timeline dublada), no primeiro trecho pronto.
  function restartPreview(url) {
    if (player) { try { player.destroy(); } catch (e) {} player = null; }
    startPreview(url, 0);
  }

  function addSample(seg) {
    if (!els.samples) return;
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

  function attachToJob(jid, resumeFrom) {
    jobId = jid;
    try { localStorage.setItem("dublador_last_job_id", jid); } catch (e) {}
    running = true;
    if (els.start) { els.start.disabled = true; els.start.textContent = "Dublando..."; }
    if (els.pause) els.pause.disabled = false;
    if (els.stop) els.stop.disabled = false;
    if (els.qsGo) { els.qsGo.textContent = "Dublando..."; els.qsGo.disabled = true; }
    showPreviewStatus("Dublando...", "Reconectado ao job em andamento no servidor");
    appendLog("[OK] Reconectado ao job " + jid + "\n");
    connectSSE(jid);
    fetch("/api/jobs/" + jid).then(function (r) { return r.json(); }).then(function (info) {
      if (info && info.preview_wanted) {
        var from = (info && info.preview_resume_from) || resumeFrom || 0;
        startPreview("/api/jobs/" + jid + "/preview", from);
      }
    }).catch(function () {});
  }

  function tryReconnect() {
    var saved = null;
    try { saved = localStorage.getItem("dublador_last_job_id"); } catch (e) {}
    if (!saved) return;
    fetch("/api/jobs/" + saved).then(function (r) {
      if (!r.ok) {
        try { localStorage.removeItem("dublador_last_job_id"); } catch (e) {}
        return null;
      }
      return r.json();
    }).then(function (info) {
      if (!info) return;
      if (info.status === "running" || info.status === "queued") {
        attachToJob(saved, info.preview_resume_from || 0);
      } else if (info.status === "done") {
        jobId = saved;
        setStatus("Concluido!");
        if (els.progress) els.progress.value = 100;
        if (els.progLabel) els.progLabel.textContent = "100%";
        if (els.download) els.download.hidden = false;
        hidePreviewStatus();
      } else if (info.status === "error" || info.status === "cancelled") {
        try { localStorage.removeItem("dublador_last_job_id"); } catch (e) {}
        setStatus("Ultimo job: " + info.status + (info.error ? " - " + info.error : ""));
      }
    }).catch(function () {
      try { localStorage.removeItem("dublador_last_job_id"); } catch (e) {}
    });
  }

  // ------------------------------------------------------------------
  function init() {
    apiInfo().then(function (info) {
      setPill(els.pillFfmpeg, info.ffmpeg ? "ffmpeg: instalado" : "ffmpeg: nao encontrado",
              info.ffmpeg ? "ok" : "err");
      setPill(els.pillPreview,
              info.preview ? "preview: disponivel" : "preview: indisponivel",
              info.preview ? "ok" : "err");
      if (!info.ffmpeg && els.banner) els.banner.hidden = false;
      if (!info.preview && els.preview && els.preview.checked) els.preview.checked = false;
      fillSelect(els.device, info.devices, info.defaults.device);
      fillSelect(els.engine, info.engines, info.defaults.engine);
      fillSelect(els.lang, info.langs, info.defaults.lang);
      fillSelect(els.whisper, info.whisper_models, info.defaults.whisper);
      fillVoice(info, info.defaults.voice);
      updateVoiceEnabled();
      fillSelect(els.parallel, ["1", "2", "4"], info.defaults.parallel || "1");
      fillSelect(els.res, info.resolutions, info.defaults.res);
      fillSelect(els.qsRes, info.resolutions, info.defaults.res);
      fillSelect(els.cookies, info.browsers, info.defaults.cookies);
      if (info.defaults.volume && els.volume) els.volume.value = info.defaults.volume;
      if (info.defaults.temp && els.temp) els.temp.value = info.defaults.temp;
      if (info.defaults.seed && els.seed) els.seed.value = info.defaults.seed;
      if (info.defaults.maxtempo && els.maxtempo) els.maxtempo.value = info.defaults.maxtempo;
      if (els.preview) els.preview.checked = !!info.defaults.preview;
      setStatus("Pronto para iniciar.");
    }).catch(function (err) {
      console.error("api/info falhou:", err);
      setStatus("Nao foi possivel carregar /api/info. Usando defaults locais.");
      if (els.qsRes && (!els.qsRes.options || els.qsRes.options.length === 0)) {
        fillSelect(els.qsRes, ["360", "720", "1080"], "720");
      }
      if (els.res && (!els.res.options || els.res.options.length === 0)) {
        fillSelect(els.res, ["360", "720", "1080"], "720");
      }
    });

    document.querySelectorAll('input[name="mode"]').forEach(function (r) {
      r.addEventListener("change", function () {
        var yt = r.value === "youtube";
        if (els.modeYt) els.modeYt.hidden = !yt;
        if (els.modeFile) els.modeFile.hidden = yt;
      });
    });

    if (els.lang) {
      els.lang.addEventListener("change", function () { fillVoice(); });
    }
    if (els.engine) {
      els.engine.addEventListener("change", updateVoiceEnabled);
    }
    updateVoiceEnabled();

    if (els.start) els.start.addEventListener("click", startJob);
    if (els.qsGo) els.qsGo.addEventListener("click", qsStart);
    if (els.pause) els.pause.addEventListener("click", togglePause);
    if (els.stop) els.stop.addEventListener("click", stopJob);
    if (els.download) els.download.addEventListener("click", function (e) {
      if (jobId) { e.preventDefault(); window.location.href = "/api/jobs/" + jobId + "/output"; }
    });
    if (els.reset) els.reset.addEventListener("click", applyReset);

    if (els.pcMute) {
      els.pcMute.addEventListener("click", function () {
        if (!els.video) return;
        els.video.muted = !els.video.muted;
        updateMuteIcon();
      });
    }
    if (els.pcVolume) {
      els.pcVolume.addEventListener("input", function () {
        if (!els.video) return;
        var v = parseInt(els.pcVolume.value, 10) / 100;
        els.video.volume = v;
        if (v > 0 && els.video.muted) els.video.muted = false;
        if (v === 0 && !els.video.muted) els.video.muted = true;
        updateMuteIcon();
      });
    }

    tryReconnect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();