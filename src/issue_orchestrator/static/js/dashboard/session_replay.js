let logPoller = null;
let logFollow = true;
let logIssue = null;
let logRunDir = null;
let logRecordingContext = null;
let sessionReplayState = null;

// Cap the idle gap honored between recorded events during playback. Real
// terminal output bursts have sub-100ms gaps and play at natural speed; long
// human-typing / idle pauses (which made playback look frozen at "0 / N
// events" — issue #6583) are compressed to this ceiling so the scrubber keeps
// advancing and early output is inspectable within a second of pressing Play.
const SESSION_REPLAY_MAX_IDLE_MS = 1000;

function clearDiagnosticsActionMessage() {
    const msg = document.getElementById('diagActionMessage');
    if (!msg) return;
    msg.textContent = '';
    msg.style.display = 'none';
}

function showDiagnosticsActionMessage(message) {
    const msg = document.getElementById('diagActionMessage');
    if (!msg) {
        showToast(message, 'error');
        return;
    }
    msg.textContent = String(message || 'Action failed');
    msg.style.display = 'block';
}

function reportActionError(message, surface = 'toast') {
    if (surface === 'inline') {
        showDiagnosticsActionMessage(message);
        return;
    }
    showToast(message, 'error');
}

function isNearBottom(element, threshold = 24) {
    return element.scrollTop + element.clientHeight >= element.scrollHeight - threshold;
}

async function refreshAgentLog(issueNumber, forceScroll = false, runDir = null) {
    const effectiveRunDir = runDir || logRunDir;
    if (!effectiveRunDir) {
        const msg = 'Session recording requires a run-scoped action (missing run_dir).';
        const statusEl = document.getElementById('sessionReplayStatus');
        if (statusEl) statusEl.textContent = msg;
        return;
    }
    const inTranscriptMode = sessionReplayState && sessionReplayState.mode === 'transcript';
    const request = uiActionContract.buildTerminalRecordingRequest(issueNumber, effectiveRunDir, {
        // Transcript mode carries the last transcript_hash so the backend
        // can short-circuit when the recording hasn't grown — no bytes on
        // the wire for a long codex session that's idle. Terminal mode
        // keeps the existing offset-based incremental fetch.
        offset: inTranscriptMode
            ? 0
            : (sessionReplayState ? sessionReplayState.events.length : 0),
        limit: 0,
        round_index: sessionReplayState && sessionReplayState.recordingContext
            ? sessionReplayState.recordingContext.round_index
            : null,
        session_role: sessionReplayState && sessionReplayState.recordingContext
            ? sessionReplayState.recordingContext.session_role
            : null,
        since_hash: inTranscriptMode ? (sessionReplayState.transcriptHash || '') : '',
    });
    const res = await fetch(request.endpoint, { method: request.method });
    const data = await res.json().catch(() => ({}));

    if (data.error) {
        const statusEl = document.getElementById('sessionReplayStatus');
        if (statusEl) statusEl.textContent = data.error;
        return;
    }

    if (!sessionReplayState || sessionReplayState.issueNumber !== issueNumber || sessionReplayState.runDir !== effectiveRunDir) {
        return;
    }
    if (data.unchanged) {
        // Recording hasn't grown since our last fetch; nothing to do.
        return;
    }
    if (resolveRenderMode(data) === 'transcript') {
        renderSessionTranscript(issueNumber, effectiveRunDir, data);
        return;
    }
    const incomingEvents = Array.isArray(data.events) ? data.events : [];
    if (!sessionReplayState.initialGeometry) {
        sessionReplayState.initialGeometry = resolveSessionReplayInitialGeometry(data, incomingEvents);
    }
    if (incomingEvents.length > 0) {
        const wasAtEnd = sessionReplayState.playbackIndex >= sessionReplayState.events.length;
        sessionReplayState.events.push(...incomingEvents);
        if (sessionReplayState.follow && (forceScroll || wasAtEnd) && !sessionReplayState.playing) {
            replaySessionToIndex(sessionReplayState.events.length);
        }
    }
    if (Array.isArray(data.chapters)) {
        // Chapters grow during a run as later rounds emit prompt/feedback
        // boundaries. Refresh the outline whenever the backend returns a
        // longer (or different) list so the user can jump to rounds that
        // didn't exist when the modal first opened.
        const previous = sessionReplayState.chapters || [];
        if (data.chapters.length !== previous.length) {
            sessionReplayState.chapters = data.chapters;
            renderSessionReplayChapters(sessionReplayState);
        }
    }
    const recordingPathEl = document.getElementById('sessionReplayPath');
    if (recordingPathEl) recordingPathEl.textContent = data.recording_path || '';
    updateSessionReplayUi();
}

