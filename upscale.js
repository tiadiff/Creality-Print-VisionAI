// Creality Print: Real-ESRGAN (RTX 4070) video enhancement, high-refresh smoothing,
// and a fine-grained print speed slider injected into the original "Printing speed" section.
(function () {
    console.log("[Upscaler] init");

    const WS_URL = "ws://127.0.0.1:8088/ws_upscale";
    const API_URL = "http://127.0.0.1:8088";
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
        brightness: 0.0    // display brightness, neutral (-0.5..0.5)
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
                target_res: settings.outRes,
                ai_blend: settings.aiBlend,
                hdr_shadows: settings.hdrShadows,
                sharpness: settings.sharpness
            }));
        }
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
                ws.send(JSON.stringify({ action: "get_speed" }));
                updateBadge();
            };

            ws.onmessage = async (event) => {
                if (typeof event.data === "string") {
                    try {
                        const msg = JSON.parse(event.data);
                        if ((msg.type === "speed_update" || msg.type === "speed_ack") && typeof msg.speed === "number") {
                            if (!speedDragging) setSliderValue(msg.speed);
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
    function sendFrameToAI() {
        if (!settings.enabled || !wsConnected || inFlight >= MAX_IN_FLIGHT) return;
        if (!activeVideo || activeVideo.paused || activeVideo.videoWidth === 0) return;
        if (ws.bufferedAmount > 8 * 1024 * 1024) return;

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
            inFlight++;
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

    function attachToVideo(videoEl) {
        if (activeVideo === videoEl && canvas && canvas.parentElement) return;
        activeVideo = videoEl;
        rvfcActive = null;

        const parent = videoEl.parentElement;
        if (!parent) return;
        if (getComputedStyle(parent).position === "static") parent.style.position = "relative";

        if (!canvas) {
            canvas = document.createElement("canvas");
            canvas.id = "crealityUpscaleCanvas";
            canvas.style.cssText = "position:absolute;top:0;left:0;width:100%;height:100%;border-radius:inherit;pointer-events:none;z-index:5;";
            ctx2d = canvas.getContext("2d", { alpha: false });
        }
        if (canvas.parentElement !== parent) parent.appendChild(canvas);

        createUI(parent);
        connectAIWebSocket();
        startVideoFrameLoop(videoEl);

        if (animId) cancelAnimationFrame(animId);
        animId = requestAnimationFrame(renderLoop);
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
                #aiPanel{display:none;position:absolute;top:38px;right:0;width:318px;background:rgba(24,24,26,.97);backdrop-filter:blur(18px);border:1px solid rgba(255,255,255,.10);border-radius:12px;padding:16px;color:#e8e8ea;box-shadow:0 14px 36px rgba(0,0,0,.55);max-height:85vh;overflow-y:auto}
                #aiPanel.open{display:block}
                #aiPanel::-webkit-scrollbar{width:4px}
                #aiPanel::-webkit-scrollbar-thumb{background:#3a3a3d;border-radius:4px}
                .ai-head{display:flex;justify-content:space-between;align-items:center;padding-bottom:12px;margin-bottom:14px;border-bottom:1px solid rgba(255,255,255,.08)}
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
            </style>

            <div id="aiBadge"><span class="dot"></span><span id="badgeText">AI</span></div>

            <div id="aiPanel">
                <div class="ai-head">
                    <span class="ai-title">Enhancement AI</span>
                    <input type="checkbox" class="ai-switch" id="aiToggle" ${settings.enabled ? "checked" : ""}>
                </div>

                <div class="ai-stats">
                    <div class="ai-stat">Camera<b id="hudSrcFps">${srcFps} fps</b></div>
                    <div class="ai-stat">AI<b id="hudAiFps">${aiFps} fps</b></div>
                    <div class="ai-stat">GPU / totale<b id="hudLat">- ms</b></div>
                    <div class="ai-stat">Display<b id="hudDisplayFps">${displayFps} fps</b></div>
                    <div class="ai-stat" style="grid-column:span 2">Limite attuale degli fps AI<b id="hudLimit">-</b></div>
                </div>

                <div class="ai-section">Immagine</div>
                <div class="ai-hdr">
                    <div class="ai-row-head" style="margin-bottom:4px">
                        <span style="color:#f2f2f4;font-weight:600">HDR ombre</span>
                        <span class="ai-num"><input type="number" id="num_hdr" min="0" max="100" step="1" value="${pct(settings.hdrShadows)}">%</span>
                    </div>
                    <p class="ai-hdr-note">Schiarisce solo le zone scure (utile con PLA nero). Lascia invariate luci e mezzitoni.</p>
                    <input type="range" class="ai-range" id="slider_hdr" min="0" max="100" step="1" value="${pct(settings.hdrShadows)}" style="--v:${pct(settings.hdrShadows)}%">
                </div>
                ${slider("sharp", "Nitidezza", pct(settings.sharpness))}
                ${slider("blend", "Intensit&agrave; AI (0% = originale)", pct(settings.aiBlend))}
                ${slider("clarity", "Contrasto", pct(settings.clarity))}
                ${slider("bright", "Luminosit&agrave; (50% = neutra)", pct(settings.brightness + 0.5))}

                <div class="ai-section">Qualit&agrave;</div>
                <div class="ai-label">Risoluzione input AI</div>
                <select id="inputSelect" class="ai-select">
                    <option value="480" ${sel("480", settings.aiInput)}>Veloce &middot; 480 &times; 270</option>
                    <option value="640" ${sel("640", settings.aiInput)}>Bilanciata &middot; 640 &times; 360</option>
                    <option value="960" ${sel("960", settings.aiInput)}>Qualit&agrave; &middot; 960 &times; 540</option>
                </select>
                <div class="ai-label">Risoluzione output</div>
                <select id="resSelect" class="ai-select">
                    <option value="720p" ${sel("720p", settings.outRes)}>HD &middot; 1280 &times; 720</option>
                    <option value="1080p" ${sel("1080p", settings.outRes)}>Full HD &middot; 1920 &times; 1080</option>
                    <option value="1440p" ${sel("1440p", settings.outRes)}>QHD &middot; 2560 &times; 1440</option>
                </select>

                <div class="ai-section">Fluidit&agrave;</div>
                <div class="ai-switch-row" style="margin-bottom:12px">
                    <div>Smussamento<small>Dissolvenza tra frame AI, non &egrave; frame generation reale</small></div>
                    <input type="checkbox" class="ai-switch" id="fgToggle" ${settings.frameGenEnabled ? "checked" : ""}>
                </div>
                <select id="fpsSelect" class="ai-select">
                    <option value="auto" ${sel("auto", settings.targetFps)}>Frequenza del display</option>
                    <option value="60" ${sel("60", settings.targetFps)}>Limita a 60 Hz</option>
                    <option value="75" ${sel("75", settings.targetFps)}>Limita a 75 Hz</option>
                    <option value="120" ${sel("120", settings.targetFps)}>Limita a 120 Hz</option>
                    <option value="144" ${sel("144", settings.targetFps)}>Limita a 144 Hz</option>
                </select>

                <button class="ai-btn" id="btnReset">Ripristina predefiniti</button>
            </div>
        `;
        parent.appendChild(uiContainer);

        const q = (id) => uiContainer.querySelector(id);
        const badge = q("#aiBadge"), panel = q("#aiPanel"), badgeText = q("#badgeText");

        window.__updateUI = function () {
            if (!settings.enabled) { badge.className = "disabled"; badgeText.textContent = "AI disattivata"; }
            else if (wsConnected) { badge.className = ""; badgeText.textContent = `AI \u00b7 ${aiFps} fps`; }
            else { badge.className = "offline"; badgeText.textContent = "AI non attiva"; }
        };
        window.__updateHUD = function () {
            q("#hudSrcFps").textContent = `${srcFps} fps`;
            q("#hudAiFps").textContent = `${aiFps} fps`;
            q("#hudDisplayFps").textContent = `${displayFps} fps`;
            q("#hudLat").textContent = `${serverInferMs} / ${serverTotalMs} ms`;
            // The AI can never run faster than the camera delivers new frames.
            const src = parseFloat(srcFps), ai = parseFloat(aiFps);
            q("#hudLimit").textContent = !wsConnected || !(src > 0) ? "-"
                : ai >= src * 0.85 ? `Camera (${srcFps} fps in ingresso)`
                : "Elaborazione GPU/CPU (abbassa la qualità)";
        };

        badge.addEventListener("click", (e) => { e.stopPropagation(); panel.classList.toggle("open"); });
        document.addEventListener("click", (e) => { if (uiContainer && !uiContainer.contains(e.target)) panel.classList.remove("open"); });

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

        q("#aiToggle").addEventListener("change", (e) => { settings.enabled = e.target.checked; saveSettings(); updateBadge(); });
        q("#fgToggle").addEventListener("change", (e) => { settings.frameGenEnabled = e.target.checked; saveSettings(); });
        q("#fpsSelect").addEventListener("change", (e) => { settings.targetFps = e.target.value; saveSettings(); });
        q("#inputSelect").addEventListener("change", (e) => { settings.aiInput = e.target.value; saveSettings(); });
        q("#resSelect").addEventListener("change", (e) => { settings.outRes = e.target.value; saveSettings(); sendConfigToService(); });
        q("#btnReset").addEventListener("click", () => {
            settings = Object.assign({}, defaults);
            saveSettings();
            sendConfigToService();
            createUI(parent);
            uiContainer.querySelector("#aiPanel").classList.add("open");
        });

        window.__updateUI();
    }

    function updateBadge() { if (window.__updateUI) window.__updateUI(); }
    function updateHud() { if (window.__updateHUD) window.__updateHUD(); }

    // ---------------------------------------------------------------- Print speed slider
    // Injected into Creality's own "Printing speed" block, below the preset pills.
    let speedDragging = false;
    let speedDebounce = null;
    let printerWs = null;
    let sliderEl = null;
    let sliderValEl = null;

    function setSliderValue(v) {
        currentPrintSpeed = Math.max(25, Math.min(190, Math.round(v)));
        if (sliderEl && !speedDragging) sliderEl.value = currentPrintSpeed;
        if (sliderValEl) sliderValEl.textContent = `${currentPrintSpeed}%`;
    }

    function printerAddress(wrapper) {
        const inst = wrapper.__vueParentComponent;
        const addr = inst && inst.props && inst.props.address;
        return typeof addr === "string" && /^\d{1,3}(\.\d{1,3}){3}$/.test(addr) ? addr : null;
    }

    function openPrinterSocket(addr) {
        if (!addr) return null;
        if (printerWs && printerWs.__addr === addr && printerWs.readyState <= 1) return printerWs;
        try {
            const sock = new WebSocket(`ws://${addr}:9999`);
            sock.__addr = addr;
            sock.onclose = () => { if (printerWs === sock) printerWs = null; };
            printerWs = sock;
            return sock;
        } catch (e) { return null; }
    }

    // Same messages Creality Print itself sends for its preset buttons.
    function sendPrintSpeed(pct, addr) {
        const v = Math.max(25, Math.min(190, Math.round(pct)));
        const msgs = v === 25
            ? [{ method: "set", params: { speedMode: 1 } }]
            : [{ method: "set", params: { speedMode: 0 } }, { method: "set", params: { setFeedratePct: v } }];

        const viaBridge = () => fetch(`${API_URL}/printer/set_speed?speed=${v}`).catch(() => {});
        const sock = openPrinterSocket(addr);
        if (sock && sock.readyState === WebSocket.OPEN) {
            msgs.forEach((m) => sock.send(JSON.stringify(m)));
        } else if (sock && sock.readyState === WebSocket.CONNECTING) {
            const t = setTimeout(viaBridge, 1500);
            sock.addEventListener("open", () => { clearTimeout(t); msgs.forEach((m) => sock.send(JSON.stringify(m))); }, { once: true });
        } else {
            viaBridge();
        }
    }

    function injectSpeedSlider() {
        const wrapper = document.querySelector(".print-speed-wrapper");
        if (!wrapper) { sliderEl = null; return; }
        if (wrapper.querySelector(".agy-speed-fine")) return;

        if (!document.getElementById("agySpeedStyle")) {
            const st = document.createElement("style");
            st.id = "agySpeedStyle";
            st.textContent = `
                .agy-speed-fine{display:flex;align-items:center;gap:14px;margin-top:14px;padding:0 6px;font-size:14px}
                .agy-speed-fine .agy-lbl{white-space:nowrap;opacity:.8}
                .agy-speed-fine input[type=range]{flex:1;-webkit-appearance:none;appearance:none;height:6px;border-radius:3px;background:var(--primary-new-detail-print-speed-bg-color,#373739);outline:none;cursor:pointer}
                .agy-speed-fine input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:18px;height:18px;border-radius:50%;background:var(--primary-button-hover-color,#00d26a);cursor:pointer}
                .agy-speed-fine .agy-val{min-width:52px;text-align:right;font-weight:600;color:var(--primary-button-hover-color,#00d26a)}
            `;
            document.head.appendChild(st);
        }

        const row = document.createElement("div");
        row.className = "agy-speed-fine";
        row.innerHTML = `<span class="agy-lbl">Preciso</span><input type="range" min="25" max="190" step="1" value="${currentPrintSpeed}"><span class="agy-val">${currentPrintSpeed}%</span>`;
        wrapper.appendChild(row);

        sliderEl = row.querySelector("input");
        sliderValEl = row.querySelector(".agy-val");
        const addr = printerAddress(wrapper);

        sliderEl.addEventListener("input", () => {
            speedDragging = true;
            sliderValEl.textContent = `${sliderEl.value}%`;
            clearTimeout(speedDebounce);
            speedDebounce = setTimeout(() => sendPrintSpeed(Number(sliderEl.value), addr), 150);
        });
        sliderEl.addEventListener("change", () => {
            clearTimeout(speedDebounce);
            sendPrintSpeed(Number(sliderEl.value), addr);
            setTimeout(() => { speedDragging = false; }, 2500); // let the printer report back before we resync
        });

        fetch(`${API_URL}/printer/get_speed`).then((r) => r.json()).then((d) => setSliderValue(d.speed)).catch(() => {});
    }

    setInterval(() => {
        if (sliderEl && document.body.contains(sliderEl) && !speedDragging) {
            fetch(`${API_URL}/printer/get_speed`).then((r) => r.json()).then((d) => setSliderValue(d.speed)).catch(() => {});
        }
    }, 3000);

    // ---------------------------------------------------------------- Wiring
    function searchAndAttach() {
        const video = document.getElementById("remoteVideo") || document.querySelector("video.video-player") || document.querySelector("video");
        if (video) attachToVideo(video);
        injectSpeedSlider();
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
