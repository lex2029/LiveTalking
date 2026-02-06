var pc = null;
var remoteStream = null;
var playoutStatsTimer = null;
var qualityAutoTimer = null;
var currentAutoQuality = null;
var statsHudTimer = null;

function getQualityPreference() {
    if (typeof window.getQualityPreference === 'function') {
        return window.getQualityPreference();
    }
    return 'balanced';
}

function getProfilePreference() {
    if (typeof window.getProfilePreference === 'function') {
        return window.getProfilePreference();
    }
    return 'head';
}

function getPlayoutDelaySeconds() {
    if (typeof window.getPlayoutDelaySeconds === 'function') {
        return window.getPlayoutDelaySeconds();
    }
    return 0;
}

function getPlayoutDelayMode() {
    if (typeof window.getPlayoutDelayMode === 'function') {
        return window.getPlayoutDelayMode();
    }
    return 'fixed';
}

function applyPlayoutDelayHint(valueSeconds, kind) {
    if (!pc) return;
    pc.getReceivers().forEach((receiver) => {
        if (!receiver || typeof receiver.playoutDelayHint === 'undefined') {
            return;
        }
        if (kind && receiver.track && receiver.track.kind !== kind) {
            return;
        }
        receiver.playoutDelayHint = valueSeconds;
    });
}

function startAutoPlayoutDelay() {
    if (!pc) return;
    if (playoutStatsTimer) clearInterval(playoutStatsTimer);
    playoutStatsTimer = setInterval(async () => {
        if (!pc) return;
        try {
            const stats = await pc.getStats();
            let audioReport = null;
            stats.forEach((report) => {
                if (report.type === 'inbound-rtp' && report.kind === 'audio') {
                    audioReport = report;
                }
            });
            if (!audioReport) return;
            let avg = 0.0;
            if (audioReport.jitterBufferDelay && audioReport.jitterBufferEmittedCount) {
                avg = audioReport.jitterBufferDelay / audioReport.jitterBufferEmittedCount;
            }
            const jitter = audioReport.jitter || 0.0;
            const lossRate = audioReport.packetsLost && audioReport.packetsReceived
                ? Math.min(1.0, audioReport.packetsLost / Math.max(1, audioReport.packetsLost + audioReport.packetsReceived))
                : 0.0;
            let target = (jitter > 0 ? jitter * 2.5 : avg || 0.2);
            if (lossRate > 0.02) target += 0.15;
            if (lossRate > 0.05) target += 0.2;
            target = Math.min(1.0, Math.max(0.1, target));
            // Apply same playout delay for audio+video to keep sync under jitter.
            applyPlayoutDelayHint(target, 'audio');
            applyPlayoutDelayHint(target, 'video');
        } catch (e) {
            // ignore
        }
    }, 3000);
}

function stopAutoPlayoutDelay() {
    if (playoutStatsTimer) {
        clearInterval(playoutStatsTimer);
        playoutStatsTimer = null;
    }
}

function applyPlayoutDelayNowInternal() {
    if (!pc) return;
    const mode = getPlayoutDelayMode();
    if (mode === 'auto') {
        startAutoPlayoutDelay();
        return;
    }
    stopAutoPlayoutDelay();
    const delay = getPlayoutDelaySeconds();
    applyPlayoutDelayHint(delay, 'audio');
    applyPlayoutDelayHint(delay, 'video');
}

function applyQualityNowInternal(quality) {
    if (!pc) return;
    const sid = parseInt(document.getElementById('sessionid').value || '0', 10);
    if (!sid) return;
    fetch('/webrtc_quality', {
        body: JSON.stringify({
            sessionid: sid,
            quality: quality
        }),
        headers: {
            'Content-Type': 'application/json'
        },
        method: 'POST'
    }).catch(() => {});
}