async function openAgentLog(issueNumber, logLabel = 'Session Recording', runDir = null, errorSurface = 'toast', context = null) {
    if (!runDir) {
        reportActionError('Session recording requires run context. Open from a timeline entry.', errorSurface);
        return;
    }
    modalOverlay.querySelector('.modal').classList.remove('diagnostics-modal');
    clearDiagnosticsActionMessage();
    logIssue = issueNumber;
    logRunDir = runDir;
    logRecordingContext = context && (context.round_index || context.session_role) ? {
        round_index: Number.isInteger(Number(context.round_index)) ? Number(context.round_index) : null,
        session_role: context.session_role ? String(context.session_role).trim() : null,
    } : null;
    const request = uiActionContract.buildTerminalRecordingRequest(issueNumber, runDir, {
        offset: 0,
        limit: 0,
        round_index: logRecordingContext ? logRecordingContext.round_index : null,
        session_role: logRecordingContext ? logRecordingContext.session_role : null,
    });
    const res = await fetch(request.endpoint, { method: request.method });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) {
        reportActionError(data.error || `Failed to load session recording (HTTP ${res.status})`, errorSurface);
        return;
    }

    const logContent = `
        <div class="session-replay-shell" id="sessionReplayShell">
            <div class="session-replay-toolbar">
                <div class="session-replay-toolbar-main">
                    <button class="issue-action-btn" id="sessionReplayRestart" title="Jump back to the first event and start playing">Replay</button>
                    <button class="issue-action-btn" id="sessionReplayPlayPause" title="Play or pause replay of recorded output">Play</button>
                    <button class="issue-action-btn" id="sessionReplayJumpLive" title="Jump to the newest recorded output">Jump to latest</button>
                    <button class="issue-action-btn" id="sessionReplayRefresh" title="Fetch any newly recorded output">Refresh</button>
                </div>
                <div class="session-replay-toolbar-meta">
                    <label class="session-replay-control">
                        Speed
                        <select id="sessionReplaySpeed" title="Playback speed multiplier">
                            <option value="0.5">0.5x</option>
                            <option value="1" selected>1x</option>
                            <option value="2">2x</option>
                            <option value="4">4x</option>
                        </select>
                    </label>
                    <label class="session-replay-control">
                        <input type="checkbox" id="logFollowToggle" checked>
                        Follow live
                    </label>
                    <span class="session-replay-status" id="sessionReplayStatus" role="status" aria-live="polite"></span>
                </div>
            </div>
            <div class="session-replay-chapters" id="sessionReplayChapters" hidden></div>
            <div class="session-replay-progress">
                <input class="session-replay-seek" type="range" id="sessionReplaySeek" min="0" max="0" value="0" step="1" aria-label="Replay position (events)">
                <span class="session-replay-progress-text" id="sessionReplayProgressText">0 / 0 events</span>
                <span class="session-replay-meta" id="sessionReplayClock">0.0s</span>
            </div>
            <div class="session-replay-terminal-wrap">
                <div id="sessionReplayTerminal" class="session-replay-terminal"></div>
            </div>
            <div class="session-replay-hint">Replay restarts from the first event; Play/Pause and the scrubber move through recorded output, with long idle gaps shortened so progress stays visible. Follow live pins you to the newest output during active runs; chapters jump to round boundaries.</div>
            <div class="session-replay-prompt">
                <details>
                    <summary>Launch prompt</summary>
                    <div id="logPromptMeta" class="session-replay-meta"></div>
                    <pre id="logPromptPre"></pre>
                </details>
            </div>
            <div class="session-replay-meta">Recording: <span id="sessionReplayPath"></span></div>
        </div>
    `;

    document.getElementById('modalTitle').textContent = `${logLabel} #${issueNumber}`;
    document.getElementById('modalBody').innerHTML = logContent;
    document.getElementById('modalOverlay').classList.add('visible');

    initializeSessionReplay(issueNumber, runDir, data);

    const toggle = document.getElementById('logFollowToggle');
    if (toggle) {
        toggle.addEventListener('change', (e) => {
            logFollow = e.target.checked;
            if (sessionReplayState) {
                sessionReplayState.follow = logFollow;
            }
            updateSessionReplayUi();
        });
    }
    document.getElementById('sessionReplayRestart')?.addEventListener('click', () => restartSessionReplay(true));
    document.getElementById('sessionReplayPlayPause')?.addEventListener('click', () => toggleSessionReplayPlayback());
    document.getElementById('sessionReplayJumpLive')?.addEventListener('click', () => jumpSessionReplayToLatest());
    document.getElementById('sessionReplayRefresh')?.addEventListener('click', () => refreshAgentLog(issueNumber, true, runDir));
    document.getElementById('sessionReplaySeek')?.addEventListener('input', (event) => {
        pauseSessionReplay();
        const nextIndex = Number(event.target.value || 0);
        replaySessionToIndex(nextIndex);
    });
    document.getElementById('sessionReplaySpeed')?.addEventListener('change', (event) => {
        if (!sessionReplayState) return;
        sessionReplayState.speed = Number(event.target.value || 1) || 1;
        updateSessionReplayUi();
        if (sessionReplayState.playing) {
            scheduleSessionReplayStep();
        }
    });
    window.addEventListener('resize', fitSessionReplayTerminal);

    await refreshInlineSessionPrompt(issueNumber, runDir);
    if (logPoller) {
        clearInterval(logPoller);
    }
    logPoller = setInterval(() => {
        refreshAgentLog(issueNumber, false, logRunDir);
    }, 2000);
}

