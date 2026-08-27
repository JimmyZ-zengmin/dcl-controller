window.DclUsb = {
    ws: null,
    onMessage: null,
    connected: false,
    _reconnectTimer: null,
    _reconnectDelay: 3000,

    init() {
        this.connect();
    },

    connect() {
        if (this.ws && (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)) {
            return;
        }
        try {
            this.ws = new WebSocket('ws://localhost:8765');
        } catch (e) {
            console.error('USB WebSocket connect error:', e);
            this._scheduleReconnect();
            return;
        }

        this.ws.onopen = () => {
            this.connected = true;
            console.log('USB WebSocket connected');
            if (this.onMessage) {
                this.onMessage({ type: 'usb_connected' });
            }
        };

        this.ws.onclose = () => {
            this.connected = false;
            console.log('USB WebSocket disconnected');
            if (this.onMessage) {
                this.onMessage({ type: 'usb_disconnected' });
            }
            this._scheduleReconnect();
        };

        this.ws.onerror = (err) => {
            console.error('USB WebSocket error:', err);
        };

        this.ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (this.onMessage) {
                    this.onMessage(msg);
                }
            } catch (e) {
                console.error('USB message parse error:', e);
            }
        };
    },

    _scheduleReconnect() {
        if (this._reconnectTimer) return;
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            console.log('USB reconnecting...');
            this.connect();
        }, this._reconnectDelay);
    },

    send(obj) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn('USB WebSocket not connected, cannot send:', obj);
            return false;
        }
        try {
            this.ws.send(JSON.stringify(obj));
            return true;
        } catch (e) {
            console.error('USB send error:', e);
            return false;
        }
    },

    scan() {
        this.send({ cmd: 'scan' });
    },

    connectPort(name) {
        this.send({ cmd: 'connect', port: name });
    },

    disconnectPort() {
        this.send({ cmd: 'disconnect' });
    },

    deploy(binary) {
        this.send({ cmd: 'deploy', binary: binary });
    },

    startEngine() {
        this.send({ cmd: 'start' });
    },

    stopEngine() {
        this.send({ cmd: 'stop' });
    },

    resetEngine() {
        this.send({ cmd: 'reset' });
    },

    readWires(start, count) {
        this.send({ cmd: 'read_wires', start: start, count: count });
    },

    writeWire(idx, value) {
        this.send({ cmd: 'write_wire', index: idx, value: value });
    },

    getSymbolTable() {
        this.send({ cmd: 'get_symbol_table' });
    },

    compile(source) {
        this.send({ cmd: 'compile', source: source });
    }
};