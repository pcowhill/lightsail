/*
 * lightsail-demo: shared WebSocket helper for the chat, draw and game pages.
 *
 * - Builds the WebSocket URL from the current page: https pages use wss,
 *   http pages (local development) use ws, and the host is location.host.
 *   No production address or private backend port is embedded anywhere.
 * - Shows the connection state (connecting / connected / disconnected) and a
 *   manual Reconnect button. There is no automatic reconnect loop.
 * - send() is safe before the socket is open and after it has closed: the
 *   message is dropped and false is returned. Nothing is queued offline.
 * - connect() never attaches a second set of handlers: the previous socket's
 *   handlers are detached before a new socket is created.
 */
(function () {
  'use strict';

  var NOTICE =
    'Public shared demo: everyone connected right now sees the same content. ' +
    'This is not private messaging. Updates or restarts disconnect everyone and reset the server state.';

  function wsUrl(path) {
    var scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return scheme + '://' + window.location.host + path;
  }

  function mountStatus(options) {
    options = options || {};
    var root = document.createElement('div');
    root.className = 'demo-status' + (options.overlay ? ' demo-overlay' : '');
    root.setAttribute('data-state', 'connecting');

    var row = document.createElement('div');
    row.className = 'demo-status-row';
    var dot = document.createElement('span');
    dot.className = 'dot';
    dot.setAttribute('aria-hidden', 'true');
    var text = document.createElement('span');
    text.className = 'text';
    text.textContent = 'Connecting…';
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'reconnect';
    button.textContent = 'Reconnect';
    button.hidden = true;
    row.appendChild(dot);
    row.appendChild(text);
    row.appendChild(button);
    root.appendChild(row);

    var notice = document.createElement('p');
    notice.className = 'demo-notice';
    notice.textContent = NOTICE;
    root.appendChild(notice);

    var parent = options.parent || document.body;
    if (options.before) {
      parent.insertBefore(root, options.before);
    } else {
      parent.appendChild(root);
    }
    return { root: root, text: text, button: button };
  }

  function DemoSocket(path, handlers) {
    this.path = path;
    this.handlers = handlers || {};
    this.socket = null;
    this.status = mountStatus(this.handlers.status || {});
    var self = this;
    this.status.button.addEventListener('click', function () {
      self.connect();
    });
  }

  DemoSocket.prototype.setState = function (state, message) {
    this.status.root.setAttribute('data-state', state);
    this.status.text.textContent = message;
    this.status.button.hidden = state !== 'closed';
  };

  DemoSocket.prototype.isOpen = function () {
    return !!this.socket && this.socket.readyState === WebSocket.OPEN;
  };

  DemoSocket.prototype.detach = function () {
    if (!this.socket) return;
    this.socket.onopen = null;
    this.socket.onmessage = null;
    this.socket.onclose = null;
    this.socket.onerror = null;
    if (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING) {
      try { this.socket.close(); } catch (e) { /* already closing */ }
    }
    this.socket = null;
  };

  DemoSocket.prototype.connect = function () {
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) {
      return; // already connected or connecting; never open a second socket
    }
    this.detach();
    var self = this;
    var socket;
    this.setState('connecting', 'Connecting…');
    try {
      socket = new WebSocket(wsUrl(this.path));
    } catch (e) {
      this.setState('closed', 'Could not connect');
      return;
    }
    this.socket = socket;
    socket.onopen = function () {
      if (self.socket !== socket) return;
      self.setState('open', 'Connected');
      if (self.handlers.onOpen) self.handlers.onOpen();
    };
    socket.onmessage = function (event) {
      if (self.socket !== socket) return;
      var data;
      try {
        data = JSON.parse(event.data);
      } catch (e) {
        return; // ignore anything that is not JSON
      }
      if (!data || typeof data !== 'object' || typeof data.type !== 'string') return;
      if (self.handlers.onMessage) self.handlers.onMessage(data);
    };
    socket.onclose = function (event) {
      if (self.socket !== socket) return;
      var why = event.reason ? ' (' + event.reason + ')' : '';
      self.setState('closed', 'Disconnected' + why);
      if (self.handlers.onClose) self.handlers.onClose(event);
    };
    socket.onerror = function () {
      // onclose follows and reports the state; nothing else to do here.
    };
  };

  /** Send a JSON message. Returns false (and drops it) unless the socket is open. */
  DemoSocket.prototype.send = function (message) {
    if (!this.isOpen()) return false;
    try {
      this.socket.send(JSON.stringify(message));
      return true;
    } catch (e) {
      return false;
    }
  };

  window.LightsailDemo = {
    wsUrl: wsUrl,
    DemoSocket: DemoSocket,
    NOTICE: NOTICE
  };
})();