function openAgentLogAction(issueNumber, runDir = null, logLabel = 'Session Recording', errorSurface = 'toast', context = null) {
    return openAgentLog(issueNumber, logLabel, runDir, errorSurface, context);
}

async function openReviewTranscript(issueNumber, runDir = null, context = null, errorSurface = 'toast') {
    if (!runDir) {
        const message = 'Review transcript requires run-scoped context.';
        if (errorSurface === 'inline') {
            openModal(`Review Transcript #${issueNumber}`, `<p>${escapeHtml(message)}</p>`);
        } else {
            showToast(message, true);
        }
        return;
    }
    try {
        const params = new URLSearchParams({ run_dir: String(runDir) });
        const effectiveRound = Number(context && context.round_index);
        if (Number.isInteger(effectiveRound) && effectiveRound > 0) {
            params.set('round_index', String(effectiveRound));
        }
        const effectiveRole = context && context.transcript_role
            ? String(context.transcript_role).trim()
            : '';
        if (effectiveRole) {
            params.set('transcript_role', effectiveRole);
        }
        const res = await fetch(`/api/session/review-transcript/${issueNumber}?${params.toString()}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            const message = data.error || `Review transcript unavailable (HTTP ${res.status})`;
            if (errorSurface === 'inline') {
                openModal(`Review Transcript #${issueNumber}`, `<p>${escapeHtml(message)}</p>`);
            } else {
                showToast(message, true);
            }
            return;
        }
        const meta = data.transcript_path
            ? `<div class="session-replay-note">Transcript: ${escapeHtml(data.transcript_path)}</div>`
            : '';
        const content = typeof data.content === 'string' && data.content.length > 0
            ? escapeHtml(data.content)
            : '(empty)';
        const scopeLabel = typeof data.scope_label === 'string' && data.scope_label.trim()
            ? ` — ${escapeHtml(data.scope_label)}`
            : '';
        openModal(`Review Transcript #${data.issue_number}${scopeLabel}`, `${meta}<pre>${content}</pre>`);
    } catch (err) {
        const message = `Failed to load review transcript: ${err instanceof Error ? err.message : String(err)}`;
        if (errorSurface === 'inline') {
            openModal(`Review Transcript #${issueNumber}`, `<p>${escapeHtml(message)}</p>`);
        } else {
            showToast(message, true);
        }
    }
}

