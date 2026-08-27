window.DclIDE = {
    compiledBinary: null,

    init() {
        DclUsb.init();
        DclEditor.init('monaco-editor');
        DclMonitor.init();
        DclProject.init();
        DclAI.init();
        this.bindEvents();
    },

    bindEvents() {
        // Menu bar buttons
        document.getElementById('btn-compile').addEventListener('click', () => this.compile());
        document.getElementById('btn-deploy').addEventListener('click', () => this.deploy());
        document.getElementById('btn-start').addEventListener('click', () => DclUsb.startEngine());
        document.getElementById('btn-stop').addEventListener('click', () => DclUsb.stopEngine());
        document.getElementById('btn-reset').addEventListener('click', () => DclUsb.resetEngine());
        document.getElementById('btn-scan-usb').addEventListener('click', () => DclUsb.scan());

        // USB port selection
        document.getElementById('usb-select').addEventListener('change', (e) => {
            const port = e.target.value;
            if (port) {
                DclUsb.connectPort(port);
            } else {
                DclUsb.disconnectPort();
            }
        });

        // Monitor controls
        document.getElementById('btn-monitor-toggle').addEventListener('click', () => DclMonitor.toggle());
        document.getElementById('sample-rate').addEventListener('change', (e) => {
            DclMonitor.setSampleRate(parseInt(e.target.value));
        });

        // Ctrl+B compile from editor
        document.addEventListener('dcl-compile', () => this.compile());

        // Real-time error check (debounced, from editor changes)
        window.addEventListener('dcl-check', (e) => {
            DclUsb.send({ cmd: 'check', source: e.detail });
        });

        // USB message handler
        DclUsb.onMessage = (msg) => this._handleMessage(msg);
    },

    _handleMessage(msg) {
        switch (msg.type) {
            case 'connected':
                document.getElementById('status-usb').textContent = 'USB: ' + (msg.port || 'Connected');
                break;

            case 'disconnected':
                document.getElementById('status-usb').textContent = 'USB: Disconnected';
                break;

            case 'ports':
                this._handleScanResult(msg);
                break;

            case 'compile_result':
                this._handleCompileResult(msg);
                break;

            case 'check_result':
                if (msg.errors && msg.errors.length > 0) {
                    DclEditor.setMarkers(msg.errors);
                } else {
                    DclEditor.clearMarkers();
                }
                break;

            case 'wires':
                DclMonitor.onWireData(msg.values || []);
                break;

            case 'heartbeat':
                document.getElementById('status-usb').textContent = 'USB: Connected';
                if (msg.running !== undefined) {
                    document.getElementById('status-isr').textContent =
                        msg.running ? 'ISR: Running' : 'ISR: Stopped';
                }
                document.getElementById('status-samples').textContent = 'SAMPLES: ' + (msg.samples || 0);
                break;

            case 'get_source_result':
            case 'source_changed':
                DclEditor.onSourceChanged(msg.source);
                break;

            case 'monitor_status':
                this._handleMonitorStatus(msg);
                break;

            case 'monitor_alert':
                this._handleMonitorAlert(msg);
                break;

            case 'deploy_sent':
                document.getElementById('status-compile').textContent = 'Deploy: ' + msg.size + ' bytes sent';
                break;

            case 'command_sent':
                document.getElementById('status-compile').textContent = msg.cmd + ' sent';
                break;

            case 'wire_written':
                document.getElementById('status-compile').textContent = 'WIRE[' + msg.idx + '] = ' + msg.value;
                break;

            case 'error':
                document.getElementById('status-compile').textContent = 'Error: ' + (msg.msg || 'unknown');
                console.error('Backend error:', msg.msg);
                break;

            case 'ai_response':
                DclAI.handleResponse(msg);
                break;

            case 'ai_key_set':
                document.getElementById('status-compile').textContent = 
                    'AI: ' + (msg.available ? 'Ready' : 'Not configured');
                break;
        }
    },

    _handleCompileResult(msg) {
        if (msg.success) {
            this.compiledBinary = msg.binary || null;
            document.getElementById('status-compile').textContent =
                'Compile OK: ' + (msg.stats ? msg.stats.routes + ' routes, ' + msg.stats.binary_size + ' bytes' : '');
            DclEditor.clearMarkers();

            if (msg.symbol_table) {
                DclMonitor.setSymbolTable(msg.symbol_table);
            }
        } else {
            this.compiledBinary = null;
            document.getElementById('status-compile').textContent = 'Compile: Failed';

            if (msg.errors && msg.errors.length > 0) {
                DclEditor.setMarkers(msg.errors);
            }
        }
    },

    _handleMonitorStatus(msg) {
        const s = msg;
        const el_samp = document.getElementById('monitor-samples');
        const el_jitter = document.getElementById('monitor-jitter');
        const el_engine = document.getElementById('monitor-engine');
        if (el_samp) el_samp.textContent = s.samples ?? '-';
        if (el_jitter) {
            const j = (s.period_max >= s.period_min) ? (s.period_max - s.period_min) : 0;
            el_jitter.textContent = j;
            el_jitter.style.color = j > 500 ? '#f44747' : j > 200 ? '#ff9800' : '#4ec9b0';
        }
        if (el_engine) {
            el_engine.textContent = s.engine_running ? 'RUNNING' : 'STOPPED';
            el_engine.style.color = s.engine_running ? '#4ec9b0' : '#f44747';
        }
    },

    _handleMonitorAlert(msg) {
        console.warn('Monitor alert:', msg);
    },

    _handleScanResult(msg) {
        const select = document.getElementById('usb-select');
        if (!select) return;
        select.innerHTML = '<option value="">-- USB Port --</option>';
        const ports = msg.ports || [];
        for (const port of ports) {
            const opt = document.createElement('option');
            opt.value = port.device;
            opt.textContent = port.description + (port.is_h723 ? ' (H723)' : '');
            if (port.is_h723) opt.selected = true;
            select.appendChild(opt);
        }
    },

    compile() {
        const source = DclEditor.getValue();
        document.getElementById('status-compile').textContent = 'Compile: ...';
        DclUsb.compile(source);
    },

    deploy() {
        if (!this.compiledBinary) {
            document.getElementById('status-compile').textContent = 'Compile: No binary';
            return;
        }
        DclUsb.deploy(this.compiledBinary);
    }
};

window.addEventListener('DOMContentLoaded', () => DclIDE.init());