function decideAutoQuality(stats) {
    let rtt = 0.0;
    let lossRate = 0.0;
    let jitter = 0.0;
    let drops = 0.0;
    let available = 0.0;
    stats.forEach((report) => {
        if (report.type === 'candidate-pair' && report.state === 'succeeded' && (report.nominated || report.selected)) {
            if (typeof report.currentRoundTripTime === 'number') rtt = report.currentRoundTripTime;
            if (typeof report.availableIncomingBitrate === 'number') available = report.availableIncomingBitrate;
        }
        if (report.type === 'inbound-rtp' && report.kind === 'video') {
            if (typeof report.jitter === 'number') jitter = report.jitter;
            if (typeof report.packetsLost === 'number' && typeof report.packetsReceived === 'number') {
                lossRate = report.packetsLost / Math.max(1, report.packetsLost + report.packetsReceived);
            }
            if (typeof report.framesDropped === 'number' && typeof report.framesReceived === 'number') {
                drops = report.framesDropped / Math.max(1, report.framesReceived);
            }
        }
    });

    let qByBitrate = 'high';
    if (available > 0) {
        if (available < 200000) qByBitrate = 'emergency';
        else if (available < 350000) qByBitrate = 'very_low';
        else if (available < 700000) qByBitrate = 'low';
        else if (available < 1200000) qByBitrate = 'balanced';
    }

    let qByMetrics = 'high';
    if (lossRate > 0.15 || rtt > 0.7 || jitter > 0.12 || drops > 0.25) qByMetrics = 'emergency';
    else if (lossRate > 0.08 || rtt > 0.45 || jitter > 0.08 || drops > 0.12) qByMetrics = 'very_low';
    else if (lossRate > 0.04 || rtt > 0.30 || jitter > 0.05 || drops > 0.08) qByMetrics = 'low';
    else if (lossRate > 0.02 || rtt > 0.22 || jitter > 0.03 || drops > 0.04) qByMetrics = 'balanced';

    const order = ['emergency', 'very_low', 'low', 'balanced', 'high'];
    const byBitrateIndex = order.indexOf(qByBitrate);
    const byMetricsIndex = order.indexOf(qByMetrics);
    return order[Math.min(byBitrateIndex, byMetricsIndex)];
}

function startAutoQuality() {
    if (!pc) return;
    if (qualityAutoTimer) return;
    currentAutoQuality = null;
    qualityAutoTimer = setInterval(async () => {
        if (!pc) return;
        try {
            const stats = await pc.getStats();
            const quality = decideAutoQuality(stats);
            if (quality && quality !== currentAutoQuality) {
                currentAutoQuality = quality;
                applyQualityNowInternal(quality);
            }
        } catch (e) {
            // ignore
        }
    }, 3000);
}

function stopAutoQuality() {
    if (qualityAutoTimer) {
        clearInterval(qualityAutoTimer);
        qualityAutoTimer = null;
    }
}

function formatPercent(value) {
    if (typeof value !== 'number') return '';
    return (value * 100).toFixed(1) + '%';
}

function summarizeStats(stats) {
    let selectedPair = null;
    stats.forEach((report) => {
        if (report.type === 'candidate-pair' && report.state === 'succeeded' && (report.nominated || report.selected)) {
            selectedPair = report;
        }
    });

    let transport = '';
    let candidateType = '';
    let rttMs = null;
    let available = null;
    if (selectedPair) {
        const local = stats.get(selectedPair.localCandidateId);
        if (local) {
            transport = local.protocol || local.transport || '';
            candidateType = local.candidateType || '';
        }
        if (typeof selectedPair.currentRoundTripTime === 'number') {
            rttMs = Math.round(selectedPair.currentRoundTripTime * 1000);
        }
        if (typeof selectedPair.availableIncomingBitrate === 'number') {
            available = selectedPair.availableIncomingBitrate;
        }
    }

    let lossRate = null;
    let jitterMs = null;
    let bufferMs = null;
    stats.forEach((report) => {
        if (report.type === 'inbound-rtp' && report.kind === 'audio') {
            if (typeof report.jitter === 'number') jitterMs = Math.round(report.jitter * 1000);
            if (typeof report.packetsLost === 'number' && typeof report.packetsReceived === 'number') {
                lossRate = report.packetsLost / Math.max(1, report.packetsLost + report.packetsReceived);
            }
            if (report.jitterBufferDelay && report.jitterBufferEmittedCount) {
                bufferMs = Math.round((report.jitterBufferDelay / report.jitterBufferEmittedCount) * 1000);
            }
        }
    });

    const parts = [];
    if (transport) {
        const relay = candidateType ? ` ${candidateType}` : '';
        parts.push(`${transport.toUpperCase()}${relay}`);
    }
    if (rttMs !== null) parts.push(`RTT ${rttMs}ms`);
    if (lossRate !== null) parts.push(`loss ${formatPercent(lossRate)}`);
    if (jitterMs !== null) parts.push(`jitter ${jitterMs}ms`);
    if (bufferMs !== null) parts.push(`buf ${bufferMs}ms`);
    if (available) parts.push(`in ${Math.round(available / 1000)}kbps`);
    return parts.join(' • ');
}