async function copyAgentLogAction(issueNumber, runDir = null) {
    if (!runDir) {
        showToast('No run-scoped session recording is available to copy', true);
        return;
    }
    try {
        const request = uiActionContract.buildTerminalRecordingRequest(issueNumber, runDir, { offset: 0, limit: 0 });
        const res = await fetch(request.endpoint, { method: request.method });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            throw new Error(data.error || `HTTP ${res.status}`);
        }
        const text = extractPlainTextFromRecordingEvents(Array.isArray(data.events) ? data.events : []);
        if (!text.trim()) {
            showToast('Session recording is empty', true);
            return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            showToast('Session recording copied');
            return;
        }
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast(ok ? 'Session recording copied' : 'Failed to copy', !ok);
    } catch (err) {
        showToast(`Failed to copy session recording: ${err instanceof Error ? err.message : String(err)}`, true);
    }
}

const VALID_RENDER_MODES = new Set(['terminal', 'transcript']);

function resolveRenderMode(payload) {
    // Whitelist the mode coming off the wire so a backend typo doesn't
    // silently fall through to the emulator with a transcript payload
    // (or vice-versa).
    const raw = payload && payload.render_mode;
    return VALID_RENDER_MODES.has(raw) ? raw : 'terminal';
}

function initializeSessionReplay(issueNumber, runDir, payload) {
    destroySessionReplay();
    const renderMode = resolveRenderMode(payload);
    if (renderMode === 'transcript') {
        renderSessionTranscript(issueNumber, runDir, payload);
        return;
    }
    const events = Array.isArray(payload.events) ? payload.events : [];
    const initialGeometry = resolveSessionReplayInitialGeometry(payload, events);
    sessionReplayState = {
        issueNumber,
        runDir,
        events,
        initialGeometry,
        recordingContext: logRecordingContext,
        playbackIndex: 0,
        playing: false,
        playTimer: null,
        speed: 1,
        follow: true,
        terminal: null,
        fitAddon: null,
        chapters: Array.isArray(payload.chapters) ? payload.chapters : null,
        recordingEventIndex: Number.isInteger(payload.recording_event_index)
            ? payload.recording_event_index
            : null,
    };
    logFollow = true;
    const pathEl = document.getElementById('sessionReplayPath');
    if (pathEl) pathEl.textContent = payload.recording_path || '';
    renderSessionReplayChapters(sessionReplayState);
    const terminalHost = document.getElementById('sessionReplayTerminal');
    if (!terminalHost) return;
    createSessionReplayTerminal();
    replaySessionToIndex(events.length);
}

function renderSessionReplayChapters(state) {
    // Persistent-runner exchanges write chapters.json next to each
    // role's recording. When the backend slices the role recording to
    // a specific round, it returns the full chapter outline plus the
    // absolute ``recording_event_index`` where this slice starts. We
    // surface both so the user can see "Round 2 → Coder Prompt"
    // without having to scrub the whole role recording. Whole-run
    // recordings (no chapters) keep the chapters drawer hidden.
    const host = document.getElementById('sessionReplayChapters');
    if (!host) return;
    const chapters = (state && Array.isArray(state.chapters)) ? state.chapters : null;
    if (!chapters || chapters.length === 0) {
        host.hidden = true;
        host.innerHTML = '';
        return;
    }
    const baseIndex = Number.isInteger(state.recordingEventIndex)
        ? state.recordingEventIndex
        : 0;
    host.hidden = false;
    host.innerHTML = '';
    const heading = document.createElement('div');
    heading.className = 'session-replay-chapters-title';
    heading.textContent = 'Chapters';
    host.appendChild(heading);
    const list = document.createElement('ul');
    list.className = 'session-replay-chapters-list';
    for (const chapter of chapters) {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'session-replay-chapter-link';
        const labelText = chapter && chapter.label
            ? String(chapter.label)
            : `Round ${chapter.cycle_index} ${chapter.section}`;
        button.textContent = labelText;
        // Translate the chapter's absolute index into a slice-relative
        // playback index so seeking to "Round 2 Prompt" lands on the
        // event at offset (chapter_index - slice_start). Out-of-window
        // chapters are still listed (so users can see what other
        // rounds exist) but their links no-op gracefully.
        const sliceIndex = Number(chapter.recording_event_index) - baseIndex;
        if (
            Number.isInteger(sliceIndex)
            && sliceIndex >= 0
            && sliceIndex <= state.events.length
        ) {
            button.addEventListener('click', () => {
                pauseSessionReplay();
                replaySessionToIndex(sliceIndex);
            });
        } else {
            button.disabled = true;
            button.title = 'Outside of the current round window';
        }
        item.appendChild(button);
        list.appendChild(item);
    }
    host.appendChild(list);
}

