var callObject = null;
var remoteStream = null;
var qualityState = {
    current: 'high',
    mode: 'auto',
    goodStreak: 0,
    warnStreak: 0,
    badStreak: 0,
    lastNetworkState: 'unknown'
};

var QUALITY_RANK = { low: 0, med: 1, high: 2 };
var QUALITY_PROFILES = {
    high: { bandwidthKbps: 2500, spatialLayer: 2, temporalLayer: 2 },
    med: { bandwidthKbps: 1200, spatialLayer: 1, temporalLayer: 1 },
    low: { bandwidthKbps: 450, spatialLayer: 0, temporalLayer: 0 }
};

function handleRemoteGone(reason) {
    if (!remoteStream) return;
    if (remoteStream.getTracks().length > 0) return;
    window.__dailyConnected = false;
    if (typeof window.onPeerDisconnected === 'function') {
        window.onPeerDisconnected(reason || 'remote-left');
    } else if (typeof window.onWebRTCDisconnected === 'function') {
        window.onWebRTCDisconnected();
    }
}

function updateQualityStatus() {
    try {
        var el = document.getElementById('status-details');
        if (!el) return;
        var text = 'Качество: AUTO ' + qualityState.current.toUpperCase();
        if (qualityState.lastNetworkState && qualityState.lastNetworkState !== 'unknown') {
            text += ' • сеть: ' + qualityState.lastNetworkState;
        }
        el.textContent = text;
    } catch (e) {}
}

function applyQualityProfile(profile, source, reason) {
    if (!profile || !QUALITY_PROFILES[profile]) return;
    if (!callObject) return;

    if (source === 'bot') {
        if (QUALITY_RANK[profile] >= QUALITY_RANK[qualityState.current]) {
            return; // only lower on bot hints
        }
    }

    if (profile === qualityState.current) return;
    qualityState.current = profile;
    updateQualityStatus();

    var cfg = QUALITY_PROFILES[profile];
    try {
        if (typeof callObject.updateReceiveSettings === 'function') {
            callObject.updateReceiveSettings({
                base: {
                    video: {
                        maxSpatialLayer: cfg.spatialLayer,
                        maxTemporalLayer: cfg.temporalLayer
                    }
                }
            });
        }
    } catch (e) {}
    try {
        if (typeof callObject.setBandwidth === 'function') {
            callObject.setBandwidth({ kbs: cfg.bandwidthKbps });
        }
    } catch (e) {}
}

function handleNetworkState(state) {
    if (!state) return;
    var s = String(state).toLowerCase();
    qualityState.lastNetworkState = s;

    if (s === 'bad' || s === 'poor' || s === 'very-bad' || s === 'very-poor') {
        qualityState.badStreak += 1;
        qualityState.warnStreak = 0;
        qualityState.goodStreak = 0;
    } else if (s === 'warning' || s === 'warn' || s === 'fair') {
        qualityState.warnStreak += 1;
        qualityState.badStreak = 0;
        qualityState.goodStreak = 0;
    } else if (s === 'good' || s === 'excellent') {
        qualityState.goodStreak += 1;
        qualityState.warnStreak = 0;
        qualityState.badStreak = 0;
    } else {
        qualityState.goodStreak = 0;
        qualityState.warnStreak = 0;
        qualityState.badStreak = 0;
    }

    if (qualityState.badStreak >= 2) {
        applyQualityProfile('low', 'auto', 'bad');
    } else if (qualityState.warnStreak >= 1) {
        applyQualityProfile('med', 'auto', 'warn');
    } else if (qualityState.goodStreak >= 8) {
        applyQualityProfile('high', 'auto', 'good');
    }
}

function getProfilePreference() {
    if (typeof window.getProfilePreference === 'function') {
        return window.getProfilePreference();
    }
    return 'head';
}