function startStatsHud() {
    if (!pc) return;
    if (statsHudTimer) return;
    statsHudTimer = setInterval(async () => {
        if (!pc) return;
        try {
            const stats = await pc.getStats();
            const summary = summarizeStats(stats);
            if (typeof window.onWebRTCStats === 'function') {
                window.onWebRTCStats(summary);
            }
        } catch (e) {
            // ignore
        }
    }, 3000);
}

function stopStatsHud() {
    if (statsHudTimer) {
        clearInterval(statsHudTimer);
        statsHudTimer = null;
    }
    if (typeof window.onWebRTCStats === 'function') {
        window.onWebRTCStats('');
    }
}

function applyQualityForSelection() {
    const quality = getQualityPreference();
    if (quality === 'auto') {
        startAutoQuality();
        return;
    }
    stopAutoQuality();
    applyQualityNowInternal(quality);
}

async function fetchIceServers() {
    try {
        const response = await fetch('/ice');
        const data = await response.json();
        if (data && Array.isArray(data.iceServers) && data.iceServers.length > 0) {
            return data.iceServers;
        }
    } catch (e) {
        console.log('Failed to fetch ICE servers:', e);
    }
    return [];
}

function negotiate() {
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });
    return pc.createOffer().then((offer) => {
        return pc.setLocalDescription(offer);
    }).then(() => {
        // wait for ICE gathering to complete
        return new Promise((resolve) => {
            if (pc.iceGatheringState === 'complete') {
                resolve();
            } else {
                const checkState = () => {
                    if (pc.iceGatheringState === 'complete') {
                        pc.removeEventListener('icegatheringstatechange', checkState);
                        resolve();
                    }
                };
                pc.addEventListener('icegatheringstatechange', checkState);
            }
        });
    }).then(() => {
        var offer = pc.localDescription;
        var quality = getQualityPreference();
        var profile = getProfilePreference();
        return fetch('/offer', {
            body: JSON.stringify({
                sdp: offer.sdp,
                type: offer.type,
                quality: quality,
                profile: profile,
            }),
            headers: {
                'Content-Type': 'application/json'
            },
            method: 'POST'
        });
    }).then(async (response) => {
        const text = await response.text();
        if (!response.ok) {
            throw new Error(text || `HTTP ${response.status}`);
        }
        let answer = null;
        try {
            answer = JSON.parse(text);
        } catch (e) {
            throw new Error(`Invalid JSON from /offer: ${text.slice(0, 200)}`);
        }
        if (answer && typeof answer === 'object' && answer.code < 0) {
            throw new Error(answer.msg || 'Offer failed');
        }
        document.getElementById('sessionid').value = answer.sessionid || 0;
        if (typeof window.onSessionReady === 'function') {
            window.onSessionReady(answer.sessionid);
        }
        applyQualityForSelection();
        return pc.setRemoteDescription(answer);
    }).catch((e) => {
        alert(e);
    });
}