function renderSessionTranscript(issueNumber, runDir, payload) {
    // Codex ``exec --json`` captures a JSON event stream to the PTY; a terminal
    // emulator replay of those bytes renders as raw JSON envelopes (the
    // "Reviewer Session Recording" complaint). The backend dispatches on
    // format and pre-computes a human-facing transcript via the session-log
    // prettifier; we just render it as a scrollable monospace block and
    // disable the emulator-only controls so the toolbar stops lying about
    // what Play/Jump-to-latest would do.
    sessionReplayState = {
        issueNumber,
        runDir,
        mode: 'transcript',
        transcriptHash: payload.transcript_hash || null,
    };
    logFollow = false;
    const pathEl = document.getElementById('sessionReplayPath');
    if (pathEl) pathEl.textContent = payload.recording_path || '';
    const terminalHost = document.getElementById('sessionReplayTerminal');
    if (!terminalHost) return;
    const lines = Array.isArray(payload.transcript_lines) ? payload.transcript_lines : [];

    // Preserve the user's scroll offset across incremental refreshes. If the
    // viewer was at the bottom (e.g. first open or follow-like behaviour),
    // snap to the new bottom so newly-appended content is visible.
    const existingPre = terminalHost.querySelector('pre.session-replay-transcript');
    const wasAtBottom = existingPre
        ? (existingPre.scrollTop + existingPre.clientHeight >= existingPre.scrollHeight - 4)
        : true;
    const preservedScrollTop = existingPre ? existingPre.scrollTop : 0;

    const pre = document.createElement('pre');
    pre.className = 'session-replay-transcript';
    pre.textContent = lines.length
        ? lines.join('\n')
        : '(no transcript content — the underlying recording was empty)';
    terminalHost.innerHTML = '';
    terminalHost.appendChild(pre);
    if (wasAtBottom) {
        pre.scrollTop = pre.scrollHeight;
    } else {
        pre.scrollTop = preservedScrollTop;
    }
    const hint = document.querySelector('.session-replay-hint');
    if (hint) {
        hint.textContent = 'Codex JSON-stream recording rendered as a transcript. Replay controls disabled for this format.';
    }
    for (const buttonId of ['sessionReplayRestart', 'sessionReplayPlayPause', 'sessionReplayJumpLive']) {
        const button = document.getElementById(buttonId);
        if (button) button.disabled = true;
    }
    const seek = document.getElementById('sessionReplaySeek');
    if (seek) seek.disabled = true;
    const speed = document.getElementById('sessionReplaySpeed');
    if (speed) speed.disabled = true;
    const follow = document.getElementById('logFollowToggle');
    if (follow) follow.disabled = true;
    const status = document.getElementById('sessionReplayStatus');
    if (status) status.textContent = 'Transcript view';
    const shellEl = document.getElementById('sessionReplayShell');
    if (shellEl) shellEl.dataset.playbackState = 'transcript';
    const progress = document.getElementById('sessionReplayProgressText');
    if (progress) progress.textContent = `${lines.length} line(s)`;
    const clock = document.getElementById('sessionReplayClock');
    if (clock) clock.textContent = '';
}

function resolveSessionReplayInitialGeometry(payload, events) {
    const payloadGeometry = normalizeSessionReplayGeometry(payload?.initial_geometry);
    if (payloadGeometry) {
        return payloadGeometry;
    }
    for (const event of events || []) {
        const eventGeometry = normalizeSessionReplayGeometry(event);
        if (eventGeometry) {
            return eventGeometry;
        }
    }
    return null;
}