async function start() {
    var profile = getProfilePreference();
    var attempts = 0;
    var data = null;
    var lastError = '';

    while (attempts < 3) {
        attempts += 1;
        var response = await fetch('/daily/start', {
            body: JSON.stringify({ profile: profile }),
            headers: { 'Content-Type': 'application/json' },
            method: 'POST'
        });

        var text = await response.text();
        try {
            data = JSON.parse(text);
        } catch (e) {
            lastError = text.slice(0, 200);
            if (attempts < 3) {
                await new Promise(function(r) { setTimeout(r, 700 * attempts); });
                continue;
            }
            alert('Daily start failed: ' + lastError);
            return false;
        }

        if (!response.ok || !data || data.code < 0) {
            lastError = data && data.msg ? data.msg : 'Daily start failed';
            if (attempts < 3) {
                await new Promise(function(r) { setTimeout(r, 700 * attempts); });
                continue;
            }
            alert(lastError);
            return false;
        }

        break;
    }

    document.getElementById('sessionid').value = data.sessionid || 0;

    if (typeof window.onSessionReady === 'function') {
        window.onSessionReady(data.sessionid);
    }

    callObject = Daily.createCallObject({
        audioSource: false,
        videoSource: false,
        startAudioOff: true,
        startVideoOff: true,
        subscribeToTracksAutomatically: true
    });
    remoteStream = new MediaStream();
    var videoEl = document.getElementById('video');
    videoEl.srcObject = remoteStream;

    callObject.on('track-started', function (ev) {
        if (ev.participant && ev.participant.local) {
            return;
        }
        if (ev.track && (ev.track.kind === 'video' || ev.track.kind === 'audio')) {
            remoteStream.addTrack(ev.track);
        }
    });

    callObject.on('track-stopped', function (ev) {
        if (ev.participant && ev.participant.local) {
            return;
        }
        if (ev.track) {
            remoteStream.removeTrack(ev.track);
            handleRemoteGone('remote-track-stopped');
        }
    });

    callObject.on('joined-meeting', function () {
        window.__dailyConnected = true;
        if (typeof window.onWebRTCConnected === 'function') {
            window.onWebRTCConnected();
        }
    });

    callObject.on('left-meeting', function () {
        window.__dailyConnected = false;
        if (typeof window.onWebRTCDisconnected === 'function') {
            window.onWebRTCDisconnected();
        }
    });

    callObject.on('participant-left', function (ev) {
        if (ev && ev.participant && ev.participant.local) return;
        handleRemoteGone('remote-left');
    });

    callObject.on('error', function (ev) {
        alert('Daily error: ' + (ev && ev.errorMsg ? ev.errorMsg : 'unknown'));
    });

    callObject.on('app-message', function (ev) {
        var msg = ev && (ev.data || ev.message || ev);
        if (msg && msg.type === 'QUALITY_PROFILE' && msg.profile) {
            applyQualityProfile(String(msg.profile).toLowerCase(), 'bot', msg.reason || '');
        }
    });

    callObject.on('network-quality-change', function (ev) {
        var state = ev && (ev.quality || ev.networkState || ev.networkState || ev);
        handleNetworkState(state);
        updateQualityStatus();
    });

    await callObject.join({
        url: data.room_url,
        token: data.token,
        subscribeToTracksAutomatically: true,
    });

    applyQualityProfile('high', 'auto', 'init');
    updateQualityStatus();

    if (typeof callObject.getNetworkStats === 'function') {
        setInterval(function () {
            try {
                var stats = callObject.getNetworkStats();
                if (stats && typeof stats.then === 'function') {
                    stats.then(function (s) {
                        var state = s && (s.networkState || s.quality || (s.stats && s.stats.networkState));
                        handleNetworkState(state);
                        updateQualityStatus();
                    }).catch(function () {});
                } else {
                    var state = stats && (stats.networkState || stats.quality || (stats.stats && stats.stats.networkState));
                    handleNetworkState(state);
                    updateQualityStatus();
                }
            } catch (e) {}
        }, 2000);
    }

    try {
        callObject.setLocalAudio(false);
        callObject.setLocalVideo(false);
    } catch (e) {}
    return true;
}

function stop() {
    document.getElementById('stop').style.display = 'none';
    if (callObject) {
        callObject.leave();
        callObject = null;
    }
    if (typeof window.onWebRTCDisconnected === 'function') {
        window.onWebRTCDisconnected();
    }
    window.__dailyConnected = false;

    const sid = parseInt(document.getElementById('sessionid').value || '0', 10);
    if (sid) {
        fetch('/end_session', {
            body: JSON.stringify({ sessionid: sid }),
            headers: { 'Content-Type': 'application/json' },
            method: 'POST'
        }).catch(() => {});
    }
}

window.applyQualityNow = function() {};
window.applyPlayoutDelayNow = function() {};

window.onunload = function() {
    try {
        const sid = parseInt(document.getElementById('sessionid').value || '0', 10);
        if (sid && navigator.sendBeacon) {
            const blob = new Blob([JSON.stringify({ sessionid: sid })], { type: 'application/json' });
            navigator.sendBeacon('/end_session', blob);
        }
    } catch (e) {}
};
