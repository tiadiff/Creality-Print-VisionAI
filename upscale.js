// Creality Print: Real-ESRGAN (RTX 4070) video enhancement, high-refresh smoothing,
// and a fine-grained print speed slider injected into the original "Printing speed" section.
(function () {
    console.log("[Upscaler] init");

    const WS_URL = "ws://127.0.0.1:8088/ws_upscale";
    const API_URL = "http://127.0.0.1:8088";
    const CTRL_URL = "http://127.0.0.1:8089";
    const MAX_IN_FLIGHT = 3;
    const INPUT_PRESETS = { "480": [480, 270], "640": [640, 360], "960": [960, 540] };

    let ws = null;
    let wsConnected = false;
    let inFlight = 0;
    let sendSeq = 0;
    let lastRecvSeq = -1;

    let activeVideo = null;
    let canvas = null;
    let ctx2d = null;
    let grabCanvas = null;
    let grabCtx = null;
    let uiContainer = null;
    let animId = null;
    let rvfcActive = null;

    // Smoothing state
    let prevBitmap = null;
    let currentBitmap = null;
    let prevTime = 0;
    let currTime = 0;
    let frameDuration = 66.0;
    let lastRenderTimestamp = 0;

    // Telemetry
    let displayFps = 0;
    let detectedMonitorHz = 60;
    let frameCount = 0;
    let lastFpsCalc = performance.now();
    let aiFps = 0;
    let aiFramesCount = 0;
    let srcFps = 0;
    let srcFramesCount = 0;
    let lastAiCalc = performance.now();
    let serverInferMs = 0;
    let serverTotalMs = 0;
    let currentPrintSpeed = 100;

    // Stringing AI state
    let stringingCanvas = null;
    let stringingCtx = null;
    let stringingGrabCanvas = null;
    let stringingGrabCtx = null;
    let stringingBadgeEl = null;
    let stringingAlertEl = null;
    let stringingPostInFlight = false;
    let lastStringingPostAt = 0;
    let stringingStatus = null;
    let stringingConfigSynced = false;
    let alertDismissedAt = 0;
    let lastUiPauseSentAt = -100000;

    // Server lifecycle supervisor state (:8089 / :8088)
    let serverControlStatus = null;
    let serverControlOnline = false;
    let serverControlBusy = false;
    let serverLogsVisible = false;
    let serverLogsLines = [];
    let lastCtrlFetchAt = -100000;
    let ctrlFetchInFlight = false;

    const defaults = {
        enabled: true,
        frameGenEnabled: true,
        targetFps: "auto",
        outRes: "1080p",   // 720p | 1080p | 1440p
        aiInput: "640",    // 480 | 640 | 960
        aiBlend: 1.0,
        hdrShadows: 0.0,   // shadow lift, off by default
        sharpness: 0.35,
        clarity: 0.0,      // display contrast, neutral
        brightness: 0.0,   // display brightness, neutral (-0.5..0.5)
        stringingEnabled: true,
        stringingAutoPause: true,
        stringingSensitivity: "balanced", // low | balanced | high
        serverMode: "auto" // auto | standalone_cpu | full_gpu
    };
    let settings = Object.assign({}, defaults);
    try {
        const saved = localStorage.getItem("creality_ai_upscale_settings_v5");
        if (saved) settings = Object.assign(settings, JSON.parse(saved));
    } catch (e) {}

    function saveSettings() {
        try { localStorage.setItem("creality_ai_upscale_settings_v5", JSON.stringify(settings)); } catch (e) {}
    }

    function sendConfigToService() {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                upscale_enabled: Boolean(settings.enabled),
                target_res: settings.outRes,
                ai_blend: settings.aiBlend,
                hdr_shadows: settings.hdrShadows,
                sharpness: settings.sharpness,
                ip: activePrinterIp || undefined
            }));
        }
    }

    function pushStringingConfigToService() {
        const cfg = {
            enabled: Boolean(settings.stringingEnabled),
            auto_pause: Boolean(settings.stringingAutoPause),
            sensitivity: settings.stringingSensitivity || "balanced"
        };
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                action: "stringing_config",
                ip: activePrinterIp || undefined,
                config: cfg
            }));
            stringingConfigSynced = true;
            return;
        }
        fetch(`${API_URL}/ai/stringing/config`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(cfg)
        }).then((r) => {
            if (r.ok) stringingConfigSynced = true;
        }).catch(() => {});
    }

    // ---------------------------------------------------------------- WebSocket
    function connectAIWebSocket() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
        try {
            ws = new WebSocket(WS_URL);
            ws.binaryType = "arraybuffer";

            ws.onopen = () => {
                wsConnected = true;
                inFlight = 0;
                lastRecvSeq = -1;
                sendConfigToService();
                pushStringingConfigToService();
                ws.send(JSON.stringify({ action: "get_speed", ip: activePrinterIp || undefined }));
                ws.send(JSON.stringify({ action: "get_stringing", ip: activePrinterIp || undefined }));
                updateBadge();
            };

            ws.onmessage = async (event) => {
                if (typeof event.data === "string") {
                    try {
                        const msg = JSON.parse(event.data);
                        if ((msg.type === "speed_update" || msg.type === "speed_ack") && typeof msg.speed === "number") {
                            if (!speedDragging && !isPointerDownOnSpeed) setSliderValue(msg.speed);
                        } else if (msg.type === "telemetry" && msg.data) {
                            handleServiceTelemetry(msg.data);
                        } else if (msg.type === "stringing_status" && msg.data) {
                            handleStringingStatus(msg.data);
                        }
                    } catch (e) {}
                    return;
                }

                if (inFlight > 0) inFlight--;
                if (!settings.enabled) return;

                const dv = new DataView(event.data, 0, 8);
                const seq = dv.getUint32(0, true);
                serverInferMs = dv.getUint16(4, true);
                serverTotalMs = dv.getUint16(6, true);
                if (seq <= lastRecvSeq) return; // late frame, drop
                lastRecvSeq = seq;

                const now = performance.now();
                const newBitmap = await createImageBitmap(new Blob([new Uint8Array(event.data, 8)], { type: "image/jpeg" }));

                if (currentBitmap) {
                    if (prevBitmap) prevBitmap.close();
                    prevBitmap = currentBitmap;
                    prevTime = currTime;
                }
                currentBitmap = newBitmap;
                currTime = now;
                if (prevTime > 0) {
                    const dt = Math.max(8.0, Math.min(250.0, currTime - prevTime));
                    frameDuration = frameDuration * 0.8 + dt * 0.2;
                }

                aiFramesCount++;
                if (now - lastAiCalc >= 1000.0) {
                    const el = (now - lastAiCalc) / 1000.0;
                    aiFps = (aiFramesCount / el).toFixed(1);
                    srcFps = (srcFramesCount / el).toFixed(1);
                    aiFramesCount = 0;
                    srcFramesCount = 0;
                    lastAiCalc = now;
                }
            };

            ws.onclose = () => {
                wsConnected = false;
                inFlight = 0;
                updateBadge();
                setTimeout(connectAIWebSocket, 3000);
            };
            ws.onerror = () => { wsConnected = false; inFlight = 0; updateBadge(); };
        } catch (e) {
            wsConnected = false;
        }
    }

    // ---------------------------------------------------------------- Frame capture
    let lastStringingWsSentAt = 0;
    function sendFrameToAI() {
        if (!wsConnected || inFlight >= MAX_IN_FLIGHT) return;
        if (!activeVideo || activeVideo.paused || activeVideo.videoWidth === 0) return;
        if (ws.bufferedAmount > 8 * 1024 * 1024) return;

        // If neither upscaler nor stringing is active, stop transmitting frames
        if (!settings.enabled && !settings.stringingEnabled) return;

        // If upscaler is OFF and only stringing is ON, throttle frame sending to ~2.5 fps (400ms)
        if (!settings.enabled && settings.stringingEnabled) {
            const now = performance.now();
            if (now - lastStringingWsSentAt < 400) return;
            lastStringingWsSentAt = now;
        }

        const [w, h] = INPUT_PRESETS[settings.aiInput] || INPUT_PRESETS["640"];
        if (!grabCanvas || grabCanvas.width !== w || grabCanvas.height !== h) {
            grabCanvas = document.createElement("canvas");
            grabCanvas.width = w;
            grabCanvas.height = h;
            grabCtx = grabCanvas.getContext("2d", { alpha: false, willReadFrequently: true });
            grabCtx.imageSmoothingQuality = "high";
        }

        try {
            grabCtx.drawImage(activeVideo, 0, 0, w, h);
            const img = grabCtx.getImageData(0, 0, w, h);
            const buf = new ArrayBuffer(8 + img.data.length);
            const dv = new DataView(buf);
            dv.setUint32(0, sendSeq++ >>> 0, true);
            dv.setUint16(4, w, true);
            dv.setUint16(6, h, true);
            new Uint8Array(buf, 8).set(img.data);
            ws.send(buf);
            if (settings.enabled) inFlight++;
        } catch (e) { /* frame skipped */ }
    }

    // Send a frame only when the camera actually delivers a new one.
    function startVideoFrameLoop(video) {
        if (!("requestVideoFrameCallback" in HTMLVideoElement.prototype)) return false;
        if (rvfcActive === video) return true;
        rvfcActive = video;
        const tick = () => {
            if (rvfcActive !== video) return;
            srcFramesCount++;
            sendFrameToAI();
            video.requestVideoFrameCallback(tick);
        };
        video.requestVideoFrameCallback(tick);
        return true;
    }

    // ---------------------------------------------------------------- Render loop
    function renderLoop(renderTimestamp) {
        animId = requestAnimationFrame(renderLoop);
        const now = renderTimestamp || performance.now();

        if (settings.targetFps !== "auto") {
            const rate = parseFloat(settings.targetFps);
            if (rate > 0 && now - lastRenderTimestamp < (1000.0 / rate) - 1.5) return;
        }
        lastRenderTimestamp = now;

        frameCount++;
        if (now - lastFpsCalc >= 1000.0) {
            const raw = frameCount / ((now - lastFpsCalc) / 1000.0);
            displayFps = raw.toFixed(1);
            frameCount = 0;
            lastFpsCalc = now;
            if (raw >= 50 && raw <= 360) detectedMonitorHz = Math.round(raw);
            updateHud();
            updateBadge();
        }

        if (!(settings.enabled && wsConnected)) {
            if (activeVideo) activeVideo.style.opacity = "1";
            if (canvas) canvas.style.display = "none";
            if (!rvfcActive && settings.stringingEnabled) sendFrameToAI();
            return;
        }

        if (!rvfcActive) sendFrameToAI(); // browsers without requestVideoFrameCallback

        if (!(canvas && ctx2d && currentBitmap && activeVideo)) return;

        canvas.style.display = "block";
        activeVideo.style.opacity = "0";

        const rect = activeVideo.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        const tw = Math.round(rect.width * dpr);
        const th = Math.round(rect.height * dpr);
        if (canvas.width !== tw || canvas.height !== th) {
            canvas.width = tw;
            canvas.height = th;
        }
        ctx2d.imageSmoothingEnabled = true;
        ctx2d.imageSmoothingQuality = "high";

        const b = settings.brightness, c = settings.clarity;
        canvas.style.filter = (b === 0 && c === 0)
            ? "none"
            : `contrast(${Math.round((1 + c * 0.4) * 100)}%) brightness(${Math.round((1 + b) * 100)}%)`;

        // Cross-fade smoothing between the two latest AI frames. It is not motion
        // compensated, so skip it when frames are far apart (it would only ghost).
        if (settings.frameGenEnabled && prevBitmap && frameDuration < 120.0) {
            let alpha = Math.min(1.0, Math.max(0.0, (now - currTime) / frameDuration));
            ctx2d.globalAlpha = 1.0;
            ctx2d.drawImage(prevBitmap, 0, 0, canvas.width, canvas.height);
            ctx2d.globalAlpha = alpha;
            ctx2d.drawImage(currentBitmap, 0, 0, canvas.width, canvas.height);
            ctx2d.globalAlpha = 1.0;
        } else {
            ctx2d.globalAlpha = 1.0;
            ctx2d.drawImage(currentBitmap, 0, 0, canvas.width, canvas.height);
        }
    }

    function getRenderedVideoBox(videoEl, hostRect) {
        const vw = (videoEl && videoEl.videoWidth) || hostRect.width || 16;
        const vh = (videoEl && videoEl.videoHeight) || hostRect.height || 9;
        const cw = hostRect.width || 1;
        const ch = hostRect.height || 1;
        const vidAr = vw / vh;
        const boxAr = cw / ch;
        let rw = cw, rh = ch, rx = 0, ry = 0;
        if (Math.abs(vidAr - boxAr) > 0.01) {
            if (boxAr > vidAr) {
                rh = ch;
                rw = ch * vidAr;
                rx = (cw - rw) * 0.5;
            } else {
                rw = cw;
                rh = cw / vidAr;
                ry = (ch - rh) * 0.5;
            }
        }
        return { x: rx, y: ry, w: rw, h: rh };
    }

    function drawStringingOverlay() {
        if (!stringingCanvas || !stringingCtx || !activeVideo) return;
        const rect = activeVideo.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;

        const dpr = window.devicePixelRatio || 1;
        const tw = Math.round(rect.width * dpr);
        const th = Math.round(rect.height * dpr);
        if (stringingCanvas.width !== tw || stringingCanvas.height !== th) {
            stringingCanvas.width = tw;
            stringingCanvas.height = th;
        }
        stringingCtx.clearRect(0, 0, tw, th);

        if (!settings.stringingEnabled || !stringingStatus || !stringingStatus.ok) return;
        const regions = stringingStatus.regions;
        const state = stringingStatus.ai_state;
        if (!Array.isArray(regions) || !regions.length) return;
        if (state !== "warning" && state !== "paused_by_ai" && state !== "alert_only" && Number(stringingStatus.score || 0) < 0.45) {
            return;
        }

        const box = getRenderedVideoBox(activeVideo, rect);
        const isDanger = state === "paused_by_ai" || state === "alert_only" || Number(stringingStatus.ema_score || 0) >= Number(stringingStatus.threshold || 0.55);
        const stroke = isDanger ? "#EF4444" : "#F59E0B";
        const fill = isDanger ? "rgba(239, 68, 68, 0.12)" : "rgba(245, 158, 11, 0.10)";

        stringingCtx.save();
        stringingCtx.scale(dpr, dpr);
        regions.forEach((r) => {
            const x = box.x + Number(r.x || 0) * box.w;
            const y = box.y + Number(r.y || 0) * box.h;
            const w = Number(r.w || 0) * box.w;
            const h = Number(r.h || 0) * box.h;
            if (w < 8 || h < 8) return;

            stringingCtx.fillStyle = fill;
            stringingCtx.fillRect(x, y, w, h);

            stringingCtx.strokeStyle = stroke;
            stringingCtx.lineWidth = 1.8;
            stringingCtx.strokeRect(x, y, w, h);

            // Corner HUD ticks
            const c = Math.min(12, w * 0.25, h * 0.25);
            stringingCtx.lineWidth = 2.8;
            stringingCtx.beginPath();
            stringingCtx.moveTo(x, y + c); stringingCtx.lineTo(x, y); stringingCtx.lineTo(x + c, y);
            stringingCtx.moveTo(x + w - c, y); stringingCtx.lineTo(x + w, y); stringingCtx.lineTo(x + w, y + c);
            stringingCtx.moveTo(x, y + h - c); stringingCtx.lineTo(x, y + h); stringingCtx.lineTo(x + c, y + h);
            stringingCtx.moveTo(x + w - c, y + h); stringingCtx.lineTo(x + w, y + h); stringingCtx.lineTo(x + w, y + h - c);
            stringingCtx.stroke();

            const tag = `Stringing ${Math.round(Number(r.score || 0) * 100)}%`;
            stringingCtx.font = '600 11px "Segoe UI", sans-serif';
            const twLbl = stringingCtx.measureText(tag).width + 10;
            const ly = y > 20 ? y - 18 : y + 4;
            stringingCtx.fillStyle = isDanger ? "rgba(185, 28, 28, 0.90)" : "rgba(180, 83, 9, 0.88)";
            stringingCtx.fillRect(x, ly, twLbl, 16);
            stringingCtx.fillStyle = "#FFFFFF";
            stringingCtx.fillText(tag, x + 5, ly + 12);
        });
        stringingCtx.restore();
    }

    function sendUiFallbackPause(addr) {
        const now = performance.now();
        if ((now - lastUiPauseSentAt) < 10000) return;
        if (ws9999State.stateCode === 5) return; // already paused
        const resolvedAddr = addr || findPrinterAddress(activeVideo) || findPrinterAddress(sliderEl);
        if (!resolvedAddr) return;
        const sock = openPrinterSocket(resolvedAddr);
        const sendPause = (s) => {
            try {
                s.send(JSON.stringify({ method: "set", params: { pause: 1 } }));
                lastUiPauseSentAt = performance.now();
            } catch (e) {}
        };
        if (sock && sock.readyState === WebSocket.OPEN) {
            sendPause(sock);
        } else if (sock && sock.readyState === WebSocket.CONNECTING) {
            sock.addEventListener("open", () => sendPause(sock), { once: true });
        }
    }

    function handleStringingStatus(data) {
        if (!data || typeof data !== "object") return;
        stringingStatus = data;

        // If backend confirmed auto-pause and asks UI socket to pause as fallback/sync
        if (data.auto_pause_triggered && data.should_pause_ui) {
            sendUiFallbackPause(data.ip);
        }

        drawStringingOverlay();
        updateStringingUI();
        updateBadge();
    }

    function formatUptimeSec(sec) {
        const s = Math.max(0, Math.floor(Number(sec || 0)));
        if (s < 60) return `${s}s`;
        const m = Math.floor(s / 60);
        const r = s % 60;
        if (m < 60) return `${m}m ${String(r).padStart(2, "0")}s`;
        const h = Math.floor(m / 60);
        return `${h}h ${String(m % 60).padStart(2, "0")}m`;
    }

    async function fetchServerLogs() {
        if (!serverControlOnline) {
            serverLogsLines = [];
            updateServerControlUI();
            return;
        }
        try {
            const res = await fetch(`${CTRL_URL}/control/logs?lines=30`);
            if (res.ok) {
                const j = await res.json();
                serverLogsLines = Array.isArray(j.lines) ? j.lines : [];
            }
        } catch (e) {}
        updateServerControlUI();
    }

    async function pollServerControlStatus(force = false) {
        if (ctrlFetchInFlight) return;
        const now = performance.now();
        const panelOpen = Boolean(uiContainer && uiContainer.querySelector("#aiPanel.open"));
        const interval = serverControlBusy ? 450 : (panelOpen ? 1200 : 3000);
        if (!force && (now - lastCtrlFetchAt) < interval) return;

        ctrlFetchInFlight = true;
        lastCtrlFetchAt = now;
        try {
            const ctrl = new AbortController();
            const tid = setTimeout(() => ctrl.abort(), 1400);
            try {
                const r = await fetch(`${CTRL_URL}/control/status`, { signal: ctrl.signal });
                if (r.ok) {
                    const j = await r.json();
                    serverControlOnline = Boolean(j && j.supervisor_online);
                    serverControlStatus = j;
                    updateServerControlUI();
                    updateBadge();
                    renderTelemetryCard();
                    return;
                }
            } catch (e) {}
            finally {
                clearTimeout(tid);
            }

            // Supervisor (:8089) offline — check if :8088 was started manually via .bat
            serverControlOnline = false;
            const ctrl2 = new AbortController();
            const tid2 = setTimeout(() => ctrl2.abort(), 900);
            try {
                const r2 = await fetch(`${API_URL}/status`, { signal: ctrl2.signal });
                if (r2.ok) {
                    const j2 = await r2.json();
                    serverControlStatus = {
                        ok: true,
                        supervisor_online: false,
                        ai_running: true,
                        port_8088_listening: true,
                        managed_by_supervisor: false,
                        ai_mode: j2.mode || (j2.telemetry_only ? "standalone_cpu" : "full_gpu"),
                        ai_pid: j2.pid || null,
                        uptime_sec: j2.uptime_sec || 0,
                        has_full_gpu: Boolean(j2.gpu_upscaler),
                        can_stringing_ai: Boolean(j2.stringing_available),
                        last_error: ""
                    };
                } else {
                    serverControlStatus = null;
                }
            } catch (e2) {
                serverControlStatus = null;
            } finally {
                clearTimeout(tid2);
            }
            updateServerControlUI();
            updateBadge();
            renderTelemetryCard();
        } finally {
            ctrlFetchInFlight = false;
        }
    }

    async function controlServerAction(action, modeOverride) {
        if (serverControlBusy) return;
        const mode = modeOverride || settings.serverMode || "auto";
        serverControlBusy = true;
        updateServerControlUI();
        renderTelemetryCard();

        try {
            if (serverControlOnline) {
                const res = await fetch(`${CTRL_URL}/control/${action}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ mode })
                });
                const json = await res.json().catch(() => null);
                if (json) {
                    serverControlStatus = json;
                    serverControlOnline = json.supervisor_online !== undefined ? Boolean(json.supervisor_online) : serverControlOnline;
                }
            } else if (action === "stop") {
                await fetch(`${API_URL}/server/stop`, { method: "POST" }).catch(() => {});
                serverControlStatus = null;
            }

            if (action === "stop") {
                try { if (ws) ws.close(); } catch (e) {}
                wsConnected = false;
                serviceTelemetryOk = false;
                serviceTelemetry = null;
                stringingStatus = null;
                stringingConfigSynced = false;
                drawStringingOverlay();
                updateStringingUI();
            } else {
                stringingConfigSynced = false;
                pollServiceTelemetry(true);
                if (settings.enabled) connectAIWebSocket();
            }
        } catch (e) {}
        finally {
            serverControlBusy = false;
            await pollServerControlStatus(true);
            if (serverLogsVisible) fetchServerLogs();
        }
    }

    function updateServerControlUI() {
        if (!uiContainer) return;
        const q = (s) => uiContainer.querySelector(s);
        const pill = q("#srvPill");
        if (!pill) return;

        const st = serverControlStatus;
        const running = Boolean(st && (st.ai_running || st.port_8088_listening));
        const starting = Boolean(st && st.starting);
        const supOn = Boolean(serverControlOnline);
        const modeEl = q("#hudSrvMode");
        const pidEl = q("#hudSrvPid");
        const upEl = q("#hudSrvUptime");
        const capsEl = q("#hudSrvCaps");
        const selEl = q("#srvModeSelect");
        const btnStart = q("#btnSrvStart");
        const btnStop = q("#btnSrvStop");
        const btnRestart = q("#btnSrvRestart");
        const errBox = q("#srvErrBox");
        const logsBox = q("#srvLogsBox");

        if (serverControlBusy) {
            pill.className = "agy-srv-pill warn";
            pill.textContent = "Operazione in corso...";
        } else if (starting) {
            pill.className = "agy-srv-pill warn";
            pill.textContent = "Avvio server (:8088)...";
        } else if (running) {
            const isGpu = st.ai_mode === "full_gpu";
            pill.className = isGpu ? "agy-srv-pill ok" : "agy-srv-pill cpu";
            pill.textContent = isGpu ? "Attivo \u00b7 Full GPU" : "Attivo \u00b7 CPU AI";
        } else if (supOn) {
            pill.className = "agy-srv-pill off";
            pill.textContent = "Spento \u00b7 Pronto";
        } else {
            pill.className = "agy-srv-pill off";
            pill.textContent = "Controller :8089 offline";
        }

        if (st) {
            const modeLabels = {
                full_gpu: "Real-ESRGAN + Stringing",
                standalone_cpu: "Telemetria + Stringing",
                external: "Esterno (.bat)",
                stopped: "Fermo"
            };
            modeEl.textContent = modeLabels[st.ai_mode] || st.ai_mode || "-";
            pidEl.textContent = st.ai_pid ? `PID ${st.ai_pid}` : (running ? ":8088" : "-");
            upEl.textContent = running ? formatUptimeSec(st.uptime_sec) : "-";
            capsEl.textContent = st.has_full_gpu
                ? "GPU DirectML + Stringing"
                : (st.can_stringing_ai ? "Solo CPU (no .venv GPU)" : "Dipendenze mancanti");
        } else {
            modeEl.textContent = "-";
            pidEl.textContent = "-";
            upEl.textContent = "-";
            capsEl.textContent = "Avvia server_manager.py / .bat";
        }

        if (selEl) {
            const gpuOpt = selEl.querySelector('option[value="full_gpu"]');
            if (gpuOpt) gpuOpt.disabled = Boolean(st && st.has_full_gpu === false);
            selEl.disabled = serverControlBusy || !supOn;
        }

        if (btnStart) {
            btnStart.disabled = serverControlBusy || running || !supOn;
            btnStart.textContent = serverControlBusy && !running ? "Avvio..." : "Avvia";
        }
        if (btnStop) {
            btnStop.disabled = serverControlBusy || !running;
            btnStop.textContent = serverControlBusy && running ? "Arresto..." : "Ferma";
        }
        if (btnRestart) {
            btnRestart.disabled = serverControlBusy || !supOn;
        }

        if (errBox) {
            const errTxt = (st && st.last_error) ? String(st.last_error) : (!supOn && !running ? "Esegui install_upscaler.ps1 o Avvia_AI_Upscaler.bat per abilitare il controller :8089." : "");
            if (errTxt) {
                errBox.style.display = "block";
                errBox.textContent = errTxt;
            } else {
                errBox.style.display = "none";
            }
        }

        if (logsBox) {
            logsBox.style.display = serverLogsVisible ? "block" : "none";
            if (serverLogsVisible) {
                logsBox.textContent = serverLogsLines.length
                    ? serverLogsLines.join("\n")
                    : "Nessun log disponibile.";
                logsBox.scrollTop = logsBox.scrollHeight;
            }
        }
    }

    function updateStringingUI() {
        const st = stringingStatus;
        const badge = stringingBadgeEl;
        if (badge) {
            const dot = badge.querySelector(".agy-str-dot");
            const txt = badge.querySelector(".agy-str-txt");
            const bar = badge.querySelector(".agy-str-bar-fill");

            if (!settings.stringingEnabled) {
                badge.className = "agy-str-badge muted";
                txt.textContent = "Stringing AI · Off";
                if (bar) bar.style.width = "0%";
            } else if (!st || !st.ok) {
                badge.className = "agy-str-badge muted";
                txt.textContent = "Stringing AI · :8088 offline";
                if (bar) bar.style.width = "0%";
            } else {
                const state = st.ai_state;
                const conf = Math.round(Number(st.ema_score || st.score || 0) * 100);
                const cpct = Math.round(Number(st.confirm_pct || 0) * 100);
                if (bar) bar.style.width = `${Math.max(0, Math.min(100, cpct))}%`;

                if (state === "paused_by_ai") {
                    badge.className = "agy-str-badge danger";
                    txt.textContent = `Stampa in pausa · Stringing ${conf}%`;
                } else if (state === "alert_only") {
                    badge.className = "agy-str-badge danger";
                    txt.textContent = `Stringing rilevato · ${conf}%`;
                } else if (state === "warning") {
                    badge.className = "agy-str-badge warn";
                    txt.textContent = `Sospetto stringing ${conf}% · ${Number(st.streak_sec || 0).toFixed(1)}s`;
                } else if (state === "calibrating") {
                    badge.className = "agy-str-badge calib";
                    txt.textContent = "Stringing AI · Calibrazione...";
                } else if (state === "monitoring") {
                    badge.className = "agy-str-badge ok";
                    txt.textContent = conf >= 15 ? `Stringing AI · Attiva (${conf}%)` : "Stringing AI · Attiva";
                } else if (state === "paused") {
                    badge.className = "agy-str-badge muted";
                    txt.textContent = "Stringing AI · Stampante in pausa";
                } else {
                    badge.className = "agy-str-badge muted";
                    txt.textContent = "Stringing AI · Standby";
                }
                if (dot) dot.style.opacity = "1";
            }
        }

        // Update AI Panel drawer stats if mounted
        if (uiContainer) {
            const elState = uiContainer.querySelector("#hudStrState");
            const elConf = uiContainer.querySelector("#hudStrConf");
            const elLat = uiContainer.querySelector("#hudStrLat");
            const elStreak = uiContainer.querySelector("#hudStrStreak");
            if (elState && st && st.ok) {
                const map = {
                    monitoring: "Attiva (Pulita)",
                    calibrating: "Calibrazione scena",
                    warning: "Sospetto stringing",
                    paused_by_ai: "Pausa Automatica",
                    alert_only: "Avviso Stringing",
                    paused: "Stampante in pausa",
                    standby: "In attesa stampa",
                    disabled: "Disattivata",
                };
                elState.textContent = map[st.ai_state] || st.ai_state;
                const rawFrameScore = st.frame_score !== undefined ? st.frame_score : st.score;
                elConf.textContent = `${Math.round(Number(st.ema_score || 0) * 100)}% (frame ${Math.round(Number(rawFrameScore || 0) * 100)}%)`;
                elLat.textContent = `${Number(st.avg_infer_ms || 0).toFixed(1)} ms`;
                elStreak.textContent = `${Number(st.streak_sec || 0).toFixed(1)} / ${Number(st.confirm_seconds || 4.5).toFixed(1)}s`;
            } else if (elState) {
                elState.textContent = settings.stringingEnabled ? "Servizio :8088 offline" : "Disattivata";
                elConf.textContent = "-";
                elLat.textContent = "-";
                elStreak.textContent = "-";
            }
        }

        // Alert banner when auto-paused or alert fired
        if (stringingAlertEl) {
            const alert = st && st.alert;
            const show = Boolean(
                st && st.ok && alert &&
                (st.ai_state === "paused_by_ai" || st.ai_state === "alert_only") &&
                Number(alert.timestamp || 0) > alertDismissedAt
            );
            if (show) {
                stringingAlertEl.style.display = "block";
                const titleEl = stringingAlertEl.querySelector(".agy-str-alert-title");
                const descEl = stringingAlertEl.querySelector(".agy-str-alert-desc");
                const imgEl = stringingAlertEl.querySelector(".agy-str-alert-img");

                titleEl.textContent = alert.paused
                    ? "Stampa messa in pausa dall'AI — Stringing rilevato"
                    : "Allarme Stringing rilevato dall'AI";

                const parts = [`Confidenza ${alert.confidence_pct || 0}%`];
                if (alert.layer !== null && alert.layer !== undefined) parts.push(`Layer ${alert.layer}`);
                if (alert.feature_label) parts.push(alert.feature_label);
                if (alert.time_str) parts.push(alert.time_str);
                descEl.textContent = parts.join(" · ");

                const ipQ = st.ip ? `ip=${encodeURIComponent(st.ip)}&` : "";
                const snapKey = `${alert.timestamp}`;
                if (imgEl.dataset.snapKey !== snapKey) {
                    imgEl.dataset.snapKey = snapKey;
                    imgEl.src = `${API_URL}/ai/stringing/snapshot?${ipQ}alert_only=1&t=${Math.floor(Number(alert.timestamp || Date.now()) * 1000)}`;
                    imgEl.style.display = "block";
                }
            } else {
                stringingAlertEl.style.display = "none";
            }
        }
    }

    function pollStandaloneStringingFrame() {
        if (!settings.stringingEnabled || stringingPostInFlight) return;
        // When WebSocket is open and connected, live frames are already transmitted over WS
        if (wsConnected) return;
        if (!activeVideo || activeVideo.paused || activeVideo.readyState < 2 || activeVideo.videoWidth === 0) return;

        const now = performance.now();
        if ((now - lastStringingPostAt) < 440) return;
        lastStringingPostAt = now;

        const w = 480;
        const h = Math.max(64, Math.round(activeVideo.videoHeight * (w / activeVideo.videoWidth)));
        if (!stringingGrabCanvas || stringingGrabCanvas.width !== w || stringingGrabCanvas.height !== h) {
            stringingGrabCanvas = document.createElement("canvas");
            stringingGrabCanvas.width = w;
            stringingGrabCanvas.height = h;
            stringingGrabCtx = stringingGrabCanvas.getContext("2d", { alpha: false });
        }

        stringingPostInFlight = true;
        try {
            stringingGrabCtx.drawImage(activeVideo, 0, 0, w, h);
            stringingGrabCanvas.toBlob((blob) => {
                if (!blob) {
                    stringingPostInFlight = false;
                    return;
                }
                const addr = findPrinterAddress(activeVideo) || findPrinterAddress(sliderEl);
                const ipQ = addr ? `?ip=${encodeURIComponent(addr)}` : "";
                if (!stringingConfigSynced) pushStringingConfigToService();
                fetch(`${API_URL}/ai/stringing/analyze${ipQ}`, {
                    method: "POST",
                    body: blob
                })
                    .then((r) => (r.ok ? r.json() : null))
                    .then((json) => { if (json && json.ok) handleStringingStatus(json); })
                    .catch(() => {})
                    .finally(() => { stringingPostInFlight = false; });
            }, "image/jpeg", 0.85);
        } catch (e) {
            stringingPostInFlight = false;
        }
    }

    function attachToVideo(videoEl) {
        if (activeVideo === videoEl && canvas && canvas.parentElement) return;
        activeVideo = videoEl;
        rvfcActive = null;

        const parent = videoEl.parentElement;
        if (!parent) return;
        if (getComputedStyle(parent).position === "static") parent.style.position = "relative";

        const addr = findPrinterAddress(videoEl);
        if (addr) openPrinterSocket(addr);

        if (!canvas) {
            canvas = document.createElement("canvas");
            canvas.id = "crealityUpscaleCanvas";
            canvas.style.cssText = "position:absolute;top:0;left:0;width:100%;height:100%;border-radius:inherit;pointer-events:none;z-index:5;";
            ctx2d = canvas.getContext("2d", { alpha: false });
        }
        if (canvas.parentElement !== parent) parent.appendChild(canvas);

        if (!stringingCanvas) {
            stringingCanvas = document.createElement("canvas");
            stringingCanvas.id = "agyStringingCanvas";
            stringingCanvas.style.cssText = "position:absolute;top:0;left:0;width:100%;height:100%;border-radius:inherit;pointer-events:none;z-index:6;";
            stringingCtx = stringingCanvas.getContext("2d");
        }
        if (stringingCanvas.parentElement !== parent) parent.appendChild(stringingCanvas);

        createUI(parent);
        connectAIWebSocket();
        startVideoFrameLoop(videoEl);

        if (animId) cancelAnimationFrame(animId);
        animId = requestAnimationFrame(renderLoop);
        drawStringingOverlay();

        if (!videoEl.__agyResizeObserved && typeof ResizeObserver !== "undefined") {
            videoEl.__agyResizeObserved = true;
            new ResizeObserver(() => { drawStringingOverlay(); }).observe(videoEl);
        }
    }

    window.addEventListener("resize", () => { drawStringingOverlay(); positionAIPanel(); });
    window.addEventListener("scroll", () => { positionAIPanel(); }, true);

    function positionAIPanel() {
        if (!uiContainer) return;
        const panel = uiContainer.querySelector("#aiPanel");
        const badge = uiContainer.querySelector("#aiBadge");
        if (!panel || !badge || !panel.classList.contains("open")) return;
        const r = badge.getBoundingClientRect();
        const top = Math.max(8, Math.round(r.bottom + 8));
        const right = Math.max(8, Math.round(window.innerWidth - r.right));
        const maxW = Math.min(650, Math.max(320, window.innerWidth - 24));
        panel.style.position = "fixed";
        panel.style.top = `${top}px`;
        panel.style.right = `${right}px`;
        panel.style.width = `${maxW}px`;
        panel.style.zIndex = "99999";
        panel.style.maxHeight = `${Math.max(220, window.innerHeight - top - 16)}px`;
    }

    function openAIPanel(toggle = false) {
        if (!uiContainer) return;
        const panel = uiContainer.querySelector("#aiPanel");
        if (!panel) return;
        const willOpen = toggle ? !panel.classList.contains("open") : true;
        if (willOpen) {
            panel.classList.add("open");
            positionAIPanel();
            pollServerControlStatus(true);
        } else {
            panel.classList.remove("open");
        }
    }

    // ---------------------------------------------------------------- AI panel
    function createUI(parent) {
        if (uiContainer && uiContainer.parentElement) uiContainer.parentElement.removeChild(uiContainer);

        uiContainer = document.createElement("div");
        uiContainer.id = "crealityUpscaleUI";
        uiContainer.style.cssText = `position:absolute;top:10px;right:12px;z-index:999;font-family:"Segoe UI",-apple-system,BlinkMacSystemFont,Roboto,sans-serif;font-size:12px;user-select:none;`;

        const sel = (v, cur) => (v === cur ? "selected" : "");
        const pct = (v) => Math.round(v * 100);
        const slider = (id, label, value, extra = "") => `
            <div class="ai-row">
                <div class="ai-row-head"><span>${label}</span><span class="ai-val" id="val_${id}">${value}%</span></div>
                <input type="range" class="ai-range" id="slider_${id}" min="0" max="100" step="1" value="${value}" style="--v:${value}%" ${extra}>
            </div>`;

        uiContainer.innerHTML = `
            <style>
                #crealityUpscaleUI, #crealityUpscaleUI * { box-sizing: border-box; }
                #aiBadge{display:inline-flex;align-items:center;gap:8px;padding:6px 13px;background:rgba(24,24,26,.88);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.12);border-radius:8px;color:#e8e8ea;cursor:pointer;font-weight:500;letter-spacing:.2px;box-shadow:0 2px 10px rgba(0,0,0,.35);transition:background .15s,border-color .15s}
                #aiBadge:hover{background:rgba(36,36,39,.94);border-color:rgba(255,255,255,.24)}
                #aiBadge .dot{width:6px;height:6px;border-radius:50%;background:#e8e8ea}
                #aiBadge.offline .dot{background:transparent;border:1px solid #8e8e93}
                #aiBadge.offline{color:#a1a1a6}
                #aiBadge.disabled .dot{background:transparent;border:1px solid #636366}
                #aiBadge.disabled{color:#8e8e93}
                #aiPanel{display:none;position:absolute;top:38px;right:0;width:650px;max-width:calc(100vw - 24px);background:rgba(24,24,26,.97);backdrop-filter:blur(18px);border:1px solid rgba(255,255,255,.10);border-radius:12px;padding:16px 18px;color:#e8e8ea;box-shadow:0 14px 36px rgba(0,0,0,.55);max-height:85vh;overflow-y:auto}
                #aiPanel.open{display:block}
                #aiPanel::-webkit-scrollbar{width:4px}
                #aiPanel::-webkit-scrollbar-thumb{background:#3a3a3d;border-radius:4px}
                .ai-grid{display:grid;grid-template-columns:1.02fr .98fr;gap:16px;align-items:start}
                .ai-col{display:flex;flex-direction:column;min-width:0}
                @media (max-width:680px){.ai-grid{grid-template-columns:1fr}}
                .ai-head{display:flex;justify-content:space-between;align-items:center;padding-bottom:12px;margin-bottom:12px;border-bottom:1px solid rgba(255,255,255,.08)}
                .ai-title{font-size:13px;font-weight:600;letter-spacing:.3px;color:#f2f2f4}
                .ai-section{font-size:10px;font-weight:600;letter-spacing:1.2px;text-transform:uppercase;color:#8e8e93;margin:16px 0 10px}
                .ai-stats{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.07);border-radius:8px;overflow:hidden}
                .ai-stat{background:#1c1c1e;padding:8px 10px;display:flex;flex-direction:column;gap:2px;color:#8e8e93;font-size:10px}
                .ai-stat b{color:#f2f2f4;font-size:13px;font-weight:600;letter-spacing:.2px}
                .ai-row{margin-bottom:13px}
                .ai-row-head{display:flex;justify-content:space-between;color:#b4b4b9;margin-bottom:7px}
                .ai-val{color:#f2f2f4;font-weight:600;font-variant-numeric:tabular-nums}
                .ai-range{width:100%;-webkit-appearance:none;appearance:none;height:3px;border-radius:2px;outline:none;cursor:pointer;background:linear-gradient(to right,#e8e8ea var(--v,0%),#3a3a3d var(--v,0%))}
                .ai-range::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:13px;height:13px;border-radius:50%;background:#f2f2f4;border:0;box-shadow:0 0 0 3px rgba(242,242,244,.12);cursor:pointer}
                .ai-range::-webkit-slider-thumb:hover{box-shadow:0 0 0 5px rgba(242,242,244,.16)}
                .ai-select{width:100%;background:#2a2a2d;border:1px solid rgba(255,255,255,.10);border-radius:7px;color:#e8e8ea;padding:7px 9px;font-size:12px;outline:none;margin-bottom:12px;cursor:pointer}
                .ai-select:focus{border-color:rgba(255,255,255,.28)}
                .ai-label{color:#b4b4b9;margin-bottom:7px}
                .ai-switch-row{display:flex;justify-content:space-between;align-items:center;gap:12px}
                .ai-switch-row small{display:block;color:#8e8e93;font-size:10px;margin-top:2px;line-height:1.35}
                .ai-switch{-webkit-appearance:none;appearance:none;flex:none;width:30px;height:17px;border-radius:9px;background:#3a3a3d;position:relative;cursor:pointer;transition:background .15s;margin:0}
                .ai-switch::after{content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;background:#8e8e93;transition:transform .15s,background .15s}
                .ai-switch:checked{background:#e8e8ea}
                .ai-switch:checked::after{transform:translateX(13px);background:#1c1c1e}
                .ai-hdr{background:#1c1c1e;border:1px solid rgba(255,255,255,.08);border-radius:9px;padding:12px;margin-bottom:13px}
                .ai-hdr .ai-row{margin:0}
                .ai-hdr-note{color:#8e8e93;font-size:10px;line-height:1.4;margin:0 0 10px}
                .ai-num{display:flex;align-items:center;gap:4px;color:#f2f2f4}
                .ai-num input{width:44px;background:#2a2a2d;border:1px solid rgba(255,255,255,.10);border-radius:5px;color:#f2f2f4;text-align:right;padding:3px 5px;font-size:12px;font-weight:600;outline:none;font-variant-numeric:tabular-nums;-moz-appearance:textfield}
                .ai-num input:focus{border-color:rgba(255,255,255,.3)}
                .ai-num input::-webkit-outer-spin-button,.ai-num input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
                .ai-btn{width:100%;padding:8px;margin-top:6px;background:transparent;border:1px solid rgba(255,255,255,.14);border-radius:7px;color:#b4b4b9;font-size:11px;cursor:pointer;transition:all .15s}
                .ai-btn:hover{border-color:rgba(255,255,255,.3);color:#f2f2f4;background:rgba(255,255,255,.04)}
                .ai-btn-row{display:flex;gap:8px;margin-top:8px}
                .ai-btn-row .ai-btn{margin-top:0;padding:6px 8px}

                /* Server AI lifecycle controller card */
                .agy-srv-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px}
                .agy-srv-pill{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;font-size:10.5px;font-weight:600;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.06);color:#d4d4d8}
                .agy-srv-pill.ok{background:rgba(23,204,95,.14);border-color:rgba(23,204,95,.42);color:#4ADE80}
                .agy-srv-pill.cpu{background:rgba(56,189,248,.14);border-color:rgba(56,189,248,.42);color:#7DD3FC}
                .agy-srv-pill.warn{background:rgba(245,158,11,.14);border-color:rgba(245,158,11,.45);color:#FCD34D}
                .agy-srv-pill.off{background:rgba(142,142,147,.14);border-color:rgba(142,142,147,.32);color:#a1a1a6}
                .ai-btn-primary{background:#17CC5F;border-color:#17CC5F;color:#08160d;font-weight:700}
                .ai-btn-primary:hover:not(:disabled){background:#22e06d;border-color:#22e06d;color:#08160d}
                .ai-btn-danger{background:rgba(239,68,68,.16);border-color:rgba(239,68,68,.45);color:#FCA5A5;font-weight:600}
                .ai-btn-danger:hover:not(:disabled){background:rgba(239,68,68,.28);border-color:rgba(239,68,68,.75);color:#FEE2E2}
                .ai-btn:disabled{opacity:.42;cursor:not-allowed}
                .agy-srv-err{display:none;margin-top:8px;padding:6px 8px;border-radius:6px;background:rgba(239,68,68,.14);border:1px solid rgba(239,68,68,.35);color:#FCA5A5;font-size:10px;line-height:1.35;word-break:break-word}
                .agy-srv-logs{display:none;margin-top:8px;padding:7px 8px;border-radius:6px;background:#111113;border:1px solid rgba(255,255,255,.08);color:#a1a1aa;font-family:Consolas,monospace;font-size:9.5px;line-height:1.35;max-height:110px;overflow-y:auto;white-space:pre-wrap;word-break:break-word}

                /* Stringing AI left badge & camera alert card */
                #agyStringingHost{position:absolute;top:10px;left:12px;z-index:998;display:flex;flex-direction:column;gap:8px;pointer-events:none;font-family:"Segoe UI",-apple-system,BlinkMacSystemFont,Roboto,sans-serif}
                .agy-str-badge{pointer-events:auto;position:relative;overflow:hidden;display:inline-flex;align-items:center;gap:7px;padding:6px 12px;background:rgba(24,24,26,.88);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.12);border-radius:8px;color:#e8e8ea;cursor:pointer;font-size:12px;font-weight:500;box-shadow:0 2px 10px rgba(0,0,0,.35);width:fit-content}
                .agy-str-badge:hover{background:rgba(36,36,39,.94);border-color:rgba(255,255,255,.24)}
                .agy-str-dot{width:7px;height:7px;border-radius:50%;background:#17CC5F;flex:none}
                .agy-str-badge.calib .agy-str-dot{background:#38BDF8}
                .agy-str-badge.warn{border-color:rgba(245,158,11,.55);color:#FDE68A}
                .agy-str-badge.warn .agy-str-dot{background:#F59E0B}
                .agy-str-badge.danger{border-color:rgba(239,68,68,.75);background:rgba(69,10,10,.92);color:#FEE2E2}
                .agy-str-badge.danger .agy-str-dot{background:#EF4444}
                .agy-str-badge.muted{color:#a1a1a6}
                .agy-str-badge.muted .agy-str-dot{background:transparent;border:1px solid #8e8e93}
                .agy-str-bar{position:absolute;left:0;right:0;bottom:0;height:2px;background:rgba(255,255,255,.06)}
                .agy-str-bar-fill{height:100%;width:0%;background:#F59E0B;transition:width .25s ease}
                .agy-str-badge.danger .agy-str-bar-fill{background:#EF4444}

                #agyStringingAlert{display:none;pointer-events:auto;width:320px;max-width:calc(100vw - 48px);background:rgba(24,20,22,.96);backdrop-filter:blur(16px);border:1px solid rgba(239,68,68,.65);border-radius:10px;padding:12px;color:#f4f4f6;box-shadow:0 12px 32px rgba(0,0,0,.6)}
                .agy-str-alert-title{font-size:12.5px;font-weight:700;color:#FCA5A5;margin-bottom:4px}
                .agy-str-alert-desc{font-size:11px;color:#d4d4d8;margin-bottom:8px;line-height:1.35}
                .agy-str-alert-img{width:100%;max-height:145px;object-fit:cover;border-radius:6px;border:1px solid rgba(255,255,255,.1);margin-bottom:8px;display:none}
                .agy-str-alert-actions{display:flex;gap:8px}
                .agy-str-alert-actions button{flex:1;padding:6px 8px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;border:1px solid rgba(255,255,255,.14);background:#27272a;color:#f4f4f6}
                .agy-str-alert-actions button.primary{background:#17CC5F;border-color:#17CC5F;color:#0b1810}
            </style>

            <div id="aiBadge"><span class="dot"></span><span id="badgeText">AI</span></div>

            <div id="aiPanel">
                <div class="ai-head">
                    <div>
                        <span class="ai-title">Enhancement AI &amp; Protezione</span>
                        <div style="font-size:10.5px;color:#8e8e93;margin-top:2px">Server locale, monitoraggio cinematico e super-risoluzione</div>
                    </div>
                    <div style="display:flex;align-items:center;gap:8px">
                        <span style="font-size:11px;color:#a1a1a6">Upscaler Video</span>
                        <input type="checkbox" class="ai-switch" id="aiToggle" ${settings.enabled ? "checked" : ""}>
                    </div>
                </div>

                <div class="ai-grid">
                    <div class="ai-col">
                        <div class="ai-section" style="margin-top:0">Server AI (:8088)</div>
                        <div class="ai-hdr">
                            <div class="agy-srv-head">
                                <span style="color:#f2f2f4;font-weight:600">Stato Servizio</span>
                                <span id="srvPill" class="agy-srv-pill off">Controllo...</span>
                            </div>
                            <div class="ai-stats" style="margin-bottom:10px">
                                <div class="ai-stat">Modalit&agrave;<b id="hudSrvMode">-</b></div>
                                <div class="ai-stat">Processo<b id="hudSrvPid">-</b></div>
                                <div class="ai-stat">Attivo da<b id="hudSrvUptime">-</b></div>
                                <div class="ai-stat">Capacit&agrave;<b id="hudSrvCaps">-</b></div>
                            </div>
                            <div class="ai-label">Modalit&agrave; di avvio</div>
                            <select id="srvModeSelect" class="ai-select" style="margin-bottom:8px">
                                <option value="auto" ${sel("auto", settings.serverMode)}>Auto (GPU se disp., altrimenti CPU)</option>
                                <option value="standalone_cpu" ${sel("standalone_cpu", settings.serverMode)}>Solo Telemetria + Stringing AI (CPU)</option>
                                <option value="full_gpu" ${sel("full_gpu", settings.serverMode)}>Full Real-ESRGAN + Stringing (GPU)</option>
                            </select>
                            <div class="ai-btn-row">
                                <button type="button" class="ai-btn ai-btn-primary" id="btnSrvStart">Avvia</button>
                                <button type="button" class="ai-btn ai-btn-danger" id="btnSrvStop">Ferma</button>
                                <button type="button" class="ai-btn" id="btnSrvRestart">Riavvia</button>
                                <button type="button" class="ai-btn" id="btnSrvLogs" style="flex:0 0 46px">Log</button>
                            </div>
                            <div id="srvErrBox" class="agy-srv-err"></div>
                            <pre id="srvLogsBox" class="agy-srv-logs"></pre>
                        </div>

                        <div class="ai-section">Rilevamento Stringing (AI)</div>
                        <div class="ai-hdr">
                            <div class="ai-switch-row" style="margin-bottom:10px">
                                <div><span style="color:#f2f2f4;font-weight:600">Protezione Stringing AI</span><small>Attiva indipendentemente dall'upscaler</small></div>
                                <input type="checkbox" class="ai-switch" id="strToggle" ${settings.stringingEnabled ? "checked" : ""}>
                            </div>
                            <div class="ai-switch-row" style="margin-bottom:10px">
                                <div>Pausa automatica stampa<small>Invia PAUSA immediata alla conferma</small></div>
                                <input type="checkbox" class="ai-switch" id="strAutoPauseToggle" ${settings.stringingAutoPause ? "checked" : ""}>
                            </div>
                            <div class="ai-label">Sensibilit&agrave; rilevamento</div>
                            <select id="strSensSelect" class="ai-select" style="margin-bottom:8px">
                                <option value="low" ${sel("low", settings.stringingSensitivity)}>Conservativa (pochi falsi allarmi &middot; 6s)</option>
                                <option value="balanced" ${sel("balanced", settings.stringingSensitivity)}>Bilanciata (consigliata &middot; 4.5s)</option>
                                <option value="high" ${sel("high", settings.stringingSensitivity)}>Sensibile (intervento rapido &middot; 3s)</option>
                            </select>
                            <div class="ai-stats">
                                <div class="ai-stat">Stato<b id="hudStrState">-</b></div>
                                <div class="ai-stat">Rischio<b id="hudStrConf">-</b></div>
                                <div class="ai-stat">Conferma<b id="hudStrStreak">-</b></div>
                                <div class="ai-stat">Analisi<b id="hudStrLat">- ms</b></div>
                            </div>
                            <div class="ai-btn-row">
                                <button class="ai-btn" id="btnStrReset">Ricalibra scena</button>
                            </div>
                        </div>
                    </div>

                    <div class="ai-col">
                        <div class="ai-section" style="margin-top:0">Prestazioni Video &amp; FPS</div>
                        <div class="ai-stats" style="margin-bottom:12px">
                            <div class="ai-stat">Camera<b id="hudSrcFps">${srcFps} fps</b></div>
                            <div class="ai-stat">AI<b id="hudAiFps">${aiFps} fps</b></div>
                            <div class="ai-stat">GPU / totale<b id="hudLat">- ms</b></div>
                            <div class="ai-stat">Display<b id="hudDisplayFps">${displayFps} fps</b></div>
                            <div class="ai-stat" style="grid-column:span 2">Limite attuale fps AI<b id="hudLimit">-</b></div>
                        </div>

                        <div class="ai-section">Parametri Immagine</div>
                        <div class="ai-hdr" style="margin-bottom:10px">
                            <div class="ai-row-head" style="margin-bottom:4px">
                                <span style="color:#f2f2f4;font-weight:600">HDR ombre</span>
                                <span class="ai-num"><input type="number" id="num_hdr" min="0" max="100" step="1" value="${pct(settings.hdrShadows)}">%</span>
                            </div>
                            <p class="ai-hdr-note" style="margin:0 0 6px">Schiarisce i neri (PLA scuro) senza bruciare le luci.</p>
                            <input type="range" class="ai-range" id="slider_hdr" min="0" max="100" step="1" value="${pct(settings.hdrShadows)}" style="--v:${pct(settings.hdrShadows)}%">
                        </div>
                        ${slider("sharp", "Nitidezza", pct(settings.sharpness))}
                        ${slider("blend", "Intensit&agrave; AI", pct(settings.aiBlend))}
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
                            <div>${slider("clarity", "Contrasto", pct(settings.clarity))}</div>
                            <div>${slider("bright", "Luminosit&agrave;", pct(settings.brightness + 0.5))}</div>
                        </div>

                        <div class="ai-section">Qualit&agrave; &amp; Fluidit&agrave;</div>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:6px">
                            <div>
                                <div class="ai-label">Input AI</div>
                                <select id="inputSelect" class="ai-select" style="margin-bottom:6px">
                                    <option value="480" ${sel("480", settings.aiInput)}>480 &times; 270</option>
                                    <option value="640" ${sel("640", settings.aiInput)}>640 &times; 360</option>
                                    <option value="960" ${sel("960", settings.aiInput)}>960 &times; 540</option>
                                </select>
                            </div>
                            <div>
                                <div class="ai-label">Output</div>
                                <select id="resSelect" class="ai-select" style="margin-bottom:6px">
                                    <option value="720p" ${sel("720p", settings.outRes)}>720p HD</option>
                                    <option value="1080p" ${sel("1080p", settings.outRes)}>1080p FHD</option>
                                    <option value="1440p" ${sel("1440p", settings.outRes)}>1440p QHD</option>
                                </select>
                            </div>
                        </div>

                        <div class="ai-switch-row" style="margin-bottom:8px">
                            <div>Smussamento dissolvenza<small>Tra frame AI consecutivi</small></div>
                            <input type="checkbox" class="ai-switch" id="fgToggle" ${settings.frameGenEnabled ? "checked" : ""}>
                        </div>
                        <select id="fpsSelect" class="ai-select" style="margin-bottom:10px">
                            <option value="auto" ${sel("auto", settings.targetFps)}>Frequenza display (auto)</option>
                            <option value="60" ${sel("60", settings.targetFps)}>Limita a 60 Hz</option>
                            <option value="75" ${sel("75", settings.targetFps)}>Limita a 75 Hz</option>
                            <option value="120" ${sel("120", settings.targetFps)}>Limita a 120 Hz</option>
                            <option value="144" ${sel("144", settings.targetFps)}>Limita a 144 Hz</option>
                        </select>

                        <button class="ai-btn" id="btnReset" style="margin-top:4px">Ripristina predefiniti</button>
                    </div>
                </div>
            </div>
        `;
        parent.appendChild(uiContainer);

        const oldStrHost = parent.querySelector("#agyStringingHost");
        if (oldStrHost) oldStrHost.remove();
        const strHost = document.createElement("div");
        strHost.id = "agyStringingHost";
        strHost.innerHTML = `
            <div class="agy-str-badge muted" title="Clicca per impostazioni AI Stringing">
                <span class="agy-str-dot"></span>
                <span class="agy-str-txt">Stringing AI · Attiva</span>
                <div class="agy-str-bar"><div class="agy-str-bar-fill"></div></div>
            </div>
            <div id="agyStringingAlert">
                <div class="agy-str-alert-title"></div>
                <div class="agy-str-alert-desc"></div>
                <img class="agy-str-alert-img" alt="Snapshot stringing AI" onerror="this.style.display='none'">
                <div class="agy-str-alert-actions">
                    <button type="button" class="primary" id="btnAlertReset">Pulisci &amp; Riarma AI</button>
                    <button type="button" id="btnAlertDismiss">Chiudi</button>
                </div>
            </div>
        `;
        parent.appendChild(strHost);
        stringingBadgeEl = strHost.querySelector(".agy-str-badge");
        stringingAlertEl = strHost.querySelector("#agyStringingAlert");

        const q = (id) => uiContainer.querySelector(id);
        const badge = q("#aiBadge"), panel = q("#aiPanel"), badgeText = q("#badgeText");

        stringingBadgeEl.addEventListener("click", (e) => {
            e.stopPropagation();
            openAIPanel(false);
        });
        strHost.querySelector("#btnAlertReset").addEventListener("click", (e) => {
            e.stopPropagation();
            const addr = findPrinterAddress(activeVideo) || findPrinterAddress(sliderEl);
            const ipQ = addr ? `?ip=${encodeURIComponent(addr)}` : "";
            fetch(`${API_URL}/ai/stringing/reset${ipQ}`, { method: "POST" })
                .then((r) => (r.ok ? r.json() : null))
                .then((json) => { if (json && json.ok) handleStringingStatus(json); })
                .catch(() => {});
        });
        strHost.querySelector("#btnAlertDismiss").addEventListener("click", (e) => {
            e.stopPropagation();
            if (stringingStatus && stringingStatus.alert && stringingStatus.alert.timestamp) {
                alertDismissedAt = Number(stringingStatus.alert.timestamp);
            } else {
                alertDismissedAt = Date.now() / 1000;
            }
            updateStringingUI();
        });

        window.__updateUI = function () {
            const st = serverControlStatus;
            const srvRunning = Boolean(wsConnected || (st && (st.ai_running || st.port_8088_listening)));
            if (!settings.enabled) {
                badge.className = "disabled";
                badgeText.textContent = "Upscaler disattivo";
            } else if (wsConnected) {
                badge.className = "";
                badgeText.textContent = `AI \u00b7 ${aiFps} fps`;
            } else if (serverControlOnline && !srvRunning) {
                badge.className = "offline";
                badgeText.textContent = "Server AI \u00b7 Spento";
            } else {
                badge.className = "offline";
                badgeText.textContent = "Upscaler offline";
            }
        };
        window.__updateHUD = function () {
            q("#hudSrcFps").textContent = `${srcFps} fps`;
            q("#hudAiFps").textContent = settings.enabled ? `${aiFps} fps` : "Disattivo";
            q("#hudDisplayFps").textContent = `${displayFps} fps`;
            q("#hudLat").textContent = settings.enabled ? `${serverInferMs} / ${serverTotalMs} ms` : "Nativo";
            // The AI can never run faster than the camera delivers new frames.
            const src = parseFloat(srcFps), ai = parseFloat(aiFps);
            q("#hudLimit").textContent = !settings.enabled ? "Upscaler disattivato (video nativo)"
                : !wsConnected || !(src > 0) ? "-"
                : ai >= src * 0.85 ? `Camera (${srcFps} fps in ingresso)`
                : "Elaborazione GPU/CPU (abbassa la qualità)";
        };

        badge.addEventListener("click", (e) => {
            e.stopPropagation();
            openAIPanel(true);
        });
        document.addEventListener("click", (e) => {
            if (!uiContainer) return;
            const t = e.target;
            if (!uiContainer.contains(t) && !(t && t.closest && (t.closest("#agyStringingHost") || t.closest(".agy-tel-src")))) {
                panel.classList.remove("open");
            }
        });

        q("#srvModeSelect").addEventListener("change", (e) => { settings.serverMode = e.target.value; saveSettings(); });
        q("#btnSrvStart").addEventListener("click", (e) => { e.stopPropagation(); controlServerAction("start"); });
        q("#btnSrvStop").addEventListener("click", (e) => { e.stopPropagation(); controlServerAction("stop"); });
        q("#btnSrvRestart").addEventListener("click", (e) => { e.stopPropagation(); controlServerAction("restart"); });
        q("#btnSrvLogs").addEventListener("click", (e) => {
            e.stopPropagation();
            serverLogsVisible = !serverLogsVisible;
            if (serverLogsVisible) fetchServerLogs();
            else updateServerControlUI();
        });

        const bindSlider = (name, apply) => {
            const range = q(`#slider_${name}`);
            const label = q(`#val_${name}`);
            range.addEventListener("input", () => {
                range.style.setProperty("--v", `${range.value}%`);
                if (label) label.textContent = `${range.value}%`;
                apply(Number(range.value) / 100.0);
                saveSettings();
            });
        };
        bindSlider("sharp", (v) => { settings.sharpness = v; sendConfigToService(); });
        bindSlider("blend", (v) => { settings.aiBlend = v; sendConfigToService(); });
        bindSlider("clarity", (v) => { settings.clarity = v; });
        bindSlider("bright", (v) => { settings.brightness = v - 0.5; });

        // HDR: slider and exact numeric entry stay in sync.
        const hdrRange = q("#slider_hdr"), hdrNum = q("#num_hdr");
        const setHdr = (percent) => {
            const p = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
            settings.hdrShadows = p / 100.0;
            hdrRange.value = p;
            hdrRange.style.setProperty("--v", `${p}%`);
            if (document.activeElement !== hdrNum) hdrNum.value = p;
            saveSettings();
            sendConfigToService();
        };
        hdrRange.addEventListener("input", () => setHdr(hdrRange.value));
        hdrNum.addEventListener("input", () => { if (hdrNum.value !== "") setHdr(hdrNum.value); });
        hdrNum.addEventListener("blur", () => { hdrNum.value = Math.round(settings.hdrShadows * 100); });
        hdrNum.addEventListener("keydown", (e) => e.stopPropagation());

        q("#aiToggle").addEventListener("change", (e) => {
            settings.enabled = e.target.checked;
            saveSettings();
            sendConfigToService();
            if (!settings.enabled) {
                if (canvas) canvas.style.display = "none";
                if (activeVideo) activeVideo.style.opacity = "1";
            }
            updateBadge();
            updateHud();
        });
        q("#strToggle").addEventListener("change", (e) => {
            settings.stringingEnabled = e.target.checked;
            saveSettings();
            pushStringingConfigToService();
            if (!settings.stringingEnabled && stringingCtx && stringingCanvas) {
                stringingCtx.clearRect(0, 0, stringingCanvas.width, stringingCanvas.height);
            }
            updateStringingUI();
            drawStringingOverlay();
        });
        q("#strAutoPauseToggle").addEventListener("change", (e) => { settings.stringingAutoPause = e.target.checked; saveSettings(); pushStringingConfigToService(); });
        q("#strSensSelect").addEventListener("change", (e) => { settings.stringingSensitivity = e.target.value; saveSettings(); pushStringingConfigToService(); });
        q("#btnStrReset").addEventListener("click", () => {
            const addr = findPrinterAddress(activeVideo) || findPrinterAddress(sliderEl);
            const ipQ = addr ? `?ip=${encodeURIComponent(addr)}` : "";
            fetch(`${API_URL}/ai/stringing/reset${ipQ}`, { method: "POST" })
                .then((r) => (r.ok ? r.json() : null))
                .then((json) => { if (json && json.ok) handleStringingStatus(json); })
                .catch(() => {});
        });
        q("#fgToggle").addEventListener("change", (e) => { settings.frameGenEnabled = e.target.checked; saveSettings(); });
        q("#fpsSelect").addEventListener("change", (e) => { settings.targetFps = e.target.value; saveSettings(); });
        q("#inputSelect").addEventListener("change", (e) => { settings.aiInput = e.target.value; saveSettings(); });
        q("#resSelect").addEventListener("change", (e) => { settings.outRes = e.target.value; saveSettings(); sendConfigToService(); });
        q("#btnReset").addEventListener("click", () => {
            settings = Object.assign({}, defaults);
            saveSettings();
            sendConfigToService();
            pushStringingConfigToService();
            createUI(parent);
            openAIPanel(false);
        });

        window.__updateUI();
        updateStringingUI();
        updateServerControlUI();
        pushStringingConfigToService();
        pollServerControlStatus(true);
    }

    function updateBadge() { if (window.__updateUI) window.__updateUI(); }
    function updateHud() { if (window.__updateHUD) window.__updateHUD(); }

    // ---------------------------------------------------------------- Print speed slider & live telemetry
    // Injected into Creality's own "Printing speed" block (below presets) and as a dedicated
    // synchronized extrusion/feature card immediately following it.
    let speedDragging = false;
    let isPointerDownOnSpeed = false;
    let dragReleaseTimer = null;
    let pointerUpResetTimer = null;
    let lastCommitAt = -100000;
    let lastSentPrintSpeed = null;
    let printerWs = null;
    let activePrinterIp = null;
    let sliderEl = null;
    let sliderValEl = null;
    let telemetryCardEl = null;

    // Live telemetry state (Tier 1: :8088 Moonraker + G-code Range tracker; Tier 2: direct :9999 WS fallback)
    let serviceTelemetry = null;
    let serviceTelemetryOk = false;
    let lastServiceFetchAt = 0;
    let serviceFetchInFlight = false;
    let heldFeature = null;
    let heldFeatureAt = 0;
    let nonExtrudeSince = 0;

    const ws9999State = {
        connected: false,
        stateCode: null,
        realTimeSpeed: null,
        curFeedratePct: null,
        curFlowratePct: null,
        speedMode: null,
        layer: null,
        totalLayers: null,
        printFileName: "",
        autohome: "",
        nozzleTemp: null,
        targetNozzleTemp: null,
        bedTemp0: null,
        targetBedTemp0: null,
        curPosition: "",
        lastMsgAt: 0,
    };

    const CATEGORY_COLORS = {
        wall_outer: "#FF7A18",
        wall: "#38BDF8",
        overhang: "#F59E0B",
        infill: "#A78BFA",
        solid: "#818CF8",
        surface: "#17CC5F",
        ironing: "#F472B6",
        bridge: "#2DD4BF",
        support: "#FACC15",
        adhesion: "#A3E635",
        prime: "#22D3EE",
        travel: "#94A3B8",
        wipe: "#94A3B8",
        retract: "#94A3B8",
        prep: "#FBBF24",
        paused: "#FB923C",
        complete: "#17CC5F",
        idle: "#8E8E93",
        error: "#EF4444",
        print: "#17CC5F",
    };

    function clampSpeed(v) {
        return Math.max(25, Math.min(190, Math.round(Number(v) || 100)));
    }

    function updateSpeedVisual(v) {
        const clamped = clampSpeed(v);
        const pct = ((clamped - 25) / (190 - 25)) * 100;
        if (sliderEl) {
            sliderEl.style.setProperty("--pct", `${pct.toFixed(2)}%`);
        }
        if (sliderValEl) {
            sliderValEl.textContent = `${clamped}%`;
        }
        return clamped;
    }

    function setSliderValue(v, syncSent = true) {
        const num = Number(v);
        if (!Number.isFinite(num)) return;
        currentPrintSpeed = clampSpeed(num);
        if (syncSent && !speedDragging && !isPointerDownOnSpeed) {
            lastSentPrintSpeed = currentPrintSpeed;
        }
        if (!speedDragging && !isPointerDownOnSpeed) {
            if (sliderEl) sliderEl.value = currentPrintSpeed;
            updateSpeedVisual(currentPrintSpeed);
        }
    }

    function findPrinterAddress(el) {
        let cur = el;
        while (cur && cur !== document.body) {
            const inst = cur.__vueParentComponent;
            let check = inst;
            while (check) {
                const addr = check.props && check.props.address;
                if (typeof addr === "string" && /^\d{1,3}(\.\d{1,3}){3}$/.test(addr)) {
                    activePrinterIp = addr;
                    return addr;
                }
                check = check.parent;
            }
            cur = cur.parentElement;
        }
        const qsIp = new URLSearchParams(window.location.search).get("ip");
        if (qsIp && /^\d{1,3}(\.\d{1,3}){3}$/.test(qsIp)) {
            activePrinterIp = qsIp;
            return qsIp;
        }
        return activePrinterIp;
    }

    function handlePrinterWs9999Message(msg) {
        if (!msg || typeof msg !== "object") return;
        ws9999State.lastMsgAt = performance.now();
        if (msg.state !== undefined) ws9999State.stateCode = Number(msg.state);
        if (msg.realTimeSpeed !== undefined) {
            const sp = parseFloat(msg.realTimeSpeed);
            if (Number.isFinite(sp)) ws9999State.realTimeSpeed = sp;
        }
        if (msg.speedMode !== undefined) ws9999State.speedMode = Number(msg.speedMode);
        if (msg.curFeedratePct !== undefined) {
            const fp = Number(msg.curFeedratePct);
            if (Number.isFinite(fp) && fp > 0) ws9999State.curFeedratePct = fp;
        }
        if (msg.curFlowratePct !== undefined) {
            const flp = Number(msg.curFlowratePct);
            if (Number.isFinite(flp)) ws9999State.curFlowratePct = flp;
        }
        if (msg.layer !== undefined) ws9999State.layer = Number(msg.layer);
        if (msg.TotalLayer !== undefined) ws9999State.totalLayers = Number(msg.TotalLayer);
        if (typeof msg.printFileName === "string") ws9999State.printFileName = msg.printFileName;
        if (typeof msg.autohome === "string") ws9999State.autohome = msg.autohome;
        if (msg.nozzleTemp !== undefined) ws9999State.nozzleTemp = parseFloat(msg.nozzleTemp);
        if (msg.targetNozzleTemp !== undefined) ws9999State.targetNozzleTemp = parseFloat(msg.targetNozzleTemp);
        if (msg.bedTemp0 !== undefined) ws9999State.bedTemp0 = parseFloat(msg.bedTemp0);
        if (msg.targetBedTemp0 !== undefined) ws9999State.targetBedTemp0 = parseFloat(msg.targetBedTemp0);
        if (typeof msg.curPosition === "string") ws9999State.curPosition = msg.curPosition;

        if (ws9999State.speedMode === 1) {
            setSliderValue(25);
        } else if (ws9999State.curFeedratePct !== null && !serviceTelemetryOk) {
            setSliderValue(ws9999State.curFeedratePct);
        }

        if (!serviceTelemetryOk) renderTelemetryCard();
    }

    function openPrinterSocket(addr) {
        if (!addr) return null;
        if (printerWs && printerWs.__addr === addr && printerWs.readyState <= 1) return printerWs;
        try {
            if (printerWs && printerWs.__addr !== addr) {
                try { printerWs.close(); } catch (e) {}
                printerWs = null;
                ws9999State.connected = false;
                ws9999State.stateCode = null;
                ws9999State.realTimeSpeed = null;
                ws9999State.curFeedratePct = null;
                ws9999State.curFlowratePct = null;
                ws9999State.speedMode = null;
                ws9999State.layer = null;
                ws9999State.totalLayers = null;
                ws9999State.printFileName = "";
                ws9999State.lastMsgAt = 0;
                serviceTelemetry = null;
                serviceTelemetryOk = false;
                heldFeature = null;
                nonExtrudeSince = 0;
                stringingStatus = null;
                stringingConfigSynced = false;
                alertDismissedAt = 0;
                lastUiPauseSentAt = -100000;
                drawStringingOverlay();
                updateStringingUI();
            }
            const sock = new WebSocket(`ws://${addr}:9999`);
            sock.__addr = addr;
            sock.onopen = () => {
                if (printerWs === sock) {
                    ws9999State.connected = true;
                    if (!serviceTelemetryOk) renderTelemetryCard();
                }
            };
            sock.onmessage = (ev) => {
                if (typeof ev.data !== "string") return;
                try { handlePrinterWs9999Message(JSON.parse(ev.data)); } catch (e) {}
            };
            sock.onclose = () => {
                if (printerWs === sock) {
                    printerWs = null;
                    ws9999State.connected = false;
                    if (!serviceTelemetryOk) renderTelemetryCard();
                }
            };
            sock.onerror = () => {
                if (printerWs === sock) {
                    ws9999State.connected = false;
                }
            };
            printerWs = sock;
            return sock;
        } catch (e) { return null; }
    }

    // Same messages Creality Print itself sends for its preset buttons.
    function sendPrintSpeed(pct, addr) {
        const v = clampSpeed(pct);
        const resolvedAddr = addr || findPrinterAddress(sliderEl);
        const msgs = v === 25
            ? [{ method: "set", params: { speedMode: 1 } }]
            : [{ method: "set", params: { speedMode: 0 } }, { method: "set", params: { setFeedratePct: v } }];

        const viaBridge = () => {
            const ipQuery = resolvedAddr ? `&ip=${encodeURIComponent(resolvedAddr)}` : "";
            return fetch(`${API_URL}/printer/set_speed?speed=${v}${ipQuery}`).catch(() => {});
        };
        const sock = openPrinterSocket(resolvedAddr);
        if (sock && sock.readyState === WebSocket.OPEN) {
            msgs.forEach((m) => sock.send(JSON.stringify(m)));
        } else if (sock && sock.readyState === WebSocket.CONNECTING) {
            const t = setTimeout(viaBridge, 1500);
            sock.addEventListener("open", () => { clearTimeout(t); msgs.forEach((m) => sock.send(JSON.stringify(m))); }, { once: true });
        } else {
            viaBridge();
        }
    }

    function commitSpeedChange(rawVal, addr) {
        const val = updateSpeedVisual(rawVal);
        currentPrintSpeed = val;
        if (val !== lastSentPrintSpeed) {
            lastSentPrintSpeed = val;
            lastCommitAt = performance.now();
            sendPrintSpeed(val, addr);
        }
    }

    // Handle pointer release globally so dragging outside the slider never leaves
    // speedDragging locked or loses a pending release commit.
    window.addEventListener("pointerup", () => {
        if (!isPointerDownOnSpeed) return;
        isPointerDownOnSpeed = false;
        clearTimeout(pointerUpResetTimer);
        pointerUpResetTimer = setTimeout(() => {
            if (!isPointerDownOnSpeed && sliderEl) {
                const cur = clampSpeed(sliderEl.value);
                if (cur !== lastSentPrintSpeed) {
                    clearTimeout(dragReleaseTimer);
                    commitSpeedChange(cur, findPrinterAddress(sliderEl));
                    dragReleaseTimer = setTimeout(() => { speedDragging = false; }, 2500);
                } else if ((performance.now() - lastCommitAt) > 2500) {
                    speedDragging = false;
                }
            }
        }, 40);
    });
    window.addEventListener("pointercancel", () => {
        if (!isPointerDownOnSpeed) return;
        isPointerDownOnSpeed = false;
        clearTimeout(pointerUpResetTimer);
        speedDragging = false;
    });

    function ensureSharedStyles() {
        if (document.getElementById("agySpeedStyle")) return;
        const st = document.createElement("style");
        st.id = "agySpeedStyle";
        st.textContent = `
            .print-speed-wrapper {
                height: auto !important;
                min-height: 120px !important;
                padding: 12px 14px 14px !important;
                box-sizing: border-box !important;
            }
            .print-speed-wrapper .print-model-radio {
                margin-top: 14px !important;
                margin-bottom: 0 !important;
            }
            .agy-speed-fine {
                display: flex;
                align-items: center;
                gap: 12px;
                margin: 14px 0 0 0;
                padding: 4px 0 0 0;
                font-size: 14px;
                line-height: 20px;
                box-sizing: border-box;
            }
            .agy-speed-fine .agy-lbl {
                white-space: nowrap;
                font-size: 13px;
                font-weight: 500;
                color: var(--primary-card-device-name-text-color, #AEAEB2);
                margin: 0;
            }
            .agy-speed-fine input[type=range] {
                flex: 1;
                min-width: 0;
                margin: 0;
                -webkit-appearance: none;
                appearance: none;
                height: 4px;
                border-radius: 2px;
                background: linear-gradient(
                    to right,
                    var(--primary-button-hover-color, #17CC5F) var(--pct, 45.45%),
                    var(--primary-new-detail-print-speed-bg-color, #373739) var(--pct, 45.45%)
                );
                outline: none;
                cursor: pointer;
            }
            .agy-speed-fine input[type=range]::-webkit-slider-thumb {
                -webkit-appearance: none;
                appearance: none;
                width: 14px;
                height: 14px;
                border-radius: 50%;
                background: var(--primary-button-hover-color, #17CC5F);
                border: 2px solid var(--primary-new-detail-card-bg-color, #2B2B2B);
                box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
                cursor: pointer;
                transition: transform 0.12s ease, box-shadow 0.12s ease;
            }
            .agy-speed-fine input[type=range]::-webkit-slider-thumb:hover {
                transform: scale(1.12);
                box-shadow: 0 0 0 4px rgba(23, 204, 95, 0.22);
            }
            .agy-speed-fine .agy-val {
                min-width: 44px;
                text-align: right;
                font-weight: 600;
                font-variant-numeric: tabular-nums;
                color: var(--primary-button-hover-color, #17CC5F);
                white-space: nowrap;
            }

            /* Live telemetry & extrusion card (sits right next to Modello di ventilazione) */
            .agy-fan-host-row {
                display: flex !important;
                flex-wrap: wrap !important;
                align-items: stretch !important;
                gap: 15px !important;
                padding: 10px !important;
                width: 100% !important;
                box-sizing: border-box !important;
            }
            .agy-fan-host-row > .control-wrapper {
                flex: 0 0 250px !important;
                box-sizing: border-box !important;
            }
            .agy-telemetry-row {
                width: 100%;
                padding: 10px;
                box-sizing: border-box;
            }
            .agy-telemetry-card {
                flex: 1 1 280px;
                min-width: 0;
                min-height: 120px;
                padding: 10px 14px;
                background-color: var(--primary-new-detail-card-bg-color, #2B2B2B);
                border-radius: 6px;
                font-size: 14px;
                color: var(--primary-card-right-value-text-color, #FFFFFF);
                box-sizing: border-box;
                display: flex;
                flex-direction: column;
                justify-content: space-between;
                gap: 6px;
            }
            .agy-telemetry-row > .agy-telemetry-card {
                width: 100%;
            }
            .agy-tel-head {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 8px;
                flex-wrap: nowrap;
            }
            .agy-tel-title {
                font-size: 14px;
                font-weight: 500;
                color: var(--primary-card-right-value-text-color, #FFFFFF);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .agy-tel-meta {
                display: flex;
                align-items: center;
                gap: 6px;
                flex-wrap: nowrap;
                min-width: 0;
                flex-shrink: 0;
            }
            .agy-tel-chip {
                font-size: 11px;
                line-height: 16px;
                padding: 2px 8px;
                border-radius: 100px;
                background: var(--primary-new-detail-print-speed-bg-color, #373739);
                color: var(--primary-card-device-name-text-color, #AEAEB2);
                max-width: 115px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                font-variant-numeric: tabular-nums;
            }
            .agy-tel-srv-btn {
                display: none;
                align-items: center;
                gap: 4px;
                font-size: 10.5px;
                font-weight: 600;
                line-height: 16px;
                padding: 2px 8px;
                border-radius: 100px;
                border: 1px solid rgba(23, 204, 95, 0.45);
                background: rgba(23, 204, 95, 0.14);
                color: #4ADE80;
                cursor: pointer;
                white-space: nowrap;
                transition: background 0.14s ease, border-color 0.14s ease;
            }
            .agy-tel-srv-btn:hover:not(:disabled) {
                background: rgba(23, 204, 95, 0.24);
                border-color: rgba(23, 204, 95, 0.72);
            }
            .agy-tel-srv-btn.stop {
                border-color: rgba(239, 68, 68, 0.42);
                background: rgba(239, 68, 68, 0.12);
                color: #FCA5A5;
            }
            .agy-tel-srv-btn.stop:hover:not(:disabled) {
                background: rgba(239, 68, 68, 0.24);
                border-color: rgba(239, 68, 68, 0.72);
            }
            .agy-tel-srv-btn:disabled {
                opacity: 0.55;
                cursor: not-allowed;
            }
            .agy-tel-src {
                display: inline-flex;
                align-items: center;
                gap: 5px;
                font-size: 11px;
                line-height: 16px;
                padding: 2px 8px;
                border-radius: 100px;
                background: rgba(23, 204, 95, 0.12);
                color: var(--primary-button-hover-color, #17CC5F);
                font-weight: 500;
                white-space: nowrap;
            }
            .agy-tel-src.fallback {
                background: rgba(251, 191, 36, 0.14);
                color: #FBBF24;
            }
            .agy-tel-src.offline {
                background: rgba(142, 142, 147, 0.16);
                color: var(--primary-card-device-name-text-color, #AEAEB2);
            }
            .agy-tel-src i {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: currentColor;
                display: inline-block;
            }
            .agy-tel-main {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                min-width: 0;
            }
            .agy-tel-activity {
                display: flex;
                flex-direction: column;
                gap: 3px;
                min-width: 0;
                flex: 1;
            }
            .agy-tel-badge {
                display: inline-flex;
                align-items: center;
                gap: 7px;
                padding: 4px 11px;
                border-radius: 999px;
                width: fit-content;
                max-width: 100%;
                font-size: 13.5px;
                font-weight: 600;
                letter-spacing: 0.1px;
                border: 1px solid rgba(255, 255, 255, 0.10);
                background: rgba(23, 204, 95, 0.14);
                color: var(--primary-card-right-value-text-color, #FFFFFF);
            }
            .agy-tel-badge-dot {
                width: 8px;
                height: 8px;
                border-radius: 50%;
                flex: none;
                background: #17CC5F;
            }
            .agy-tel-badge-txt {
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .agy-tel-raw {
                font-size: 11px;
                color: var(--primary-card-device-name-text-color, #AEAEB2);
                padding-left: 2px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .agy-tel-speed-box {
                display: flex;
                flex-direction: column;
                align-items: flex-end;
                flex: none;
                text-align: right;
            }
            .agy-tel-speed-val {
                font-size: 22px;
                font-weight: 700;
                line-height: 1.05;
                font-variant-numeric: tabular-nums;
                color: var(--primary-button-hover-color, #17CC5F);
                letter-spacing: -0.3px;
            }
            .agy-tel-speed-val.muted {
                color: var(--primary-card-device-name-text-color, #AEAEB2);
            }
            .agy-tel-speed-val .agy-unit {
                font-size: 12px;
                font-weight: 600;
                margin-left: 3px;
                opacity: 0.85;
            }
            .agy-tel-speed-lbl {
                font-size: 11px;
                color: var(--primary-card-device-name-text-color, #AEAEB2);
                margin-top: 2px;
            }
            .agy-tel-sub {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
                flex-wrap: nowrap;
                padding-top: 6px;
                border-top: 1px solid rgba(128, 128, 128, 0.16);
                font-size: 11.5px;
                color: var(--primary-card-device-name-text-color, #AEAEB2);
                font-variant-numeric: tabular-nums;
            }
            .agy-tel-sub-left {
                display: flex;
                align-items: center;
                gap: 11px;
                flex-wrap: nowrap;
                flex: none;
                white-space: nowrap;
            }
            .agy-tel-sub-right {
                flex: 1;
                min-width: 60px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                text-align: right;
            }
            .agy-tel-sub b {
                color: var(--primary-card-right-value-text-color, #FFFFFF);
                font-weight: 600;
            }
        `;
        document.head.appendChild(st);
    }

    function injectSpeedSlider() {
        const wrapper = document.querySelector(".print-speed-wrapper");
        if (!wrapper) {
            sliderEl = null;
            sliderValEl = null;
            return;
        }

        ensureSharedStyles();
        const addr = findPrinterAddress(wrapper);
        if (addr) openPrinterSocket(addr);

        if (wrapper.querySelector(".agy-speed-fine")) return;

        const row = document.createElement("div");
        row.className = "agy-speed-fine";
        row.innerHTML = `<span class="agy-lbl">Preciso</span><input type="range" min="25" max="190" step="1" value="${currentPrintSpeed}"><span class="agy-val">${currentPrintSpeed}%</span>`;
        wrapper.appendChild(row);

        sliderEl = row.querySelector("input");
        sliderValEl = row.querySelector(".agy-val");
        lastSentPrintSpeed = currentPrintSpeed;
        updateSpeedVisual(currentPrintSpeed);

        const beginDrag = () => {
            isPointerDownOnSpeed = true;
            speedDragging = true;
            clearTimeout(dragReleaseTimer);
            clearTimeout(pointerUpResetTimer);
        };
        sliderEl.addEventListener("pointerdown", beginDrag);
        sliderEl.addEventListener("mousedown", beginDrag);
        sliderEl.addEventListener("touchstart", beginDrag, { passive: true });

        // Never transmit HTTP/WS requests during drag movement; only refresh UI text & fill.
        sliderEl.addEventListener("input", () => {
            speedDragging = true;
            updateSpeedVisual(sliderEl.value);
        });

        // Commit ONLY on release or discrete keyboard value step, and ONLY if value changed.
        sliderEl.addEventListener("change", () => {
            if (isPointerDownOnSpeed) return;
            clearTimeout(pointerUpResetTimer);
            clearTimeout(dragReleaseTimer);
            commitSpeedChange(Number(sliderEl.value), findPrinterAddress(wrapper));
            dragReleaseTimer = setTimeout(() => { speedDragging = false; }, 2500);
        });

        // Allow immediate resync from printer feedback after clicking a native preset pill.
        const presetBar = wrapper.querySelector(".print-model-radio");
        if (presetBar && !presetBar.__agyBound) {
            presetBar.__agyBound = true;
            presetBar.addEventListener("click", (e) => {
                const span = e.target && e.target.closest("span[data-index]");
                if (span) {
                    speedDragging = false;
                    isPointerDownOnSpeed = false;
                    clearTimeout(dragReleaseTimer);
                    pollServiceTelemetry(true);
                }
            });
        }

        const ipParam = addr ? `?ip=${encodeURIComponent(addr)}` : "";
        fetch(`${API_URL}/printer/get_speed${ipParam}`)
            .then((r) => r.json())
            .then((d) => setSliderValue(d.speed))
            .catch(() => {});
    }

    function resolveBadgeColor(payload) {
        const cat = (payload.feature_category || "").toLowerCase();
        const raw = (payload.feature_raw || "").toLowerCase();
        const code = (payload.activity_code || "").toLowerCase();

        if (code === "paused") return CATEGORY_COLORS.paused;
        if (code === "complete") return CATEGORY_COLORS.complete;
        if (code === "error" || code === "disconnected") return CATEGORY_COLORS.error;
        if (code === "idle") return CATEGORY_COLORS.idle;
        if (code === "preparing" || code === "heating" || code === "homing") return CATEGORY_COLORS.prep;
        if (code === "travel" || code === "retract" || code === "wipe") return CATEGORY_COLORS.travel;

        if (raw.includes("outer") || raw.includes("external")) return CATEGORY_COLORS.wall_outer;
        if (raw.includes("overhang")) return CATEGORY_COLORS.overhang;
        if (cat && CATEGORY_COLORS[cat]) return CATEGORY_COLORS[cat];
        return CATEGORY_COLORS.print;
    }

    function applyHexTint(hexColor) {
        // Parse #RRGGBB into soft translucent tint and crisp border
        const clean = hexColor.replace("#", "");
        if (clean.length !== 6) return { bg: "rgba(23,204,95,0.14)", border: "rgba(23,204,95,0.35)" };
        const r = parseInt(clean.slice(0, 2), 16);
        const g = parseInt(clean.slice(2, 4), 16);
        const b = parseInt(clean.slice(4, 6), 16);
        return {
            bg: `rgba(${r}, ${g}, ${b}, 0.14)`,
            border: `rgba(${r}, ${g}, ${b}, 0.36)`,
        };
    }

    function handleServiceTelemetry(data) {
        if (!data || typeof data !== "object") return;
        serviceTelemetry = data;
        serviceTelemetryOk = Boolean(data.ok);
        if (serviceTelemetryOk && typeof data.speed_factor_pct === "number") {
            setSliderValue(data.speed_factor_pct, true);
        }
        if (serviceTelemetryOk && !stringingConfigSynced) {
            pushStringingConfigToService();
        }
        if (data.stringing && typeof data.stringing === "object") {
            if (!stringingStatus || !activeVideo || activeVideo.paused) {
                handleStringingStatus(data.stringing);
            }
        }
        renderTelemetryCard();
    }

    function buildWs9999FallbackView() {
        const st = ws9999State;
        const now = performance.now();
        const fresh = st.connected && (now - st.lastMsgAt) < 10000;
        const pct = st.speedMode === 1 ? 25 : (st.curFeedratePct || currentPrintSpeed || 100);
        const nom = st.realTimeSpeed !== null ? st.realTimeSpeed : null;
        const scaledSpeed = nom !== null ? (nom * pct) / 100.0 : 0.0;

        let code = "idle";
        let label = "In attesa";
        const sc = st.stateCode;
        if (sc === 1) {
            if (st.autohome && st.autohome !== "X:1 Y:1 Z:1") {
                code = "preparing";
                label = "Homing / Livellamento";
            } else {
                code = "feature";
                label = "Stampa in corso";
            }
        } else if (sc === 5) {
            code = "paused";
            label = "Stampa in pausa";
        } else if (sc === 2) {
            code = "complete";
            label = "Stampa completata";
        } else if (sc === 3) {
            code = "idle";
            label = "Stampa annullata";
        } else if (sc === 4) {
            code = "error";
            label = "Errore stampante";
        } else if (sc >= 6) {
            code = "preparing";
            label = "Preparazione stampa";
        }

        const fname = st.printFileName ? st.printFileName.split("/").pop() : "";
        return {
            ok: fresh,
            fallback: true,
            state: sc === 1 ? "printing" : (sc === 5 ? "paused" : "standby"),
            activity_code: code,
            activity_label: label,
            activity_detail: "",
            feature_raw: "",
            feature_category: sc === 1 ? "print" : code,
            is_extruding: sc === 1 && scaledSpeed > 0.2,
            nozzle_speed_mm_s: Number(scaledSpeed.toFixed(1)),
            filament_speed_mm_s: null,
            flow_mm3_s: null,
            speed_factor_pct: pct,
            layer: st.layer,
            total_layers: st.totalLayers,
            progress_pct: null,
            elapsed_label: "",
            filename: fname,
            current_object: "",
            line_width_mm: null,
        };
    }

    function renderTelemetryCard() {
        if (!telemetryCardEl || !document.body.contains(telemetryCardEl)) return;

        const now = performance.now();
        const q = (sel) => telemetryCardEl.querySelector(sel);

        const elObj = q(".agy-tel-obj");
        const elLayer = q(".agy-tel-layer");
        const elSrc = q(".agy-tel-src");
        const elSrcText = q(".agy-tel-src-txt");
        const elBadge = q(".agy-tel-badge");
        const elDot = q(".agy-tel-badge-dot");
        const elAct = q(".agy-tel-badge-txt");
        const elRaw = q(".agy-tel-raw");
        const elSpeedVal = q(".agy-tel-speed-num");
        const elSpeedWrap = q(".agy-tel-speed-val");
        const elSpeedLbl = q(".agy-tel-speed-lbl");
        const elSubLeft = q(".agy-tel-sub-left");
        const elSubRight = q(".agy-tel-sub-right");

        let view = null;
        let mode = "offline"; // "live" | "fallback" | "offline"

        if (serviceTelemetry && serviceTelemetry.ok) {
            view = Object.assign({}, serviceTelemetry);
            mode = "live";

            // Apply 550ms anti-flicker hysteresis on brief intra-layer retracts / travels
            if (view.state === "printing") {
                if (view.activity_code === "feature") {
                    heldFeature = {
                        activity_code: "feature",
                        activity_label: view.activity_label,
                        feature_raw: view.feature_raw,
                        feature_category: view.feature_category,
                        line_width_mm: view.line_width_mm,
                    };
                    heldFeatureAt = now;
                    nonExtrudeSince = 0;
                } else if (
                    view.activity_code === "travel" ||
                    view.activity_code === "retract" ||
                    view.activity_code === "wipe"
                ) {
                    if (!nonExtrudeSince) nonExtrudeSince = now;
                    if (heldFeature && (now - nonExtrudeSince) < 550 && (now - heldFeatureAt) < 1800) {
                        view.activity_code = heldFeature.activity_code;
                        view.activity_label = heldFeature.activity_label;
                        view.feature_raw = heldFeature.feature_raw;
                        view.feature_category = heldFeature.feature_category;
                        view.line_width_mm = heldFeature.line_width_mm;
                        view._hysteresisHeld = true;
                    }
                } else {
                    heldFeature = null;
                    nonExtrudeSince = 0;
                }
            } else {
                heldFeature = null;
                nonExtrudeSince = 0;
            }
        } else if (ws9999State.connected) {
            view = buildWs9999FallbackView();
            mode = "fallback";
        } else {
            view = serviceTelemetry || {
                ok: false,
                state: "disconnected",
                activity_code: "disconnected",
                activity_label: "In attesa di connessione",
                feature_raw: "",
                feature_category: "idle",
                nozzle_speed_mm_s: 0.0,
                speed_factor_pct: currentPrintSpeed,
            };
            mode = "offline";
        }

        // Source indicator pill & inline Server AI Start/Stop quick button
        const elSrvBtn = q(".agy-tel-srv-btn");
        const srvRunning = Boolean((serverControlStatus && (serverControlStatus.ai_running || serverControlStatus.port_8088_listening)) || mode === "live");
        if (elSrcText) {
            if (mode === "live") {
                elSrc.className = "agy-tel-src";
                elSrcText.textContent = "Live";
            } else if (mode === "fallback") {
                elSrc.className = "agy-tel-src fallback";
                elSrcText.textContent = "Diretto :9999";
            } else {
                elSrc.className = "agy-tel-src offline";
                elSrcText.textContent = (serverControlOnline && !srvRunning) ? "Server spento" : "Offline";
            }
        }
        if (elSrvBtn) {
            if (serverControlBusy) {
                elSrvBtn.style.display = "inline-flex";
                elSrvBtn.disabled = true;
                elSrvBtn.textContent = srvRunning ? "Arresto..." : "Avvio...";
                elSrvBtn.className = srvRunning ? "agy-tel-srv-btn stop" : "agy-tel-srv-btn";
            } else if (srvRunning) {
                elSrvBtn.style.display = "inline-flex";
                elSrvBtn.disabled = false;
                elSrvBtn.textContent = "\u25a0 Stop AI";
                elSrvBtn.title = "Ferma Server AI (:8088)";
                elSrvBtn.className = "agy-tel-srv-btn stop";
            } else if (serverControlOnline) {
                elSrvBtn.style.display = "inline-flex";
                elSrvBtn.disabled = false;
                elSrvBtn.textContent = "\u25b6 Avvia AI";
                elSrvBtn.title = "Avvia Server AI (:8088)";
                elSrvBtn.className = "agy-tel-srv-btn";
            } else {
                elSrvBtn.style.display = "none";
            }
        }

        // Object chip
        if (view.current_object) {
            elObj.style.display = "";
            elObj.textContent = view.current_object;
            elObj.title = view.current_object;
        } else {
            elObj.style.display = "none";
        }

        // Layer chip
        if (view.layer !== null && view.layer !== undefined) {
            elLayer.style.display = "";
            elLayer.textContent = view.total_layers
                ? `Layer ${view.layer}/${view.total_layers}`
                : `Layer ${view.layer}`;
        } else {
            elLayer.style.display = "none";
        }

        // Badge styling & activity text
        const color = resolveBadgeColor(view);
        const tint = applyHexTint(color);
        elBadge.style.backgroundColor = tint.bg;
        elBadge.style.borderColor = tint.border;
        elDot.style.backgroundColor = color;
        elAct.textContent = view.activity_label || "In attesa";

        // Secondary slicer line under badge
        const details = [];
        if (mode === "live") {
            if (view.feature_raw && view.activity_code === "feature") {
                details.push(`G-code: ${view.feature_raw}`);
            } else if (view.activity_detail) {
                details.push(view.activity_detail);
            }
            if (view.line_width_mm && view.activity_code === "feature") {
                details.push(`Larghezza ${Number(view.line_width_mm).toFixed(2)} mm`);
            }
            if (view.activity_code === "travel") {
                details.push("Nessuna estrusione");
            }
            if (view.activity_code === "idle" && !details.length) {
                details.push("Stampante ferma");
            }
        } else if (mode === "fallback") {
            details.push("Avvia Servizio AI (:8088) per tipo stampa e flusso reale");
        } else {
            details.push("Servizio AI (:8088) e socket stampante non attivi");
        }
        elRaw.textContent = details.join(" · ");

        // Extrusion / nozzle speed in mm/s
        const spd = Number(view.nozzle_speed_mm_s || 0);
        elSpeedVal.textContent = spd.toFixed(1);
        const activelyMoving = spd > 0.1;
        elSpeedWrap.className = activelyMoving ? "agy-tel-speed-val" : "agy-tel-speed-val muted";

        if (mode === "fallback") {
            elSpeedLbl.textContent = "Velocità stimata";
        } else if (view.activity_code === "travel" || view.activity_code === "wipe") {
            elSpeedLbl.textContent = "Velocità spostamento";
        } else {
            elSpeedLbl.textContent = "Velocità estrusione";
        }

        // Bottom metrics bar (safe DOM construction)
        elSubLeft.replaceChildren();
        const addMetric = (prefix, strongText) => {
            const span = document.createElement("span");
            span.appendChild(document.createTextNode(`${prefix}: `));
            const b = document.createElement("b");
            b.textContent = strongText;
            span.appendChild(b);
            elSubLeft.appendChild(span);
        };

        if (mode === "live") {
            addMetric("Filamento", `${Number(view.filament_speed_mm_s || 0).toFixed(2)} mm/s`);
            addMetric("Flusso", `${Number(view.flow_mm3_s || 0).toFixed(2)} mm³/s`);
            addMetric("Fatt. vel", `${view.speed_factor_pct || 100}%`);
        } else {
            addMetric("Fatt. vel", `${view.speed_factor_pct || currentPrintSpeed || 100}%`);
            if (ws9999State.curFlowratePct !== null) {
                addMetric("Flusso imp.", `${ws9999State.curFlowratePct}%`);
            }
        }

        const rightParts = [];
        const showJobProgress = view.state === "printing" || view.state === "paused";
        if (view.progress_pct !== null && view.progress_pct !== undefined && showJobProgress) {
            rightParts.push(`${Number(view.progress_pct).toFixed(1)}%`);
        }
        if (view.elapsed_label && showJobProgress) {
            rightParts.push(view.elapsed_label);
        }
        if (view.filename) {
            rightParts.push(view.filename);
        }
        elSubRight.textContent = rightParts.join(" · ");
        elSubRight.title = view.filename || "";
    }

    function findFanControlRow(panelContainer, speedCtrlRow) {
        if (!panelContainer) return null;
        const rows = Array.from(panelContainer.children);
        for (const child of rows) {
            if (child === speedCtrlRow) continue;
            if (child.classList.contains("print-control-wrapper") && child.querySelector(".control-wrapper")) {
                return child;
            }
        }
        const cw = panelContainer.querySelector(".control-wrapper");
        const row = cw && cw.closest(".print-control-wrapper");
        return (row && row !== speedCtrlRow) ? row : null;
    }

    function injectTelemetryCard() {
        const speedWrapper = document.querySelector(".print-speed-wrapper");
        if (!speedWrapper) {
            telemetryCardEl = null;
            return;
        }
        const speedCtrlRow = speedWrapper.closest(".print-control-wrapper") || speedWrapper.parentElement;
        const panelContainer = speedCtrlRow && speedCtrlRow.parentElement;
        if (!panelContainer) return;

        ensureSharedStyles();
        const fanCtrlRow = findFanControlRow(panelContainer, speedCtrlRow);
        const existingCard = panelContainer.querySelector(".agy-telemetry-card");
        if (existingCard) {
            if (fanCtrlRow) {
                fanCtrlRow.classList.add("agy-fan-host-row");
                if (existingCard.parentElement !== fanCtrlRow) {
                    const oldWrapperRow = existingCard.closest(".agy-telemetry-row");
                    fanCtrlRow.appendChild(existingCard);
                    if (oldWrapperRow && oldWrapperRow !== fanCtrlRow) oldWrapperRow.remove();
                }
            }
            telemetryCardEl = existingCard;
            return;
        }

        const card = document.createElement("div");
        card.className = "agy-telemetry-card";
        card.innerHTML = `
            <div class="agy-tel-head">
                <span class="agy-tel-title">Attività ugello</span>
                <div class="agy-tel-meta">
                    <span class="agy-tel-chip agy-tel-obj" style="display:none"></span>
                    <span class="agy-tel-chip agy-tel-layer" style="display:none"></span>
                    <span class="agy-tel-src offline"><i></i><span class="agy-tel-src-txt">Offline</span></span>
                    <button type="button" class="agy-tel-srv-btn"></button>
                </div>
            </div>
            <div class="agy-tel-main">
                <div class="agy-tel-activity">
                    <div class="agy-tel-badge">
                        <span class="agy-tel-badge-dot"></span>
                        <span class="agy-tel-badge-txt">In attesa di dati...</span>
                    </div>
                    <div class="agy-tel-raw">Connessione telemetria in corso</div>
                </div>
                <div class="agy-tel-speed-box">
                    <div class="agy-tel-speed-val muted"><span class="agy-tel-speed-num">0.0</span><span class="agy-unit">mm/s</span></div>
                    <div class="agy-tel-speed-lbl">Velocità estrusione</div>
                </div>
            </div>
            <div class="agy-tel-sub">
                <div class="agy-tel-sub-left"></div>
                <div class="agy-tel-sub-right"></div>
            </div>
        `;

        if (fanCtrlRow) {
            fanCtrlRow.classList.add("agy-fan-host-row");
            fanCtrlRow.appendChild(card);
        } else {
            const row = document.createElement("div");
            row.className = "agy-telemetry-row";
            row.appendChild(card);
            panelContainer.appendChild(row);
        }

        const srvBtn = card.querySelector(".agy-tel-srv-btn");
        if (srvBtn) {
            srvBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                const isUp = Boolean((serverControlStatus && (serverControlStatus.ai_running || serverControlStatus.port_8088_listening)) || serviceTelemetryOk);
                controlServerAction(isUp ? "stop" : "start");
            });
        }
        const srcPill = card.querySelector(".agy-tel-src");
        if (srcPill) {
            srcPill.style.cursor = "pointer";
            srcPill.title = "Clicca per aprire il pannello Server AI";
            srcPill.addEventListener("click", (e) => {
                e.stopPropagation();
                openAIPanel(false);
            });
        }

        telemetryCardEl = card;
        renderTelemetryCard();
        pollServiceTelemetry(true);
        pollServerControlStatus(true);
    }

    async function pollServiceTelemetry(force = false) {
        if (serviceFetchInFlight) return;
        if (!telemetryCardEl || !document.body.contains(telemetryCardEl)) return;
        if (document.visibilityState === "hidden") return;

        const now = performance.now();
        const interval = serviceTelemetryOk ? 350 : 1500;
        if (!force && (now - lastServiceFetchAt) < interval) return;

        const addr = findPrinterAddress(telemetryCardEl) || findPrinterAddress(sliderEl);
        if (addr) openPrinterSocket(addr);

        serviceFetchInFlight = true;
        lastServiceFetchAt = now;
        const ctrl = new AbortController();
        const tid = setTimeout(() => ctrl.abort(), 1600);
        try {
            const ipQuery = addr ? `?ip=${encodeURIComponent(addr)}` : "";
            const res = await fetch(`${API_URL}/printer/telemetry${ipQuery}`, { signal: ctrl.signal });
            if (res.ok) {
                const json = await res.json();
                handleServiceTelemetry(json);
            } else {
                serviceTelemetryOk = false;
                renderTelemetryCard();
            }
        } catch (e) {
            serviceTelemetryOk = false;
            renderTelemetryCard();
        } finally {
            clearTimeout(tid);
            serviceFetchInFlight = false;
        }
    }

    setInterval(() => {
        if (telemetryCardEl && document.body.contains(telemetryCardEl)) {
            pollServiceTelemetry(false);
        }
        if (document.visibilityState !== "hidden") {
            pollServerControlStatus(false);
        }
    }, 350);

    setInterval(() => {
        pollStandaloneStringingFrame();
    }, 250);

    setInterval(() => {
        if (sliderEl && document.body.contains(sliderEl) && !speedDragging && !isPointerDownOnSpeed && !serviceTelemetryOk) {
            const addr = findPrinterAddress(sliderEl);
            const ipParam = addr ? `?ip=${encodeURIComponent(addr)}` : "";
            fetch(`${API_URL}/printer/get_speed${ipParam}`).then((r) => r.json()).then((d) => setSliderValue(d.speed)).catch(() => {});
        }
    }, 3000);

    function findCameraVideo() {
        const selectors = ["#remoteVideo", "#remoteVideos", "video.video-player", "video.camera-image", "video"];
        for (const sel of selectors) {
            const list = document.querySelectorAll(sel);
            for (const v of list) {
                const r = v.getBoundingClientRect();
                if (r.width > 20 && r.height > 20) return v;
            }
        }
        return document.getElementById("remoteVideo") || document.getElementById("remoteVideos") || document.querySelector("video");
    }

    // ---------------------------------------------------------------- Wiring
    function searchAndAttach() {
        const video = findCameraVideo();
        if (video) attachToVideo(video);
        injectSpeedSlider();
        injectTelemetryCard();
    }

    let scanScheduled = false;
    const observer = new MutationObserver(() => {
        if (scanScheduled) return;
        scanScheduled = true;
        setTimeout(() => { scanScheduled = false; searchAndAttach(); }, 300);
    });
    observer.observe(document.body, { childList: true, subtree: true });
    setInterval(searchAndAttach, 1000);
    searchAndAttach();
})();
