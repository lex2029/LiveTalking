var pc = null;
var remoteStream = null;
var playoutStatsTimer = null;

function getQualityPreference() {
    if (typeof window.getQualityPreference === 'function') {
        return window.getQualityPreference();
    }
    return 'balanced';
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

function applyPlayoutDelayHint(valueSeconds) {
    if (!pc) return;
    pc.getReceivers().forEach((receiver) => {
        if (receiver && typeof receiver.playoutDelayHint !== 'undefined') {
            receiver.playoutDelayHint = valueSeconds;
        }
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
            const target = Math.min(0.4, Math.max(0.05, (jitter > 0 ? jitter * 2.0 : avg || 0.2)));
            applyPlayoutDelayHint(target);
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
        return fetch('/offer', {
            body: JSON.stringify({
                sdp: offer.sdp,
                type: offer.type,
                quality: quality,
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
        return pc.setRemoteDescription(answer);
    }).catch((e) => {
        alert(e);
    });
}

async function start() {
    var config = {
        sdpSemantics: 'unified-plan'
    };

    const iceServers = await fetchIceServers();
    if (iceServers.length > 0) {
        config.iceServers = iceServers;
        config.iceTransportPolicy = 'relay';
    } else if (document.getElementById('use-stun').checked) {
        config.iceServers = [{ urls: ['stun:stun.l.google.com:19302'] }];
    }

    pc = new RTCPeerConnection(config);
    remoteStream = new MediaStream();
    document.getElementById('video').srcObject = remoteStream;

    // connect audio / video
    pc.addEventListener('track', (evt) => {
        if (remoteStream) {
            remoteStream.addTrack(evt.track);
        }
        const mode = getPlayoutDelayMode();
        if (mode === 'auto') {
            startAutoPlayoutDelay();
        } else {
            const delay = getPlayoutDelaySeconds();
            if (evt.receiver && typeof evt.receiver.playoutDelayHint !== 'undefined') {
                evt.receiver.playoutDelayHint = delay;
            }
        }
    });
    pc.addEventListener('connectionstatechange', () => {
        if (!pc) return;
        if (pc.connectionState === 'connected') {
            if (typeof window.onWebRTCConnected === 'function') {
                window.onWebRTCConnected();
            }
        } else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected' || pc.connectionState === 'closed') {
            if (typeof window.onWebRTCDisconnected === 'function') {
                window.onWebRTCDisconnected();
            }
        }
    });

    document.getElementById('start').style.display = 'none';
    negotiate();
    document.getElementById('stop').style.display = 'inline-block';
}

function stop() {
    document.getElementById('stop').style.display = 'none';

    // close peer connection
    setTimeout(() => {
        if (pc) {
            pc.close();
            pc = null;
            stopAutoPlayoutDelay();
            if (typeof window.onWebRTCDisconnected === 'function') {
                window.onWebRTCDisconnected();
            }
        }
    }, 500);
}

window.onunload = function(event) {
    // 在这里执行你想要的操作
    setTimeout(() => {
        pc.close();
    }, 500);
};

window.onbeforeunload = function (e) {
        setTimeout(() => {
                pc.close();
            }, 500);
        e = e || window.event
        // 兼容IE8和Firefox 4之前的版本
        if (e) {
          e.returnValue = '关闭提示'
        }
        // Chrome, Safari, Firefox 4+, Opera 12+ , IE 9+
        return '关闭提示'
      }
