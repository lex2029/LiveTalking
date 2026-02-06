(function () {
  var script = document.currentScript;
  if (!script) return;

  var host = (script.dataset.host || '').replace(/\/+$/, '');
  if (!host) {
    console.error('LiveAvatar: data-host is required.');
    return;
  }

  var width = script.dataset.width || '720';
  var height = script.dataset.height || '520';
  var autostart = script.dataset.autostart || '0';
  var quality = script.dataset.quality || 'auto';
  var profile = script.dataset.profile || 'head';
  var playout = script.dataset.playout || 'auto';
  var playoutDelay = script.dataset.playoutDelay || '0.25';
  var targetId = script.dataset.target || '';

  var origin = window.location.origin || '';
  var src = host + '/widget.html'
    + '?autostart=' + encodeURIComponent(autostart)
    + '&quality=' + encodeURIComponent(quality)
    + '&profile=' + encodeURIComponent(profile)
    + '&playout=' + encodeURIComponent(playout)
    + '&playoutDelay=' + encodeURIComponent(playoutDelay)
    + '&origin=' + encodeURIComponent(origin);

  var iframe = document.createElement('iframe');
  iframe.src = src;
  iframe.allow = 'autoplay; fullscreen; picture-in-picture';
  iframe.style.border = '0';
  iframe.style.width = (/^\d+$/.test(width) ? width + 'px' : width);
  iframe.style.height = (/^\d+$/.test(height) ? height + 'px' : height);
  iframe.setAttribute('loading', 'lazy');
  iframe.setAttribute('allowfullscreen', '');

  var container = targetId ? document.getElementById(targetId) : script.parentNode;
  if (!container) {
    console.error('LiveAvatar: target container not found.');
    return;
  }
  container.appendChild(iframe);

  function postMessage(payload) {
    try {
      iframe.contentWindow.postMessage(payload, '*');
    } catch (e) {}
  }

  window.LiveAvatar = {
    send: function (text) {
      postMessage({ type: 'liveavatar:send', text: String(text || '') });
    },
    start: function () {
      postMessage({ type: 'liveavatar:start' });
    },
    stop: function () {
      postMessage({ type: 'liveavatar:stop' });
    },
    setQuality: function (q) {
      postMessage({ type: 'liveavatar:quality', quality: String(q || '') });
    }
  };
})();
