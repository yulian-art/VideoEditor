(() => {
  "use strict";

  const token = new URLSearchParams(location.search).get("token") || "";
  const $ = (id) => document.getElementById(id);
  const refs = Object.fromEntries([
    "projectName", "appStatus", "openProjectBtn", "importCsvBtn", "saveProjectBtn", "exportBtn",
    "undoBtn", "redoBtn", "shortcutsBtn", "boardVideo", "narrationAudio", "emptyPlayer", "captionOverlay",
    "proxyBadge", "playBtn", "stepBackBtn", "stepForwardBtn", "currentTime", "durationTime", "scrubber",
    "loopBtn", "markAudioBtn", "audioMarkerValue", "pairBtn", "boardMarkerValue", "markBoardBtn",
    "zoomOutBtn", "zoomSelect", "zoomInBtn", "centerPlayheadBtn", "timeAxis", "audioTrack", "boardTrack",
    "waveformCanvas", "segmentCanvas", "captionBands", "thumbnailStrip", "anchorLayer", "audioMarkerLine",
    "boardMarkerLine", "timelineTooltip", "viewportLabel", "videoPath", "audioPath", "captionsPath",
    "ffmpegPath", "applySourcesBtn", "deleteAnchorBtn", "anchorInspector", "selectedAnchorId", "anchorTarget",
    "anchorSource", "anchorContent", "anchorOrigin", "anchorEvidence", "anchorLocked", "speedSummary",
    "applyAnchorBtn", "validationCount", "validationList", "anchorCount", "exportCsvBtn", "anchorTable",
    "exportDialog", "outputPath", "browseOutputBtn", "encoderSelect", "exportProgressPanel", "exportState",
    "exportPercent", "exportProgress", "exportMessage", "cancelExportBtn", "startExportBtn", "shortcutsDialog",
    "toastRegion", "timelineShell", "sampleInterval", "refineBudget", "analysisTranscript",
    "analysisOcr", "analysisModel", "analysisSemanticModel", "analysisPython", "analysisPaths", "analysisCrop", "analysisCacheOnly",
    "startAnalysisBtn", "cancelAnalysisBtn", "analysisProgress", "analysisMessage", "analysisSummary",
    "candidateList", "candidateDetails", "reviewList", "reviewSummary", "analysisCacheDir", "framePreview", "cutSourceStart", "cutSourceEnd", "cutTarget", "cutSuspendManual",
    "cutStartFrameBtn", "cutEndFrameBtn", "createCutBtn", "cutList",
    "inspectionPanel", "inspectionStatus", "returnMappingBtn"
  ].map((id) => [id, $(id)]));

  const state = {
    project: null,
    framePreview: false, frameBusy: false, framePending: false, frameLastRequest: 0,
    mediaGeneration: 0, sourceInspection: null, previewSourceOverride: null, displayedSource: null, nativeFrameTime: null,
    projectPath: "",
    selectedId: null,
    audioMarker: null,
    boardMarker: null,
    waveform: [],
    undo: [],
    redo: [],
    dirty: false,
    viewportStart: 0,
    viewportDuration: 120,
    loop: false,
    raf: 0,
    analysisJobId: null,
    analysisPoll: null,
    exportJobId: null,
    exportPoll: null,
    lastThumbnailKey: "",
  };

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-Align-Token", token);
    if (options.body && typeof options.body !== "string") {
      headers.set("Content-Type", "application/json");
      options.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, { ...options, headers });
    const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    if (!response.ok || data.ok === false) throw new Error(data.error || `请求失败 (${response.status})`);
    return data;
  }

  function mediaUrl(path) {
    return `/media?token=${encodeURIComponent(token)}&path=${encodeURIComponent(path)}`;
  }

  function frameUrl(path, time, width = 480, exact = false) {
    const params = new URLSearchParams({ token, path, time: time.toFixed(exact ? 9 : 3), width: String(width) });
    if (refs.ffmpegPath.value) params.set("ffmpeg", refs.ffmpegPath.value);
    return `/api/frame?${params}`;
  }

  function formatTime(seconds) {
    const milliseconds = Math.max(0, Math.round((Number(seconds) || 0) * 1000));
    const hours = Math.floor(milliseconds / 3600000);
    const minutes = Math.floor(milliseconds / 60000) % 60;
    const secs = Math.floor(milliseconds / 1000) % 60;
    const ms = milliseconds % 1000;
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
  }

  function setStatus(text, kind = "ready") {
    refs.appStatus.dataset.state = kind;
    refs.appStatus.lastElementChild.textContent = text;
  }

  function toast(message, kind = "") {
    const item = document.createElement("div");
    item.className = `toast ${kind}`;
    item.textContent = message;
    refs.toastRegion.append(item);
    setTimeout(() => item.remove(), 4300);
  }

  function deepCopy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function uuid() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function duration() {
    return state.project?.anchors?.at(-1)?.target || 0;
  }

  function sortAnchors() {
    state.project.anchors.sort((a, b) => a.target - b.target || a.source - b.source);
  }

  function selectedAnchor() {
    return state.project?.anchors.find((anchor) => anchor.id === state.selectedId) || null;
  }


  function mappingSegments() {
    const anchors = state.project?.anchors || [];
    const events = anchors.map((anchor) => ({ time: anchor.target, kind: 1, value: anchor }));
    for (const cut of state.project?.cuts || []) events.push({ time: cut.target, kind: 0, value: cut });
    events.sort((a, b) => a.time - b.time || a.kind - b.kind);
    const segments = [];
    let current = null;
    for (const event of events) {
      const item = event.value;
      if (event.kind === 0) {
        const left = { target: item.target, source: item.source_start, id: item.id + ":left" };
        const right = { target: item.target, source: item.source_end, id: item.id + ":right" };
        if (current && left.target > current.target + 1e-6) segments.push([current, left]);
        current = item.target >= duration() - 1e-6 ? left : right;
      } else {
        if (current && item.target > current.target + 1e-6) segments.push([current, item]);
        current = item;
      }
    }
    return segments;
  }

  function segmentAt(target) {
    const anchors = state.project?.anchors || [];
    if (!anchors.length) return { source: 0, speed: 0, index: -1 };
    if (target >= duration()) return { source: anchors.at(-1).source, speed: 0, index: anchors.length - 1 };
    const segments = mappingSegments();
    for (let index = segments.length - 1; index >= 0; index -= 1) {
      const [left, right] = segments[index];
      if (target >= left.target) {
        const speed = (right.source - left.source) / (right.target - left.target);
        return { source: left.source + (target - left.target) * speed, speed, index };
      }
    }
    return { source: segments[0]?.[0]?.source ?? anchors[0].source, speed: 0, index: 0 };
  }

  function validate() {
    const anchors = state.project?.anchors || [];
    const issues = [];
    if (anchors.length < 2) return [{ level: "error", index: null, message: "至少需要两个锚点" }];
    if (Math.abs(anchors[0].target) > 1e-6) issues.push({ level: "error", index: 0, message: "第一个录音时间必须为 00:00:00.000" });
    anchors.forEach((anchor, index) => {
      if (![anchor.target, anchor.source].every((value) => Number.isFinite(value) && value >= 0)) {
        issues.push({ level: "error", index, message: `第 ${index + 1} 个锚点时间无效` });
      }
      if (!index) return;
      const previous = anchors[index - 1];
      const dt = anchor.target - previous.target;
      const ds = anchor.source - previous.source;
      if (dt <= 0.001) issues.push({ level: "error", index, message: `第 ${index + 1} 个录音时间必须晚于前一点至少 1 ms` });
      else if (ds < -1e-6) issues.push({ level: "error", index, message: `第 ${index + 1} 个原板书时间倒退` });
      else if (Math.abs(ds) <= 1e-6) issues.push({ level: "info", index, message: `第 ${index}–${index + 1} 点构成 ${dt.toFixed(3)} 秒定格段` });
      else {
        const speed = ds / dt;
        if (speed > 8 || speed < 0.125) issues.push({ level: "warning", index, message: `第 ${index}–${index + 1} 段为 ${speed.toFixed(3)}×，预览使用逐帧定位` });
      }
    });

    const cuts = state.project?.cuts || [];
    if (cuts.length) {
      const errors = issues.filter((item) => item.level === "error");
      issues.splice(0, issues.length, ...errors);
      const sorted = [...cuts].sort((a, b) => a.target - b.target);
      for (let index = 0; index < sorted.length; index += 1) {
        const cut = sorted[index];
        const fail = (message) => issues.push({ level: "error", index: null, message });
        if (![cut.target, cut.source_start, cut.source_end].every((value) => Number.isFinite(value) && value >= 0) ||
            cut.source_end <= cut.source_start || cut.target > duration()) fail("裁剪时间无效");
        if (index && (cut.target <= sorted[index - 1].target ||
            cut.source_start < sorted[index - 1].source_end - 1e-6)) fail("裁剪重叠或顺序冲突");
        for (const anchor of anchors) {
          const tail = Math.abs(cut.target - duration()) < 1e-6;
          const sameTime = Math.abs(anchor.target - cut.target) < 1e-6;
          const boundary = tail ? cut.source_start : cut.source_end;
          const tailBoundary = tail && sameTime && Math.abs(anchor.source - cut.source_start) < 1e-6;
          if (anchor.source >= cut.source_start - 1e-6 && anchor.source < cut.source_end - 1e-6 && !tailBoundary) fail("锚点位于被裁区间：" + anchor.id);
          if (anchor.target < cut.target - 1e-6 && anchor.source > cut.source_start + 1e-6) fail("锚点跨越裁剪左边界");
          if (anchor.target > cut.target + 1e-6 && anchor.source < cut.source_end - 1e-6) fail("锚点跨越裁剪右边界");
          if (sameTime && Math.abs(anchor.source - boundary) > 1e-6) fail("锚点与裁剪同刻冲突");
        }
      }
      for (const [left, right] of mappingSegments()) {
        const speed = (right.source - left.source) / (right.target - left.target);
        if (speed < -1e-6) issues.push({ level: "error", index: null, message: "裁剪后的源时间倒退" });
        else if (speed === 0) issues.push({ level: "info", index: null, message: formatTime(left.target) + "–" + formatTime(right.target) + " 定格" });
        else if (speed > 8 || speed < 0.125) issues.push({ level: "warning", index: null, message: "裁剪分段倍速 " + speed.toFixed(3) + "×" });
      }
    }

    return issues;
  }

  function snapshot() {
    return deepCopy(state.project);
  }

  function commit(before, message) {
    state.undo.push(before);
    if (state.undo.length > 100) state.undo.shift();
    state.redo.length = 0;
    state.dirty = true;
    updateUndoButtons();
    renderAll();
    if (message) toast(message, "good");
  }

  function restoreProject(project) {
    state.project = deepCopy(project);
    const exists = state.project.anchors.some((anchor) => anchor.id === state.selectedId);
    if (!exists) state.selectedId = null;
    state.dirty = true;
    renderAll();
    syncPreview(true);
  }

  function undo() {
    if (!state.undo.length || !state.project) return;
    state.redo.push(snapshot());
    restoreProject(state.undo.pop());
    updateUndoButtons();
  }

  function redo() {
    if (!state.redo.length || !state.project) return;
    state.undo.push(snapshot());
    restoreProject(state.redo.pop());
    updateUndoButtons();
  }

  function updateUndoButtons() {
    refs.undoBtn.disabled = !state.undo.length;
    refs.redoBtn.disabled = !state.redo.length;
  }

  function normalizeProject(project) {
    project.version ||= 1;
    project.name ||= "未命名对齐项目";
    project.video_path ||= "";
    project.audio_path ||= "";
    project.captions_path ||= "";
    project.csv_path ||= "";
    project.fps = Number(project.fps || 30);
    project.anchors = (project.anchors || []).map((anchor, index) => ({
      ...anchor,
      id: anchor.id || uuid(),
      target: Number(anchor.target),
      source: Number(anchor.source),
      content: String(anchor.content || ""),
      evidence: String(anchor.evidence || ""),
      origin: String(anchor.origin || "csv"),
      locked: Boolean(anchor.locked ?? (index === 0 || index === project.anchors.length - 1)),
    }));
    project.captions = (project.captions || []).map((caption) => ({ start: Number(caption.start), end: Number(caption.end), text: String(caption.text || "") }));
    project.cuts = (project.cuts || []).map((cut) => ({
      ...cut, target: Number(cut.target), source_start: Number(cut.source_start), source_end: Number(cut.source_end),
      excluded_anchors: cut.excluded_anchors || [], generated_ids: cut.generated_ids || [],
    }));
    project.editor ||= {};
    return project;
  }

  async function setProject(project, projectPath = "") {
    leaveSourceInspection(false);
    pause();
    state.project = normalizeProject(project);
    state.projectPath = projectPath;
    state.selectedId = null;
    state.audioMarker = null;
    state.boardMarker = null;
    state.waveform = [];
    state.undo = [];
    state.redo = [];
    state.dirty = false;
    state.viewportStart = 0;
    state.viewportDuration = Math.min(120, Math.max(10, duration()));
    refs.zoomSelect.value = duration() <= 120 ? "full" : "120";
    refs.videoPath.value = state.project.video_path;
    refs.audioPath.value = state.project.audio_path;
    refs.captionsPath.value = state.project.captions_path;
    refs.scrubber.max = String(duration());
    refs.scrubber.value = "0";
    refs.durationTime.textContent = formatTime(duration());
    enableProjectControls(true);
    fillAnalysisSettings();
    renderAll();
    if (state.project.video_path && state.project.audio_path) await loadSources(false);
    setStatus("项目已载入", "ready");
  }

  function enableProjectControls(enabled) {
    [refs.saveProjectBtn, refs.exportBtn, refs.exportCsvBtn, refs.applySourcesBtn, refs.createCutBtn, refs.cutStartFrameBtn, refs.cutEndFrameBtn].forEach((button) => { button.disabled = !enabled; });
    [refs.playBtn, refs.stepBackBtn, refs.stepForwardBtn, refs.loopBtn, refs.markAudioBtn, refs.markBoardBtn].forEach((button) => { button.disabled = !enabled; });
  }

  async function choose(kind, save = false, initial = "") {
    const data = await api("/api/dialog", { method: "POST", body: { kind, save, initial } });
    return data.path || "";
  }

  async function importCsv() {
    try {
      const path = await choose("csv");
      if (!path) return;
      setStatus("正在导入 CSV", "busy");
      const data = await api("/api/load-csv", {
        method: "POST",
        body: {
          path,
          video_path: refs.videoPath.value.trim(),
          audio_path: refs.audioPath.value.trim(),
          captions_path: refs.captionsPath.value.trim(),
        },
      });
      await setProject(data.project);
      const freezeCount = data.project.anchors.slice(1).filter((anchor, index) => Math.abs(anchor.source - data.project.anchors[index].source) < 1e-6).length;
      toast(`已导入 ${data.project.anchors.length} 个锚点${freezeCount ? `，识别 ${freezeCount} 个定格段` : ""}`, "good");
    } catch (error) { handleError(error); }
  }

  async function openProject() {
    try {
      const path = await choose("project");
      if (!path) return;
      const data = await api("/api/load-project", { method: "POST", body: { path } });
      await setProject(data.project, path);
      toast("项目已恢复", "good");
    } catch (error) { handleError(error); }
  }

  async function saveProject() {
    if (!state.project) return;
    try {
      const path = state.projectPath || await choose("project", true, `${state.project.name}.align.json`);
      if (!path) return;
      updateProjectPaths();
      const data = await api("/api/save-project", { method: "POST", body: { path, project: state.project } });
      state.projectPath = data.path;
      state.dirty = false;
      renderAll();
      setStatus("项目已保存", "ready");
      toast(`已保存：${data.path}`, "good");
    } catch (error) { handleError(error); }
  }

  async function exportCsv() {
    if (!state.project) return;
    try {
      const path = await choose("csv", true, `${state.project.name}.csv`);
      if (!path) return;
      const data = await api("/api/save-csv", { method: "POST", body: { path, project: state.project } });
      toast(`CSV 已保存：${data.path}`, "good");
    } catch (error) { handleError(error); }
  }

  function updateProjectPaths() {
    if (!state.project) return;
    state.project.video_path = refs.videoPath.value.trim();
    state.project.audio_path = refs.audioPath.value.trim();
    state.project.captions_path = refs.captionsPath.value.trim();
  }

  async function loadSources(showToast = true) {
    if (!state.project) return;
    leaveSourceInspection(false);
    updateProjectPaths();
    if (!state.project.video_path || !state.project.audio_path) {
      toast("请先选择板书视频和旁白录音", "error");
      return;
    }
    try {
      setStatus("正在载入素材", "busy");
      state.mediaGeneration += 1;
      state.framePreview = false;
      state.displayedSource = state.nativeFrameTime = null;
      refs.framePreview.hidden = true;
      refs.boardVideo.hidden = false;
      refs.boardVideo.src = mediaUrl(state.project.video_path);
      refs.narrationAudio.src = mediaUrl(state.project.audio_path);
      refs.boardVideo.load();
      refs.narrationAudio.load();
      refs.emptyPlayer.hidden = true;
      if (state.project.captions_path) {
        const data = await api("/api/load-captions", { method: "POST", body: { path: state.project.captions_path } });
        state.project.captions = data.captions;
      }
      await loadWaveform();
      state.dirty = true;
      renderAll();
      syncPreview(true);
      setStatus("素材已就绪", "ready");
      if (showToast) toast("视频、录音、字幕与波形已载入", "good");
    } catch (error) { handleError(error); }
  }

  async function loadWaveform() {
    const params = new URLSearchParams({ token, path: state.project.audio_path, points: "2400" });
    if (refs.ffmpegPath.value.trim()) params.set("ffmpeg", refs.ffmpegPath.value.trim());
    const data = await api(`/api/waveform?${params}`);
    state.waveform = data.peaks;
  }

  function playPause() {
    if (!state.project?.audio_path) return;
    if (refs.narrationAudio.paused) {
      refs.narrationAudio.play().then(() => {
        refs.playBtn.textContent = "❚❚";
        tick();
      }).catch(handleError);
    } else pause();
  }

  function pause() {
    refs.narrationAudio.pause();
    refs.boardVideo.pause();
    refs.playBtn.textContent = "▶";
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = 0;
    updatePlaybackUI();
  }

  function seek(target, center = false, keepInspection = false) {
    if (!keepInspection) leaveSourceInspection(false);
    const clamped = Math.min(duration(), Math.max(0, Number(target) || 0));
    refs.narrationAudio.currentTime = clamped;
    refs.scrubber.value = String(clamped);
    syncPreview(true);
    updatePlaybackUI();
    if (center) centerViewport(clamped);
  }

  function step(frames) {
    pause();
    seek(refs.narrationAudio.currentTime + frames / (state.project?.fps || 30), true);
  }


  function updateInspectionStatus() {
    const item = state.sourceInspection;
    refs.inspectionPanel.hidden = !item;
    if (!item) return;
    const actual = state.framePreview ? state.displayedSource : state.nativeFrameTime;
    refs.inspectionStatus.textContent = item.label + " · 保存源时间 " + formatTime(item.source) +
      (actual == null ? " · 正在解码" : " · 实际帧 " + formatTime(actual)) +
      (item.contextOnly ? " · 仅作上下文，此处没有对应板书" : " · 原始板书核对中");
    refs.inspectionPanel.dataset.sourceTime = String(item.source);
  }

  function leaveSourceInspection(refresh = true) {
    state.sourceInspection = null;
    state.previewSourceOverride = null;
    refs.inspectionPanel.hidden = true;
    if (refresh) syncPreview(true);
  }

  function inspectSourceAt(source, target, label, contextOnly = false) {
    if (!Number.isFinite(source)) {
      if (Number.isFinite(target)) seek(target, true);
      toast("此项没有对应源帧，请核对录音区间");
      return;
    }
    pause();
    if (Number.isFinite(target)) seek(target, true);
    state.sourceInspection = { source, label, contextOnly };
    state.previewSourceOverride = source;
    updateInspectionStatus();
    syncPreview(true);
  }

  function detectFramePreview() {
    state.framePreview = refs.boardVideo.videoWidth === 0 || !!refs.boardVideo.error;
    refs.framePreview.hidden = !state.framePreview;
    refs.boardVideo.hidden = state.framePreview;
    syncPreview(true);
  }

  async function updateFramePreview(force = false) {
    if (!state.framePreview || !state.project?.video_path) return;
    if (state.frameBusy) { state.framePending ||= force; return; }
    const now = performance.now();
    const target = refs.narrationAudio.currentTime || 0;
    const source = state.previewSourceOverride ?? segmentAt(target).source;
    if (!force && (now - state.frameLastRequest < 500 ||
        (state.displayedSource != null && Math.abs(state.displayedSource - source) < 0.04))) return;
    state.frameBusy = true;
    state.framePending = false;
    state.frameLastRequest = now;
    const generation = state.mediaGeneration;
    try {
      const response = await fetch(frameUrl(state.project.video_path, source, 960, true) + "&exact=1");
      if (!response.ok) throw new Error((await response.json()).error || "预览抽帧失败");
      const actualSource = Number(response.headers.get("X-Source-Time"));
      if (!Number.isFinite(actualSource)) throw new Error("预览未返回有效源帧时间戳");
      const blobUrl = URL.createObjectURL(await response.blob());
      const decoded = new Image();
      decoded.src = blobUrl;
      await decoded.decode();
      if (generation !== state.mediaGeneration) { URL.revokeObjectURL(blobUrl); return; }
      const old = refs.framePreview.src;
      refs.framePreview.src = blobUrl;
      refs.framePreview.dataset.sourceTime = String(actualSource);
      refs.framePreview.dataset.requestedSource = String(source);
      refs.framePreview.hidden = false;
      state.displayedSource = actualSource;
      updateInspectionStatus();
      if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    } catch (error) {
      if (generation === state.mediaGeneration) {
        refs.proxyBadge.textContent = "帧预览失败：" + error.message;
        refs.proxyBadge.hidden = false;
      }
    } finally {
      state.frameBusy = false;
      if (state.framePending || (refs.narrationAudio.paused &&
          Math.abs((state.previewSourceOverride ?? segmentAt(refs.narrationAudio.currentTime || 0).source) - source) > 0.04)) {
        updateFramePreview(true);
      }
    }
  }

  function syncPreview(force = false) {
    if (!state.project || !state.project.video_path) return;
    state.previewSourceOverride = state.sourceInspection?.source ?? null;
    const target = refs.narrationAudio.currentTime || 0;
    const mapped = state.sourceInspection ? { source: state.sourceInspection.source, speed: 0 } : segmentAt(target);
    updateInspectionStatus();
    const extreme = mapped.speed < 0.0625 || mapped.speed > 16 || mapped.speed === 0;
    if (state.framePreview) {
      refs.boardVideo.pause();
      refs.proxyBadge.textContent = "兼容帧预览 · 最高 2 帧/秒";
      refs.proxyBadge.hidden = false;
      updateFramePreview(force);
      return;
    }
    refs.proxyBadge.textContent = "逐帧预览";
    refs.proxyBadge.hidden = !extreme;
    if (force || Math.abs((refs.boardVideo.currentTime || 0) - mapped.source) > (extreme ? 0.025 : 0.14)) {
      try { refs.boardVideo.currentTime = mapped.source; } catch (_) { /* metadata may still be loading */ }
    }
    if (refs.narrationAudio.paused || extreme) {
      refs.boardVideo.pause();
    } else {
      refs.boardVideo.playbackRate = mapped.speed;
      if (refs.boardVideo.paused) refs.boardVideo.play().catch(() => {});
    }
  }

  function captionAt(target) {
    const captions = state.project?.captions || [];
    let low = 0;
    let high = captions.length - 1;
    while (low <= high) {
      const middle = (low + high) >> 1;
      const item = captions[middle];
      if (target < item.start) high = middle - 1;
      else if (target > item.end) low = middle + 1;
      else return item.text;
    }
    return "";
  }

  function tick() {
    if (refs.narrationAudio.paused) return;
    if (state.loop && selectedAnchor()) {
      const anchor = selectedAnchor();
      const start = Math.max(0, anchor.target - 3);
      const end = Math.min(duration(), anchor.target + 3);
      if (refs.narrationAudio.currentTime >= end) refs.narrationAudio.currentTime = start;
    }
    syncPreview(false);
    updatePlaybackUI();
    state.raf = requestAnimationFrame(tick);
  }

  function updatePlaybackUI() {
    const target = refs.narrationAudio.currentTime || 0;
    refs.currentTime.textContent = formatTime(target);
    refs.scrubber.value = String(target);
    refs.captionOverlay.textContent = captionAt(target);
    document.querySelectorAll("[data-playhead]").forEach((line) => positionLine(line, target));
  }

  function markAudio() {
    state.audioMarker = refs.narrationAudio.currentTime || 0;
    refs.audioMarkerValue.textContent = formatTime(state.audioMarker);
    refs.pairBtn.disabled = state.boardMarker == null;
    renderMarkers();
    toast(`已标记录音 ${formatTime(state.audioMarker)}`);
  }

  function markBoard() {
    const displayedSource = state.framePreview ? state.displayedSource :
      (state.nativeFrameTime ?? (!refs.boardVideo.seeking && refs.boardVideo.readyState >= 2 ? refs.boardVideo.currentTime : null));
    if (displayedSource == null || !Number.isFinite(displayedSource)) {
      toast("请等待源画面显示后再标记", "error");
      return;
    }
    const target = refs.narrationAudio.currentTime || 0;
    const mapped = segmentAt(target);
    const referenceSource = state.previewSourceOverride == null && mapped.speed === 0 &&
      Math.abs(displayedSource - mapped.source) < 0.1 ? mapped.source : null;
    state.boardMarker = { source: displayedSource, target, capturedTarget: target, referenceSource };
    refs.boardMarkerValue.textContent = formatTime(displayedSource);
    refs.pairBtn.disabled = state.audioMarker == null;
    renderMarkers();
    toast(`已读取实际显示帧 ${formatTime(displayedSource)}`);
  }

  function pairMarkers() {
    if (state.audioMarker == null || state.boardMarker == null || !state.project) return;
    const before = snapshot();
    const target = state.audioMarker;
    const capturedTarget = state.boardMarker.capturedTarget ?? state.boardMarker.target;
    const source = state.boardMarker.referenceSource ?? state.boardMarker.source;
    const frameMatch = { captured_target: capturedTarget, displayed_frame_pts: state.boardMarker.source, freeze_reference_source: state.boardMarker.referenceSource };
    const existing = state.project.anchors.find((anchor) => Math.abs(anchor.source - source) <= 0.05 &&
      Math.abs(anchor.target - capturedTarget) <= 0.1 && !anchor.locked);
    if (existing) {
      existing.target = target;
      existing.origin = "manual";
      existing.locked = true;
      existing.evidence = pairingEvidence(capturedTarget, target);
      existing.match = { ...(existing.match || {}), ...frameMatch };
    } else {
      const candidate = {
        id: uuid(), target, source, match: frameMatch, content: "人工配对", evidence: pairingEvidence(capturedTarget, target), origin: "manual", locked: true,
      };
      let anchors = [...state.project.anchors, candidate].sort((a, b) => a.target - b.target);
      const conflicts = conflictingAutoAnchors(anchors, candidate.id);
      if (conflicts.length) {
        const remove = confirm(`配对会跨越 ${conflicts.length} 个未锁定自动候选点。是否释放这些候选点后继续？`);
        if (!remove) return;
        const ids = new Set(conflicts.map((anchor) => anchor.id));
        anchors = anchors.filter((anchor) => !ids.has(anchor.id));
      }
      state.project.anchors = anchors;
    }
    sortAnchors();
    const errors = validate().filter((item) => item.level === "error");
    if (errors.length) {
      state.project = before;
      toast(`无法配对：${errors[0].message}`, "error");
      return;
    }
    const paired = state.project.anchors.find((anchor) => Math.abs(anchor.target - target) < 1e-6 && Math.abs(anchor.source - source) < 0.051);
    state.selectedId = paired?.id || null;
    state.audioMarker = null;
    state.boardMarker = null;
    refs.audioMarkerValue.textContent = "—";
    refs.boardMarkerValue.textContent = "—";
    refs.pairBtn.disabled = true;
    commit(before, `配对完成：${formatTime(source)} → ${formatTime(target)}`);
    syncPreview(true);
  }

  function pairingEvidence(boardTarget, audioTarget) {
    const delta = Math.abs(audioTarget - boardTarget);
    if (boardTarget < audioTarget - 0.001) return `人工配对：板书提前 ${delta.toFixed(3)} 秒`;
    if (boardTarget > audioTarget + 0.001) return `人工配对：板书落后 ${delta.toFixed(3)} 秒`;
    return "人工配对：同刻";
  }

  function conflictingAutoAnchors(anchors, candidateId) {
    const candidate = anchors.find((anchor) => anchor.id === candidateId);
    if (!candidate) return [];
    return anchors.filter((anchor) => anchor.id !== candidateId && anchor.origin === "auto" && !anchor.locked &&
      (Math.abs(anchor.target - candidate.target) <= 0.001 ||
       (anchor.target - candidate.target) * (anchor.source - candidate.source) < -1e-6));
  }

  function toggleLoop() {
    state.loop = !state.loop;
    refs.loopBtn.setAttribute("aria-pressed", String(state.loop));
    refs.loopBtn.textContent = state.loop ? "循环已开" : "局部循环";
    if (state.loop && !selectedAnchor()) toast("请先选择一个锚点", "error");
  }

  function viewport() {
    const total = duration();
    if (!state.project || refs.zoomSelect.value === "full") return { start: 0, end: total || 1, span: total || 1 };
    const span = Math.min(total, Number(refs.zoomSelect.value) || state.viewportDuration || 120);
    const start = Math.max(0, Math.min(state.viewportStart, total - span));
    return { start, end: start + span, span };
  }

  function centerViewport(target = refs.narrationAudio.currentTime || 0) {
    if (refs.zoomSelect.value === "full") return renderTimeline();
    const span = Math.min(duration(), Number(refs.zoomSelect.value) || 120);
    state.viewportStart = Math.max(0, Math.min(duration() - span, target - span / 2));
    renderTimeline();
  }

  function xForTime(target) {
    const view = viewport();
    return (target - view.start) / view.span * 100;
  }

  function timeForClientX(clientX, element) {
    const rect = element.getBoundingClientRect();
    const fraction = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const view = viewport();
    return view.start + fraction * view.span;
  }

  function positionLine(element, target) {
    const percent = xForTime(target);
    element.style.left = `${percent}%`;
    element.hidden = percent < 0 || percent > 100;
  }

  function resizeCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round(rect.width * dpr));
    const height = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const context = canvas.getContext("2d");
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { context, width: rect.width, height: rect.height };
  }

  function renderTimeline() {
    renderAxis();
    renderWaveform();
    renderSegments();
    renderCaptionBands();
    renderAnchors();
    renderMarkers();
    renderThumbnails();
    updatePlaybackUI();
    const view = viewport();
    refs.viewportLabel.textContent = `范围：${formatTime(view.start)} – ${formatTime(view.end)}`;
  }

  function renderAxis() {
    refs.timeAxis.replaceChildren();
    const view = viewport();
    const ticks = 7;
    const gridSize = 100 / (ticks - 1);
    refs.timeAxis.style.backgroundSize = `${gridSize}% 100%`;
    [refs.audioTrack, refs.boardTrack].forEach((track) => { track.style.backgroundSize = `${gridSize}% 100%`; });
    for (let index = 0; index < ticks; index += 1) {
      const tick = document.createElement("span");
      tick.className = "axis-tick";
      tick.style.left = `${index / (ticks - 1) * 100}%`;
      tick.textContent = formatTime(view.start + view.span * index / (ticks - 1)).slice(3, -4);
      refs.timeAxis.append(tick);
    }
  }

  function renderWaveform() {
    const { context, width, height } = resizeCanvas(refs.waveformCanvas);
    context.clearRect(0, 0, width, height);
    if (!state.waveform.length || !state.project) return;
    const view = viewport();
    const total = duration();
    context.strokeStyle = "#3987e5";
    context.lineWidth = 1;
    context.beginPath();
    const pixelCount = Math.max(1, Math.floor(width));
    for (let x = 0; x < pixelCount; x += 1) {
      const target = view.start + view.span * x / pixelCount;
      const index = Math.min(state.waveform.length - 1, Math.floor(target / total * state.waveform.length));
      const peak = state.waveform[index] || 0;
      context.moveTo(x + .5, height / 2 - peak * (height * .42));
      context.lineTo(x + .5, height / 2 + peak * (height * .42));
    }
    context.stroke();
  }

  function renderSegments() {
    const { context, width, height } = resizeCanvas(refs.segmentCanvas);
    context.clearRect(0, 0, width, height);
    if (!state.project) return;
    const view = viewport();
    mappingSegments().forEach(([left, right]) => {
      if (right.target < view.start || left.target > view.end) return;
      const start = Math.max(view.start, left.target);
      const end = Math.min(view.end, right.target);
      const x0 = (start - view.start) / view.span * width;
      const x1 = (end - view.start) / view.span * width;
      const speed = (right.source - left.source) / (right.target - left.target);
      context.strokeStyle = speed === 0 ? "#199e70" : "#d95926";
      context.lineWidth = speed === 0 ? 4 : 2;
      context.lineCap = "round";
      context.beginPath();
      context.moveTo(x0, height - 22);
      context.lineTo(x1, height - 22);
      context.stroke();
      if (x1 - x0 > 34) {
        context.fillStyle = "#898781";
        context.font = "9px system-ui";
        context.textAlign = "center";
        context.fillText(speed === 0 ? "定格" : `${speed.toFixed(2)}×`, (x0 + x1) / 2, height - 5);
      }
    });
    for (const cut of state.project.cuts || []) {
      if (cut.target < view.start || cut.target > view.end) continue;
      const x = (cut.target - view.start) / view.span * width;
      context.strokeStyle = "#f27691";
      context.setLineDash([4, 3]);
      context.beginPath(); context.moveTo(x, 0); context.lineTo(x, height); context.stroke();
      context.setLineDash([]);
      context.fillStyle = "#f27691";
      context.font = "10px system-ui";
      context.textAlign = "left";
      context.fillText("裁剪", x + 3, 11);
    }
  }

  function renderCaptionBands() {
    refs.captionBands.replaceChildren();
    if (!state.project) return;
    const view = viewport();
    state.project.captions.filter((caption) => caption.end >= view.start && caption.start <= view.end).slice(0, 500).forEach((caption) => {
      const band = document.createElement("span");
      band.className = "caption-band";
      band.style.left = `${xForTime(Math.max(view.start, caption.start))}%`;
      band.style.width = `${Math.max(.15, xForTime(Math.min(view.end, caption.end)) - xForTime(Math.max(view.start, caption.start)))}%`;
      band.title = `${formatTime(caption.start)} ${caption.text}`;
      refs.captionBands.append(band);
    });
  }

  function renderAnchors() {
    refs.anchorLayer.replaceChildren();
    if (!state.project) return;
    const view = viewport();
    state.project.anchors.forEach((anchor, index) => {
      if (anchor.target < view.start - .001 || anchor.target > view.end + .001) return;
      const button = document.createElement("button");
      button.className = "anchor-button";
      button.type = "button";
      button.style.left = `${xForTime(anchor.target)}%`;
      button.dataset.id = anchor.id;
      button.dataset.origin = anchor.origin;
      button.dataset.locked = String(anchor.locked);
      button.dataset.selected = String(anchor.id === state.selectedId);
      const previous = state.project.anchors[index - 1];
      button.dataset.freeze = String(Boolean(previous && Math.abs(previous.source - anchor.source) < 1e-6));
      button.setAttribute("aria-label", `锚点 ${index + 1}，录音 ${formatTime(anchor.target)}，板书 ${formatTime(anchor.source)}，${anchor.content}`);
      button.title = `${formatTime(anchor.target)} ← ${formatTime(anchor.source)}\n${anchor.content}`;
      button.addEventListener("click", (event) => { event.stopPropagation(); selectAnchor(anchor.id, true); });
      button.addEventListener("pointerdown", (event) => startAnchorDrag(event, anchor));
      button.addEventListener("pointerenter", (event) => showAnchorTooltip(event, anchor, index));
      button.addEventListener("pointerleave", hideTooltip);
      button.addEventListener("focus", (event) => showAnchorTooltip(event, anchor, index));
      button.addEventListener("blur", hideTooltip);
      refs.anchorLayer.append(button);
    });
  }

  function showAnchorTooltip(event, anchor, index) {
    const previous = state.project.anchors[index - 1];
    const next = state.project.anchors[index + 1];
    const rows = [
      anchor.content || `锚点 ${index + 1}`,
      `录音 ${formatTime(anchor.target)}`,
      `板书 ${formatTime(anchor.source)}`,
    ];
    if (previous) rows.push(`左侧 ${speedText(previous, anchor)}`);
    if (next) rows.push(`右侧 ${speedText(anchor, next)}`);
    refs.timelineTooltip.replaceChildren();
    const strong = document.createElement("strong");
    strong.textContent = rows.shift();
    refs.timelineTooltip.append(strong, document.createTextNode(rows.join(" · ")));
    const shellRect = refs.timelineShell.getBoundingClientRect();
    const targetRect = event.currentTarget.getBoundingClientRect();
    refs.timelineTooltip.style.left = `${Math.min(shellRect.width - 270, Math.max(95, targetRect.left - shellRect.left - 70))}px`;
    refs.timelineTooltip.style.top = `${targetRect.top - shellRect.top - 58}px`;
    refs.timelineTooltip.hidden = false;
  }

  function hideTooltip() { refs.timelineTooltip.hidden = true; }

  function startAnchorDrag(event, anchor) {
    event.preventDefault();
    event.stopPropagation();
    if (anchor.locked) { selectAnchor(anchor.id); toast("该人工锚点已锁定；先在检查器中解锁", "error"); return; }
    const before = snapshot();
    const original = anchor.target;
    const index = state.project.anchors.findIndex((item) => item.id === anchor.id);
    const protectedPoints = state.project.anchors.filter((item) =>
      item.id !== anchor.id && (item.origin !== "auto" || item.locked));
    let min = Math.max(0, ...protectedPoints.filter((item) => item.target < original).map((item) => item.target + .001));
    let max = Math.min(duration(), ...protectedPoints.filter((item) => item.target > original).map((item) => item.target - .001));
    for (const cut of state.project.cuts || []) {
      if (anchor.source < cut.source_start) max = Math.min(max, cut.target - .001);
      if (anchor.source >= cut.source_end) min = Math.max(min, cut.target + .001);
    }
    if (index === 0) min = max = 0;
    if (index === state.project.anchors.length - 1) min = max = duration();
    const move = (moveEvent) => {
      let target = timeForClientX(moveEvent.clientX, refs.boardTrack);
      if (!moveEvent.altKey && state.audioMarker != null && Math.abs(target - state.audioMarker) < viewport().span * .015) target = state.audioMarker;
      anchor.target = Math.max(min, Math.min(max, target));
      sortAnchors();
      state.selectedId = anchor.id;
      renderTimeline();
      renderInspector();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (Math.abs(anchor.target - original) > .0005) {
        const conflicts = conflictingAutoAnchors(state.project.anchors, anchor.id);
        if (conflicts.length) {
          if (!confirm("拖动将释放 " + conflicts.length + " 个未锁定自动点，是否提交？")) {
            state.project = before; renderAll(); return;
          }
          const ids = new Set(conflicts.map((item) => item.id));
          state.project.anchors = state.project.anchors.filter((item) => !ids.has(item.id));
        }
        const errors = validate().filter((row) => row.level === "error");
        if (errors.length) { state.project = before; renderAll(); toast(errors[0].message, "error"); return; }
        anchor.origin = "manual"; commit(before, `锚点移至 ${formatTime(anchor.target)}`); }
      else renderTimeline();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up, { once: true });
  }

  function renderMarkers() {
    if (state.audioMarker == null) refs.audioMarkerLine.hidden = true;
    else positionLine(refs.audioMarkerLine, state.audioMarker);
    if (state.boardMarker == null) refs.boardMarkerLine.hidden = true;
    else positionLine(refs.boardMarkerLine, state.boardMarker.target);
  }

  function startBoardMarkerDrag(event) {
    if (!state.boardMarker) return;
    event.preventDefault();
    const move = (moveEvent) => {
      let target = timeForClientX(moveEvent.clientX, refs.boardTrack);
      if (!moveEvent.altKey && state.audioMarker != null && Math.abs(target - state.audioMarker) < viewport().span * .02) target = state.audioMarker;
      state.boardMarker.target = Math.max(0, Math.min(duration(), target));
      renderMarkers();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (state.audioMarker != null && Math.abs(state.boardMarker.target - state.audioMarker) < .001) pairMarkers();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up, { once: true });
  }

  function renderThumbnails() {
    refs.thumbnailStrip.replaceChildren();
    if (!state.project?.video_path) return;
    const view = viewport();
    const count = 7;
    const key = `${state.project.video_path}|${view.start.toFixed(2)}|${view.span.toFixed(2)}`;
    state.lastThumbnailKey = key;
    for (let index = 0; index < count; index += 1) {
      const target = view.start + view.span * (index + .5) / count;
      const source = segmentAt(target).source;
      const tile = document.createElement("div");
      tile.className = "thumbnail-tile";
      tile.style.left = `${index / count * 100}%`;
      tile.style.width = `${100 / count}%`;
      tile.style.backgroundImage = `url("${frameUrl(state.project.video_path, source, 300).replaceAll('"', '%22')}")`;
      refs.thumbnailStrip.append(tile);
    }
  }

  function selectAnchor(id, seekTo = false) {
    state.selectedId = id;
    renderInspector();
    renderAnchors();
    renderTable();
    const anchor = selectedAnchor();
    if (anchor && seekTo) seek(anchor.target, true);
  }

  function renderInspector() {
    const anchor = selectedAnchor();
    refs.anchorInspector.disabled = !anchor;
    refs.deleteAnchorBtn.disabled = !anchor || anchor.locked || state.project.anchors.indexOf(anchor) === 0 || state.project.anchors.indexOf(anchor) === state.project.anchors.length - 1;
    if (!anchor) {
      refs.selectedAnchorId.textContent = "未选择";
      [refs.anchorTarget, refs.anchorSource, refs.anchorContent, refs.anchorEvidence].forEach((input) => { input.value = ""; });
      refs.speedSummary.children[0].textContent = "左侧 —";
      refs.speedSummary.children[1].textContent = "右侧 —";
      return;
    }
    refs.selectedAnchorId.textContent = `ID ${anchor.id}`;
    refs.anchorTarget.value = anchor.target.toFixed(3);
    refs.anchorSource.value = anchor.source.toFixed(3);
    refs.anchorContent.value = anchor.content;
    refs.anchorOrigin.value = anchor.origin;
    refs.anchorEvidence.value = anchor.evidence;
    refs.anchorLocked.checked = anchor.locked;
    const index = state.project.anchors.indexOf(anchor);
    const previous = state.project.anchors[index - 1];
    const next = state.project.anchors[index + 1];
    refs.speedSummary.children[0].textContent = previous ? `左侧 ${speedText(previous, anchor)}` : "左侧 起点";
    refs.speedSummary.children[1].textContent = next ? `右侧 ${speedText(anchor, next)}` : "右侧 终点";
  }

  function speedText(left, right) {
    if ((state.project?.cuts || []).some((cut) => left.target < cut.target && cut.target <= right.target)) return "含裁剪";
    const speed = (right.source - left.source) / (right.target - left.target);
    return speed === 0 ? "定格" : `${speed.toFixed(3)}×`;
  }

  function applyAnchor() {
    const anchor = selectedAnchor();
    if (!anchor) return;
    const before = snapshot();
    anchor.target = Number(refs.anchorTarget.value);
    anchor.source = Number(refs.anchorSource.value);
    anchor.content = refs.anchorContent.value.trim();
    anchor.origin = refs.anchorOrigin.value;
    anchor.evidence = refs.anchorEvidence.value.trim();
    anchor.locked = refs.anchorLocked.checked;
    sortAnchors();
    const errors = validate().filter((item) => item.level === "error");
    if (errors.length) {
      state.project = before;
      renderAll();
      toast(`修改未提交：${errors[0].message}`, "error");
      return;
    }
    commit(before, "数值微调已应用");
    syncPreview(true);
  }

  function deleteAnchor() {
    const anchor = selectedAnchor();
    if (!anchor || anchor.locked) return;
    const before = snapshot();
    state.project.anchors = state.project.anchors.filter((item) => item.id !== anchor.id);
    state.selectedId = null;
    commit(before, "锚点已删除");
  }

  function renderValidation() {
    refs.validationList.replaceChildren();
    if (!state.project) {
      const item = document.createElement("li"); item.className = "muted"; item.textContent = "导入 CSV 后显示检查结果。"; refs.validationList.append(item); return;
    }
    const issues = validate();
    refs.validationCount.textContent = String(issues.length);
    if (!issues.length) {
      const item = document.createElement("li"); item.className = "muted"; item.textContent = "映射顺序、间隔与倍速均通过。"; refs.validationList.append(item); return;
    }
    issues.forEach((issue) => {
      const item = document.createElement("li");
      item.dataset.level = issue.level;
      item.textContent = issue.message;
      if (issue.index != null) item.addEventListener("click", () => selectAnchor(state.project.anchors[issue.index]?.id, true));
      refs.validationList.append(item);
    });
  }

  function renderTable() {
    refs.anchorTable.replaceChildren();
    if (!state.project) {
      const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = 5; cell.className = "empty-cell"; cell.textContent = "尚无数据"; row.append(cell); refs.anchorTable.append(row); return;
    }
    const anchors = state.project.anchors;
    refs.anchorCount.textContent = `${anchors.length} 个锚点`;
    anchors.forEach((anchor, index) => {
      const row = document.createElement("tr");
      row.dataset.selected = String(anchor.id === state.selectedId);
      row.addEventListener("click", () => selectAnchor(anchor.id));
      const number = document.createElement("td"); number.textContent = String(index + 1);
      const target = document.createElement("td");
      const targetButton = document.createElement("button"); targetButton.className = "time-link"; targetButton.textContent = formatTime(anchor.target).slice(3); targetButton.addEventListener("click", (event) => { event.stopPropagation(); seek(anchor.target, true); selectAnchor(anchor.id); }); target.append(targetButton);
      const source = document.createElement("td");
      const sourceButton = document.createElement("button"); sourceButton.className = "source-link"; sourceButton.textContent = formatTime(anchor.source).slice(3); sourceButton.addEventListener("click", (event) => { event.stopPropagation(); inspectSourceAt(anchor.source, anchor.target, "锚点源帧"); selectAnchor(anchor.id); }); source.append(sourceButton);
      const content = document.createElement("td"); content.className = "content-cell"; content.textContent = anchor.content || "—";
      const evidence = document.createElement("span"); evidence.textContent = anchor.evidence || anchor.origin; content.append(evidence);
      if (index && Math.abs(anchor.source - anchors[index - 1].source) < 1e-6) { const tag = document.createElement("em"); tag.className = "freeze-tag"; tag.textContent = "定格"; content.append(tag); }
      const speed = document.createElement("td"); speed.textContent = index < anchors.length - 1 ? speedText(anchor, anchors[index + 1]) : "—";
      row.append(number, target, source, content, speed);
      refs.anchorTable.append(row);
    });
  }

  function renderAll() {
    const project = state.project;
    refs.projectName.textContent = project ? `${project.name}${state.dirty ? " · 未保存" : ""}` : "尚未导入项目";
    refs.durationTime.textContent = formatTime(duration());
    renderInspector();
    renderAnalysis();
    renderCuts();
    renderValidation();
    renderTable();
    renderTimeline();
    updateUndoButtons();
  }

  function changeZoom(direction) {
    const options = ["full", "300", "120", "30", "10"];
    let index = options.indexOf(refs.zoomSelect.value);
    index = Math.max(0, Math.min(options.length - 1, index + direction));
    refs.zoomSelect.value = options[index];
    centerViewport();
  }

  async function startExport() {
    if (!state.project) return;
    const errors = validate().filter((issue) => issue.level === "error");
    if (errors.length) { toast(`导出前请修复：${errors[0].message}`, "error"); return; }
    updateProjectPaths();
    if (!refs.outputPath.value.trim()) { toast("请选择输出 MP4", "error"); return; }
    try {
      refs.exportProgressPanel.hidden = false;
      refs.startExportBtn.disabled = true;
      refs.cancelExportBtn.hidden = false;
      const data = await api("/api/export", {
        method: "POST",
        body: {
          project: state.project,
          output_path: refs.outputPath.value.trim(),
          ffmpeg_path: refs.ffmpegPath.value.trim(),
          encoder: refs.encoderSelect.value,
        },
      });
      state.exportJobId = data.job.id;
      setStatus("正在导出", "busy");
      pollExport();
    } catch (error) { handleError(error); refs.startExportBtn.disabled = false; }
  }

  async function pollExport() {
    if (!state.exportJobId) return;
    try {
      const data = await api(`/api/job?token=${encodeURIComponent(token)}&id=${encodeURIComponent(state.exportJobId)}`);
      const job = data.job;
      refs.exportState.textContent = ({ queued: "排队", running: "视频导出", verifying: "AAC 校验", completed: "完成", failed: "失败", cancelled: "已取消" })[job.state] || job.state;
      refs.exportPercent.textContent = `${Math.round(job.progress * 100)}%`;
      refs.exportProgress.value = job.progress;
      refs.exportMessage.textContent = job.message;
      if (["completed", "failed", "cancelled"].includes(job.state)) {
        state.exportJobId = null;
        refs.startExportBtn.disabled = false;
        refs.cancelExportBtn.hidden = true;
        setStatus(job.state === "completed" ? "导出校验通过" : "导出未完成", job.state === "completed" ? "ready" : "error");
        if (job.state === "completed") toast(`成片、字幕、CSV 已导出；AAC 逐包校验通过`, "good");
        else toast(job.error || job.message, "error");
        return;
      }
      state.exportPoll = setTimeout(pollExport, 700);
    } catch (error) { handleError(error); state.exportPoll = setTimeout(pollExport, 1800); }
  }

  async function cancelExport() {
    if (!state.exportJobId) return;
    try { await api("/api/cancel-job", { method: "POST", body: { id: state.exportJobId } }); }
    catch (error) { handleError(error); }
  }



  function useFrameForCut(field) {
    const source = state.framePreview ? state.displayedSource :
      (state.nativeFrameTime ?? (!refs.boardVideo.seeking ? refs.boardVideo.currentTime : null));
    if (source == null || !Number.isFinite(source)) { toast("请等待源画面显示后再标记", "error"); return; }
    refs[field].value = source.toFixed(6);
  }

  async function createCut() {
    if (!state.project) return;
    const before = snapshot();
    try {
      const data = await api("/api/add-cut", { method: "POST", body: {
        project: state.project, source_start: Number(refs.cutSourceStart.value),
        source_end: Number(refs.cutSourceEnd.value),
        target: refs.cutTarget.value === "" ? null : Number(refs.cutTarget.value),
        manual_policy: refs.cutSuspendManual.checked ? "suspend" : "reject",
      } });
      state.project = normalizeProject(data.project);
      if (!selectedAnchor()) state.selectedId = null;
      commit(before, "裁剪已提交；被裁点已暂存，录音与字幕时间不变");
      pause();
      syncPreview(true);
    } catch (error) { handleError(error); }
  }

  async function restoreCut(id) {
    if (!state.project) return;
    const before = snapshot();
    try {
      const data = await api("/api/remove-cut", { method: "POST", body: { project: state.project, id } });
      state.project = normalizeProject(data.project);
      commit(before, "已恢复源片段及其暂存锚点");
      syncPreview(true);
    } catch (error) { handleError(error); }
  }

  function renderCuts() {
    refs.cutList.replaceChildren();
    const cuts = state.project?.cuts || [];
    if (!cuts.length) { refs.cutList.textContent = "尚无裁剪。"; return; }
    for (const cut of cuts) {
      const item = document.createElement("li");
      item.dataset.cutId = cut.id;
      const locate = document.createElement("button");
      locate.className = "time-link";
      locate.textContent = formatTime(cut.target) + "：源 " + formatTime(cut.source_start) + " → " + formatTime(cut.source_end);
      locate.addEventListener("click", () => seek(cut.target, true));
      const detail = document.createElement("small");
      const archived = cut.excluded_anchors || [];
      detail.textContent = "暂存 " + archived.length + " 个点（人工 " + archived.filter((row) => row.origin !== "auto").length + "）；恢复时保留 ID 和原时间。";
      const restore = document.createElement("button");
      restore.className = "button secondary small";
      restore.dataset.restoreCut = cut.id;
      restore.textContent = "恢复片段与暂存点";
      restore.addEventListener("click", () => restoreCut(cut.id));
      const archivedDetails = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "查看暂存点";
      archivedDetails.append(summary);
      for (const anchor of archived) {
        const row = document.createElement("small");
        row.textContent = (anchor.origin === "auto" ? "自动" : "人工") + " · " + formatTime(anchor.target) +
          " ← " + formatTime(anchor.source) + " · " + (anchor.content || "未命名");
        archivedDetails.append(row);
      }
      item.append(locate, detail, archivedDetails, restore);
      refs.cutList.append(item);
    }
  }

  function analysisSettings() {
    const modulePaths = refs.analysisPaths.value.split(";").map((value) => value.trim()).filter(Boolean);
    const crop = refs.analysisCrop.value.split(",").map(Number);
    return {
      interval: Number(refs.sampleInterval.value), refine_budget: Number(refs.refineBudget.value),
      transcript_path: refs.analysisTranscript.value.trim(), ocr_path: refs.analysisOcr.value.trim(),
      semantic_model: refs.analysisSemanticModel.value.trim(),
      semantic_module_paths: state.project?.analysis?.settings?.semantic_module_paths || [],
      model: refs.analysisModel.value.trim(), python: refs.analysisPython.value.trim(),
      cache_dir: refs.analysisCacheDir.value.trim(), module_paths: modulePaths, crop, cache_only: refs.analysisCacheOnly.checked,
    };
  }

  function fillAnalysisSettings() {
    const settings = state.project?.analysis?.settings;
    if (!settings) return;
    refs.sampleInterval.value = settings.interval ?? 60;
    refs.refineBudget.value = settings.refine_budget ?? 24;
    refs.analysisTranscript.value = settings.transcript_path || "";
    refs.analysisOcr.value = settings.ocr_path || "";
    refs.analysisModel.value = settings.model || "";
    refs.analysisSemanticModel.value = settings.semantic_model || "";
    refs.analysisPython.value = settings.python || "";
    refs.analysisPaths.value = (settings.module_paths || []).join(";");
    refs.analysisCrop.value = (settings.crop || [0, 0, 0, 0]).join(",");
    refs.analysisCacheOnly.checked = !!settings.cache_only;
    refs.analysisCacheDir.value = settings.cache_dir || "";
  }

  async function startAnalysis() {
    if (state.analysisJobId) return;
    const videoPath = refs.videoPath.value.trim();
    const audioPath = refs.audioPath.value.trim();
    if (!videoPath || !audioPath) { toast("请先选择板书视频与旁白录音", "error"); return; }
    try {
      const settings = analysisSettings();
      if (!Number.isFinite(settings.interval) || settings.interval < 1 || settings.interval > 3600 ||
          settings.crop.length !== 4 || settings.crop.some((value) => !Number.isFinite(value) || value < 0)) {
        throw new Error("请填写有效采样间隔和四个非负 OCR 裁边数值");
      }
      pause();
      updateProjectPaths();
      refs.startAnalysisBtn.disabled = true;
      const project = state.project || { name: "自动粗调项目", video_path: videoPath, audio_path: audioPath };
      const data = await api("/api/analyze", {
        method: "POST", body: { project, settings, ffmpeg_path: refs.ffmpegPath.value.trim() },
      });
      state.analysisJobId = data.job.id;
      refs.cancelAnalysisBtn.hidden = false;
      refs.analysisProgress.hidden = false;
      refs.analysisMessage.textContent = data.job.message;
      setStatus("正在自动粗调", "busy");
      await pollAnalysis();
    } catch (error) {
      refs.startAnalysisBtn.disabled = false;
      handleError(error);
    }
  }

  async function pollAnalysis() {
    if (!state.analysisJobId) return;
    try {
      const data = await api("/api/job?id=" + encodeURIComponent(state.analysisJobId));
      const job = data.job;
      refs.analysisProgress.value = job.progress || 0;
      refs.analysisMessage.textContent = job.message + (job.error ? "：" + job.error : "");
      if (job.state === "completed") {
        const before = state.project ? snapshot() : null;
        const merged = await api("/api/apply-analysis", {
          method: "POST", body: { id: state.analysisJobId, project: state.project },
        });
        if (before) {
          state.project = normalizeProject(merged.project);
          if (!selectedAnchor()) state.selectedId = null;
          commit(before, "粗调候选已载入，人工与锁定锚点已保留");
          refs.scrubber.max = String(duration());
          syncPreview(true);
        } else {
          await setProject(merged.project);
          state.dirty = true;
          renderAll();
        }
        refs.analysisMessage.textContent = "已完成。请检查候选和待核对区间；低置信度候选未改变映射。";
        setStatus("粗调完成，待人工核对", "ready");
      } else if (["failed", "cancelled"].includes(job.state)) {
        refs.analysisMessage.textContent += "。恢复项目：" + job.folder + "/project-before.align.json";
        setStatus(job.state === "failed" ? "分析失败，项目保留" : "分析已取消", job.state === "failed" ? "error" : "ready");
      } else {
        state.analysisPoll = setTimeout(pollAnalysis, 1000);
        return;
      }
      state.analysisJobId = null;
      refs.startAnalysisBtn.disabled = false;
      refs.cancelAnalysisBtn.hidden = true;
    } catch (error) {
      refs.analysisMessage.textContent = "读取分析状态失败：" + error.message + "；正在重试。";
      state.analysisPoll = setTimeout(pollAnalysis, 2500);
    }
  }

  async function cancelAnalysis() {
    if (!state.analysisJobId) return;
    try {
      await api("/api/cancel-job", { method: "POST", body: { id: state.analysisJobId } });
      refs.analysisMessage.textContent = "正在取消；已完成的缓存可在下次复用。";
    } catch (error) { handleError(error); }
  }

  function confirmCandidate(candidate) {
    if (!state.project) return;
    const before = snapshot();
    let anchor = state.project.anchors.find((row) => row.id === candidate.id);
    if (!anchor) {
      anchor = { ...deepCopy(candidate), origin: "manual", locked: true };
      state.project.anchors.push(anchor);
    } else {
      anchor.origin = "manual";
      anchor.locked = true;
    }
    sortAnchors();
    const conflicts = conflictingAutoAnchors(state.project.anchors, anchor.id);
    if (conflicts.length) {
      if (!confirm("确认此候选将释放 " + conflicts.length + " 个未锁定自动点，是否继续？")) {
        state.project = before;
        return;
      }
      const ids = new Set(conflicts.map((row) => row.id));
      state.project.anchors = state.project.anchors.filter((row) => !ids.has(row.id));
    }
    const errors = validate().filter((row) => row.level === "error");
    if (errors.length) {
      state.project = before;
      toast("无法确认：" + errors[0].message, "error");
      return;
    }
    state.selectedId = anchor.id;
    state.project.analysis.candidates.find((row) => row.id === candidate.id).status = "confirmed";
    commit(before, "已确认为人工锚点并锁定");
    seek(anchor.target, true);
  }

  function renderAnalysis() {
    const report = state.project?.analysis;
    if (!report?.stats) {
      refs.analysisSummary.textContent = "";
      refs.candidateList.replaceChildren();
      refs.reviewList.textContent = "分析完成后显示。";
      refs.reviewSummary.textContent = "待核对区间";
      return;
    }
    const stats = report.stats;
    const cache = report.cache || {};
    refs.analysisSummary.textContent =
      "采样 " + (report.sampling?.base_times?.length || 0) + " 张，加密 " +
      (report.sampling?.refined_times?.length || 0) + " 张；已合并 " +
      (stats.inserted_candidates ?? stats.accepted_candidates ?? 0) + " 个自动点。复用：ASR " +
      (cache.asr_hits || cache.imported_asr || 0) + " 段，OCR " + (cache.ocr_hits || 0) +
      " 张，缩略图 " + (cache.thumbnail_hits || 0) + " 张；语义向量 " + (cache.semantic_hits || 0) + " 条。" +
      (report.coverage ? " 内容点间插值支撑 " + (100*report.coverage.fraction).toFixed(1) +
       "%（非正确率）；无支撑及缺板书区另列。" : "") +
      (String(report.method || "").includes("兼容模式") ? " 未启用语义模型：词汇兼容模式。" : "") +
      (report.legacy_review_migration ? " 历史核对区间已修正；重新粗调可生成新版候选。" : "");
    refs.candidateList.replaceChildren();
    for (const candidate of report.candidates || []) {
      const item = document.createElement("li");
      const locate = document.createElement("button");
      locate.className = "time-link";
      locate.textContent = formatTime(candidate.target) + " ← " + formatTime(candidate.source) +
        " · 证据评分 " + Math.round(100 * candidate.confidence) + "%";
      locate.dataset.role = "candidate-source";
      locate.dataset.itemId = candidate.id;
      locate.dataset.sourceTime = String(candidate.source);
      locate.addEventListener("click", () => {
        inspectSourceAt(candidate.source, candidate.target, "候选源帧");
        if (state.project.anchors.some((row) => row.id === candidate.id)) selectAnchor(candidate.id);
      });
      const text = document.createElement("span");
      text.textContent = candidate.content;
      const evidence = document.createElement("small");
      evidence.textContent = candidate.evidence + "；录音：" + (candidate.match?.narration || "");
      const accept = document.createElement("button");
      accept.className = "button secondary small";
      const existing = state.project.anchors.find((row) => row.id === candidate.id);
      const cutAway = (state.project.cuts || []).some((cut) => candidate.source >= cut.source_start && candidate.source < cut.source_end);
      accept.textContent = cutAway ? "已随裁剪暂存" : (existing?.locked ? "已确认并锁定" : "确认并锁定");
      accept.disabled = cutAway || !!existing?.locked;
      accept.addEventListener("click", () => confirmCandidate(candidate));
      item.append(locate, text, evidence, accept);
      refs.candidateList.append(item);
    }
    refs.reviewList.replaceChildren();
    const reviews = report.review_intervals || [];
    refs.reviewSummary.textContent = "待核对区间 · " + reviews.filter((row) => row.status !== "resolved").length;
    for (const review of reviews) {
      const item = document.createElement("li");
      const locate = document.createElement("button");
      locate.className = "time-link";
      const hasTarget = Number.isFinite(review.target_start) && Number.isFinite(review.target_end);
      const savedSource = review.inspection_source ?? review.source_end ?? review.source_start ?? review.context_source;
      locate.dataset.role = "review-source";
      locate.dataset.itemId = review.id;
      if (Number.isFinite(savedSource)) locate.dataset.sourceTime = String(savedSource);
      locate.textContent = (hasTarget ? formatTime(review.target_start) + "–" + formatTime(review.target_end) :
        "录音位置待确定") + (Number.isFinite(savedSource) ? " · 查看源帧 " + formatTime(savedSource) : "");
      locate.addEventListener("click", () => inspectSourceAt(savedSource,
        hasTarget ? review.target_start : null, "待核对源帧", !!review.context_only));
      const reason = document.createElement("small");
      reason.textContent = review.reason + (review.source_start != null ?
        "；源画面 " + formatTime(review.source_start) + "–" + formatTime(review.source_end) : "");
      const resolved = document.createElement("button");
      resolved.className = "button secondary small";
      resolved.textContent = review.status === "resolved" ? "重新列为待核对" : "标记已核对";
      resolved.addEventListener("click", () => {
        const before = snapshot();
        review.status = review.status === "resolved" ? "pending" : "resolved";
        commit(before);
      });
      item.append(locate, reason, resolved);
      refs.reviewList.append(item);
    }
  }

  function handleError(error) {
    console.error(error);
    setStatus("发生错误", "error");
    toast(error.message || String(error), "error");
  }

  function isTypingTarget(target) {
    return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || target.isContentEditable;
  }

  refs.cutStartFrameBtn.addEventListener("click", () => useFrameForCut("cutSourceStart"));
  refs.cutEndFrameBtn.addEventListener("click", () => useFrameForCut("cutSourceEnd"));
  refs.createCutBtn.addEventListener("click", createCut);
  refs.startAnalysisBtn.addEventListener("click", startAnalysis);
  refs.cancelAnalysisBtn.addEventListener("click", cancelAnalysis);
  document.querySelectorAll("[data-analysis-browse]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const kind = button.dataset.analysisBrowse;
      const path = await choose(kind);
      if (path) refs[{ ocr: "analysisOcr", model: "analysisModel", python: "analysisPython" }[kind]].value = path;
    } catch (error) { handleError(error); }
  }));
  refs.importCsvBtn.addEventListener("click", importCsv);
  refs.openProjectBtn.addEventListener("click", openProject);
  refs.saveProjectBtn.addEventListener("click", saveProject);
  refs.exportCsvBtn.addEventListener("click", exportCsv);
  refs.applySourcesBtn.addEventListener("click", () => loadSources(true));
  refs.returnMappingBtn.addEventListener("click", () => leaveSourceInspection());
  refs.boardVideo.addEventListener("seeked", updateInspectionStatus);
  refs.playBtn.addEventListener("click", playPause);
  refs.stepBackBtn.addEventListener("click", () => step(-1));
  refs.stepForwardBtn.addEventListener("click", () => step(1));
  refs.scrubber.addEventListener("input", () => seek(Number(refs.scrubber.value)));
  refs.loopBtn.addEventListener("click", toggleLoop);
  refs.markAudioBtn.addEventListener("click", markAudio);
  refs.markBoardBtn.addEventListener("click", markBoard);
  refs.pairBtn.addEventListener("click", pairMarkers);
  refs.undoBtn.addEventListener("click", undo);
  refs.redoBtn.addEventListener("click", redo);
  refs.applyAnchorBtn.addEventListener("click", applyAnchor);
  refs.deleteAnchorBtn.addEventListener("click", deleteAnchor);
  refs.zoomSelect.addEventListener("change", () => centerViewport());
  refs.zoomInBtn.addEventListener("click", () => changeZoom(1));
  refs.zoomOutBtn.addEventListener("click", () => changeZoom(-1));
  refs.centerPlayheadBtn.addEventListener("click", () => centerViewport());
  refs.audioTrack.addEventListener("click", (event) => seek(timeForClientX(event.clientX, refs.audioTrack)));
  refs.boardTrack.addEventListener("click", (event) => seek(timeForClientX(event.clientX, refs.boardTrack)));
  refs.boardMarkerLine.addEventListener("pointerdown", startBoardMarkerDrag);
  refs.boardMarkerLine.style.pointerEvents = "auto";
  refs.boardMarkerLine.style.cursor = "ew-resize";
  refs.shortcutsBtn.addEventListener("click", () => refs.shortcutsDialog.showModal());
  refs.exportBtn.addEventListener("click", () => refs.exportDialog.showModal());
  refs.browseOutputBtn.addEventListener("click", async () => {
    try { const path = await choose("output", true, refs.outputPath.value || `${state.project?.name || "对齐成片"}.mp4`); if (path) refs.outputPath.value = path; } catch (error) { handleError(error); }
  });
  refs.startExportBtn.addEventListener("click", startExport);
  refs.cancelExportBtn.addEventListener("click", cancelExport);
  document.querySelectorAll("[data-browse]").forEach((button) => button.addEventListener("click", async () => {
    const kind = button.dataset.browse;
    const map = { video: refs.videoPath, audio: refs.audioPath, captions: refs.captionsPath, ffmpeg: refs.ffmpegPath };
    try { const path = await choose(kind, false, map[kind].value); if (path) { map[kind].value = path; if (state.project) state.dirty = true; } } catch (error) { handleError(error); }
  }));
  [refs.videoPath, refs.audioPath, refs.captionsPath].forEach((input) => input.addEventListener("change", () => { if (state.project) state.dirty = true; }));
  refs.boardVideo.addEventListener("loadedmetadata", detectFramePreview);
  refs.boardVideo.addEventListener("error", detectFramePreview);
  if (refs.boardVideo.requestVideoFrameCallback) {
    const recordFrame = (_now, metadata) => {
      state.nativeFrameTime = metadata.mediaTime;
      if (state.sourceInspection) updateInspectionStatus();
      refs.boardVideo.requestVideoFrameCallback(recordFrame);
    };
    refs.boardVideo.requestVideoFrameCallback(recordFrame);
  }
  refs.narrationAudio.addEventListener("ended", pause);
  refs.narrationAudio.addEventListener("pause", () => { if (!state.exportJobId) refs.playBtn.textContent = "▶"; });
  refs.narrationAudio.addEventListener("loadedmetadata", () => {
    if (!state.project) return;
    const difference = Math.abs(refs.narrationAudio.duration - duration());
    if (Number.isFinite(difference) && difference > .08) toast(`录音时长与 CSV 终点相差 ${difference.toFixed(3)} 秒`, "error");
  });
  window.addEventListener("resize", () => renderTimeline());
  window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
  document.addEventListener("keydown", (event) => {
    if (isTypingTarget(event.target)) return;
    if (event.ctrlKey && event.key.toLowerCase() === "z") { event.preventDefault(); undo(); return; }
    if (event.ctrlKey && event.key.toLowerCase() === "y") { event.preventDefault(); redo(); return; }
    if (event.key === " ") { event.preventDefault(); playPause(); }
    else if (event.key.toLowerCase() === "a") markAudio();
    else if (event.key.toLowerCase() === "v") markBoard();
    else if (event.key.toLowerCase() === "p") pairMarkers();
    else if (event.key.toLowerCase() === "l") toggleLoop();
    else if (event.key === "ArrowLeft") { event.preventDefault(); step(event.shiftKey ? -(state.project?.fps || 30) : -1); }
    else if (event.key === "ArrowRight") { event.preventDefault(); step(event.shiftKey ? (state.project?.fps || 30) : 1); }
  });

  api("/api/health", { headers: { "X-Align-Token": token } }).then((data) => {
    refs.ffmpegPath.value = data.ffmpeg || "";
    setStatus(data.ffmpeg ? "FFmpeg 已就绪" : "请选择 FFmpeg", data.ffmpeg ? "ready" : "error");
  }).catch(handleError);
  enableProjectControls(false);
  renderAll();
})();