// Mirrors infra/terminal_recording.screen_dimension EXACTLY. The backend has
// one owner deciding what a trustworthy resize row carries; this is the third
// call site of that policy and the only one in another language, so it cannot
// import the owner and must restate it. The bounds are pinned against the
// Python constants by tests/unit/test_terminal_recording.py, which fails if
// either side moves (#7141 round 4).
//
// Number() coercion was the hole: bool is not a number but Number(true) is 1,
// so {rows: true} sized the viewer's terminal to a single row. Exact typeof
// checking rejects that; the range bound rejects zero, negative and absurd.
const SESSION_REPLAY_MAX_ROWS = 500;
const SESSION_REPLAY_MAX_COLS = 1000;

function sessionReplayScreenDimension(value, limit) {
    if (typeof value !== 'number' || !Number.isInteger(value)) return null;
    if (value < 1 || value > limit) return null;
    return value;
}

function normalizeSessionReplayGeometry(candidate) {
    if (!candidate || typeof candidate !== 'object') return null;
    const rows = sessionReplayScreenDimension(candidate.rows, SESSION_REPLAY_MAX_ROWS);
    const cols = sessionReplayScreenDimension(candidate.cols, SESSION_REPLAY_MAX_COLS);
    if (rows === null || cols === null) return null;
    return { rows, cols };
}

function createSessionReplayTerminal() {
    const host = document.getElementById('sessionReplayTerminal');
    if (!host || !sessionReplayState) return;
    if (sessionReplayState.terminal) {
        sessionReplayState.terminal.dispose();
    }
    host.innerHTML = '';
    const terminalOptions = {
        convertEol: false,
        cursorBlink: false,
        disableStdin: true,
        fontFamily: '"SFMono-Regular", "Menlo", "Consolas", monospace',
        fontSize: 12,
        scrollback: 10000,
        theme: {
            background: '#08111c',
            foreground: '#d7e2ef',
            cursor: '#4ea1ff',
            black: '#08111c',
            brightBlack: '#5b6f87',
            red: '#e57878',
            green: '#46c37b',
            yellow: '#f0b24f',
            blue: '#4ea1ff',
            magenta: '#9db4ff',
            cyan: '#62d5f5',
            white: '#d7e2ef',
            brightWhite: '#ffffff',
        },
    };
    if (sessionReplayState.initialGeometry) {
        terminalOptions.rows = sessionReplayState.initialGeometry.rows;
        terminalOptions.cols = sessionReplayState.initialGeometry.cols;
    }
    const terminal = new Terminal(terminalOptions);
    const fitAddon = new FitAddon.FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.open(host);
    sessionReplayState.terminal = terminal;
    sessionReplayState.fitAddon = fitAddon;
    fitSessionReplayTerminal();
}

function fitSessionReplayTerminal() {
    if (!sessionReplayState || !sessionReplayState.fitAddon) return;
    if (sessionReplayState.initialGeometry) return;
    try {
        sessionReplayState.fitAddon.fit();
    } catch (_err) {
        // Ignore fit errors while the modal is still laying out.
    }
}

function destroySessionReplay() {
    if (!sessionReplayState) return;
    if (sessionReplayState.playTimer) {
        clearTimeout(sessionReplayState.playTimer);
    }
    if (sessionReplayState.terminal) {
        sessionReplayState.terminal.dispose();
    }
    sessionReplayState = null;
    logRecordingContext = null;
}

function replaySessionToIndex(targetIndex) {
    if (!sessionReplayState) return;
    const clampedIndex = Math.max(0, Math.min(Number(targetIndex || 0), sessionReplayState.events.length));
    if (!sessionReplayState.terminal) {
        createSessionReplayTerminal();
    }
    if (clampedIndex < sessionReplayState.playbackIndex) {
        createSessionReplayTerminal();
        sessionReplayState.playbackIndex = 0;
    }
    for (let index = sessionReplayState.playbackIndex; index < clampedIndex; index += 1) {
        applyTerminalRecordingEvent(sessionReplayState.events[index]);
    }
    sessionReplayState.playbackIndex = clampedIndex;
    updateSessionReplayUi();
}