async function start() {
    var config = {
        sdpSemantics: 'unified-plan',
        bundlePolicy: 'max-bundle',
        iceCandidatePoolSize: 2
    };

    const iceServers = await fetchIceServers();
    if (iceServers.length > 0) {
        config.iceServers = iceServers;
        // If user forces TURN-only, use relay, otherwise allow direct for faster connect.
        const useStunEl = document.getElementById('use-stun');
        if (useStunEl && useStunEl.checked) {
            config.iceTransportPolicy = 'relay';
        } else {
            config.iceTransportPolicy = 'all';
        }
    } else {
        // Fallback to Google STUN if TURN is unavailable.
        config.iceServers = [{ urls: ['stun:stun.l.google.com:19302'] }];
    }

    pc = new RTCPeerConnection(config);
    remoteStream = new MediaStream();
    const videoEl = document.getElementById('video');
    if (videoEl) videoEl.srcObject = remoteStream;

    // connect audio / video
    pc.addEventListener('track', (evt) => {
        if (remoteStream) {
            remoteStream.addTrack(evt.track);
        }
        applyPlayoutDelayNowInternal();
    });
    pc.addEventListener('connectionstatechange', () => {
        if (!pc) return;
        if (pc.connectionState === 'connected') {
            startStatsHud();
            if (typeof window.onWebRTCConnected === 'function') {
                window.onWebRTCConnected();
            }
        } else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected' || pc.connectionState === 'closed') {
            stopStatsHud();
            if (typeof window.onWebRTCDisconnected === 'function') {
                window.onWebRTCDisconnected();
            }
        }
    });

    const startBtn = document.getElementById('start');
    if (startBtn) startBtn.style.display = 'none';
    negotiate();
    const stopBtn = document.getElementById('stop');
    if (stopBtn) stopBtn.style.display = 'inline-block';
}

function stop() {
    const stopBtn = document.getElementById('stop');
    if (stopBtn) stopBtn.style.display = 'none';

    // close peer connection
    setTimeout(() => {
        if (pc) {
            const sid = parseInt(document.getElementById('sessionid').value || '0', 10);
            if (sid) {
                fetch('/end_session', {
                    body: JSON.stringify({ sessionid: sid }),
                    headers: { 'Content-Type': 'application/json' },
                    method: 'POST'
                }).catch(() => {});
            }
            pc.close();
            pc = null;
            stopAutoPlayoutDelay();
            stopAutoQuality();
            stopStatsHud();
            if (typeof window.onWebRTCDisconnected === 'function') {
                window.onWebRTCDisconnected();
            }
        }
    }, 500);
}

window.applyQualityNow = function() {
    applyQualityForSelection();
};

window.applyPlayoutDelayNow = function() {
    applyPlayoutDelayNowInternal();
};

window.onunload = function(event) {
    // 在这里执行你想要的操作
    try {
        const sid = parseInt(document.getElementById('sessionid').value || '0', 10);
        if (sid && navigator.sendBeacon) {
            const blob = new Blob([JSON.stringify({ sessionid: sid })], { type: 'application/json' });
            navigator.sendBeacon('/end_session', blob);
        }
    } catch (e) {}
    setTimeout(() => {
        if (pc) pc.close();
    }, 500);
};

window.onbeforeunload = function (e) {
        try {
            const sid = parseInt(document.getElementById('sessionid').value || '0', 10);
            if (sid && navigator.sendBeacon) {
                const blob = new Blob([JSON.stringify({ sessionid: sid })], { type: 'application/json' });
                navigator.sendBeacon('/end_session', blob);
            }
        } catch (e) {}
        setTimeout(() => {
                if (pc) pc.close();
            }, 500);
        e = e || window.event
        // 兼容IE8和Firefox 4之前的版本
        if (e) {
          e.returnValue = '关闭提示'
        }
        // Chrome, Safari, Firefox 4+, Opera 12+ , IE 9+
        return '关闭提示'
      }