function applyTerminalRecordingEvent(event) {
    if (!sessionReplayState || !sessionReplayState.terminal || !event || typeof event !== 'object') return;
    if (event.event_type === 'resize') {
        // Same validator as the initial-geometry fallback: an untrustworthy
        // resize is treated as absent (skipped) rather than clamped, matching
        // the Python owner's reject-not-clamp contract. Passing a zero or
        // negative straight to xterm is how a malformed row reshaped the
        // viewer mid-playback (#7141 round 4).
        const geometry = normalizeSessionReplayGeometry(event);
        if (!geometry) return;
        sessionReplayState.initialGeometry = geometry;
        sessionReplayState.terminal.resize(geometry.cols, geometry.rows);
        return;
    }
    if (event.event_type !== 'output' || !event.data_b64) {
        return;
    }
    sessionReplayState.terminal.write(decodeTerminalRecordingData(event.data_b64));
}

function decodeTerminalRecordingData(dataB64) {
    const binary = atob(String(dataB64 || ''));
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
        bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
}

function extractPlainTextFromRecordingEvents(events) {
    const decoder = new TextDecoder();
    return (events || [])
        .filter(event => event && event.event_type === 'output' && event.data_b64)
        .map(event => decoder.decode(decodeTerminalRecordingData(event.data_b64)))
        .join('');
}

function restartSessionReplay(autoPlay = false) {
    pauseSessionReplay();
    replaySessionToIndex(0);
    if (autoPlay) {
        startSessionReplay();
    }
}

function jumpSessionReplayToLatest() {
    pauseSessionReplay();
    if (!sessionReplayState) return;
    replaySessionToIndex(sessionReplayState.events.length);
}

function toggleSessionReplayPlayback() {
    if (!sessionReplayState) return;
    if (sessionReplayState.playing) {
        pauseSessionReplay();
        return;
    }
    if (sessionReplayState.playbackIndex >= sessionReplayState.events.length) {
        replaySessionToIndex(0);
    }
    startSessionReplay();
}

function startSessionReplay() {
    if (!sessionReplayState) return;
    sessionReplayState.playing = true;
    scheduleSessionReplayStep();
    updateSessionReplayUi();
}

function pauseSessionReplay() {
    if (!sessionReplayState) return;
    sessionReplayState.playing = false;
    if (sessionReplayState.playTimer) {
        clearTimeout(sessionReplayState.playTimer);
        sessionReplayState.playTimer = null;
    }
    updateSessionReplayUi();
}

function computeSessionReplayStepDelay(events, index, speed) {
    // Delay (ms) before rendering ``events[index]`` during playback. The raw
    // gap is the offset difference from the previous event (or from zero for
    // the first event). We clamp that gap to [0, SESSION_REPLAY_MAX_IDLE_MS]
    // *before* applying the speed multiplier so long idle pauses collapse to a
    // short, visible advance instead of stalling the scrubber, while natural
    // sub-second output bursts keep their real cadence. Pure function so the
    // timing policy is unit-testable without a DOM or timers.
    const list = Array.isArray(events) ? events : [];
    if (!Number.isInteger(index) || index < 0 || index >= list.length) {
        return 0;
    }
    const previousOffset = index > 0 ? Number(list[index - 1]?.offset_ms || 0) : 0;
    const nextOffset = Number(list[index]?.offset_ms || 0);
    const rawGap = nextOffset - previousOffset;
    const idleGap = Number.isFinite(rawGap)
        ? Math.min(Math.max(rawGap, 0), SESSION_REPLAY_MAX_IDLE_MS)
        : 0;
    const effectiveSpeed = Math.max(Number(speed) || 1, 0.1);
    return Math.max(0, Math.round(idleGap / effectiveSpeed));
}

function scheduleSessionReplayStep() {
    if (!sessionReplayState) return;
    if (sessionReplayState.playTimer) {
        clearTimeout(sessionReplayState.playTimer);
        sessionReplayState.playTimer = null;
    }
    if (!sessionReplayState.playing) return;
    if (sessionReplayState.playbackIndex >= sessionReplayState.events.length) {
        sessionReplayState.playing = false;
        updateSessionReplayUi();
        return;
    }
    const nextIndex = sessionReplayState.playbackIndex;
    const delayMs = computeSessionReplayStepDelay(
        sessionReplayState.events,
        nextIndex,
        sessionReplayState.speed,
    );
    sessionReplayState.playTimer = setTimeout(() => {
        if (!sessionReplayState) return;
        applyTerminalRecordingEvent(sessionReplayState.events[nextIndex]);
        sessionReplayState.playbackIndex = nextIndex + 1;
        updateSessionReplayUi();
        scheduleSessionReplayStep();
    }, delayMs);
}

function describeSessionReplayPlayback(view) {
    // Map the current playback position to a stable state key plus a
    // human-facing label. The key drives ``data-playback-state`` (styling,
    // a11y, tests); the label is announced via the ``aria-live`` status. Both
    // stay in sync from one source so the viewer never looks ambiguously
    // "stuck": empty (no events), start, playing, paused, and end are all
    // named distinctly. Pure function — no DOM, so it is unit-testable.
    const total = Math.max(0, Number(view && view.total) || 0);
    const current = Math.max(0, Number(view && view.current) || 0);
    const playing = !!(view && view.playing);
    const follow = !!(view && view.follow);
    const speed = Number(view && view.speed) || 1;
    if (total === 0) {
        return { key: 'empty', label: 'No events recorded yet' };
    }
    if (playing) {
        return { key: 'playing', label: `Playing at ${speed}x — ${current} / ${total}` };
    }
    if (current >= total) {
        return follow
            ? { key: 'end', label: 'At latest output (following live)' }
            : { key: 'end', label: 'Paused at end' };
    }
    if (current <= 0) {
        return { key: 'start', label: 'At start — press Play to inspect early output' };
    }
    return { key: 'paused', label: `Paused at ${current} / ${total}` };
}

function updateSessionReplayEmptyState(isEmpty) {
    // A blank emulator with events loaded is normal (the first frames may
    // produce no visible output); a blank emulator with *zero* events is a
    // capture gap. Surfacing an explicit overlay lets the viewer tell the two
    // apart at a glance (issue #6583). The overlay sits above the xterm host
    // and is removed as soon as any output arrives.
    const host = document.getElementById('sessionReplayTerminal');
    if (!host) return;
    const existing = document.getElementById('sessionReplayEmpty');
    if (!isEmpty) {
        if (existing) existing.remove();
        return;
    }
    if (existing) return;
    const overlay = document.createElement('div');
    overlay.id = 'sessionReplayEmpty';
    overlay.className = 'session-replay-empty';
    overlay.textContent = 'No terminal output has been recorded for this run yet. '
        + 'The viewer is connected — output will appear here as it is captured.';
    host.appendChild(overlay);
}

function updateSessionReplayUi() {
    if (!sessionReplayState) return;
    const total = sessionReplayState.events.length;
    const current = sessionReplayState.playbackIndex;
    const seekEl = document.getElementById('sessionReplaySeek');
    const progressEl = document.getElementById('sessionReplayProgressText');
    const statusEl = document.getElementById('sessionReplayStatus');
    const clockEl = document.getElementById('sessionReplayClock');
    const playPauseEl = document.getElementById('sessionReplayPlayPause');
    const followToggleEl = document.getElementById('logFollowToggle');
    const shellEl = document.getElementById('sessionReplayShell');
    if (seekEl) {
        seekEl.max = String(total);
        seekEl.value = String(current);
        seekEl.setAttribute('aria-valuemax', String(total));
        seekEl.setAttribute('aria-valuenow', String(current));
    }
    if (progressEl) {
        progressEl.textContent = `${current} / ${total} events`;
    }
    if (clockEl) {
        const activeEvent = current > 0 ? sessionReplayState.events[current - 1] : sessionReplayState.events[0];
        const offsetMs = Number(activeEvent?.offset_ms || 0);
        clockEl.textContent = `${(offsetMs / 1000).toFixed(1)}s`;
    }
    const playback = describeSessionReplayPlayback({
        total,
        current,
        playing: sessionReplayState.playing,
        follow: sessionReplayState.follow,
        speed: sessionReplayState.speed,
    });
    if (statusEl) {
        statusEl.textContent = playback.label;
    }
    if (shellEl) {
        shellEl.dataset.playbackState = playback.key;
    }
    updateSessionReplayEmptyState(total === 0);
    if (playPauseEl) {
        playPauseEl.textContent = sessionReplayState.playing ? 'Pause' : 'Play';
    }
    if (followToggleEl) {
        followToggleEl.checked = !!sessionReplayState.follow;
    }
}

