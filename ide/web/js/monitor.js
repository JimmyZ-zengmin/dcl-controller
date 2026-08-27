window.DclMonitor = {
    active: false,
    timer: null,
    sampleRate: 100,       // ms between reads
    symbolTable: [],       // from compile result: [{name, wire_idx, fb_type, direction}, ...]
    wireData: [],          // latest wire values: [float, float, ...]
    waveHistory: {},       // idx -> [{time, value}, ...]  max 500 points
    chart: null,           // Chart.js instance
    startTime: null,
    subscribedSignals: [], // which wire indices to show on chart (max 10)
    signalColors: ['#4ec9b0', '#569cd6', '#dcdcaa', '#c586c0', '#ce9178',
                   '#b5cea8', '#f44747', '#6a9955', '#9cdcfe', '#c586c0'],

    init() {
        // Initialize Chart.js on the canvas element 'waveform-canvas'
        const ctx = document.getElementById('waveform-canvas');
        if (!ctx) { console.warn('waveform-canvas not found'); return; }
        this.chart = new Chart(ctx, {
            type: 'line',
            data: { labels: [], datasets: [] },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                scales: {
                    x: {
                        title: { display: true, text: 'Time (s)', color: '#888' },
                        ticks: { color: '#888', maxTicksLimit: 10 },
                        grid: { color: '#333' }
                    },
                    y: {
                        title: { display: true, text: 'Value', color: '#888' },
                        ticks: { color: '#888' },
                        grid: { color: '#333' }
                    }
                },
                plugins: {
                    legend: {
                        labels: { color: '#ccc', font: { size: 11 } }
                    }
                }
            }
        });
        this.startTime = Date.now();
    },

    toggle() {
        if (this.active) this.stop();
        else this.start();
    },

    start() {
        if (this.active) return;
        this.active = true;
        this.startTime = Date.now();
        // Clear wave history
        this.waveHistory = {};
        this._readLoop();
        // Update UI
        const btn = document.getElementById('btn-monitor-toggle');
        if (btn) btn.textContent = 'Stop Monitor';
    },

    stop() {
        this.active = false;
        if (this.timer) { clearTimeout(this.timer); this.timer = null; }
        const btn = document.getElementById('btn-monitor-toggle');
        if (btn) btn.textContent = 'Start Monitor';
    },

    setSampleRate(ms) {
        this.sampleRate = ms;
    },

    setSymbolTable(table) {
        this.symbolTable = table || [];
        // Auto-subscribe first 5 internal/output signals for waveform
        this.subscribedSignals = [];
        for (const s of this.symbolTable) {
            if (s.direction !== 'input' && this.subscribedSignals.length < 5) {
                this.subscribedSignals.push(s.wire_idx);
            }
        }
        this.updateTable();
    },

    onWireData(values) {
        // values is an array of floats [WIRE[0], WIRE[1], ...]
        this.wireData = values;
        const elapsed = ((Date.now() - this.startTime) / 1000).toFixed(2);

        // Update wave history for subscribed signals
        for (const idx of this.subscribedSignals) {
            if (idx < values.length) {
                if (!this.waveHistory[idx]) this.waveHistory[idx] = [];
                this.waveHistory[idx].push({ time: parseFloat(elapsed), value: values[idx] });
                // Keep max 500 points
                if (this.waveHistory[idx].length > 500) {
                    this.waveHistory[idx].shift();
                }
            }
        }

        this.updateTable();
        this.updateChart();
    },

    getNameForWire(idx) {
        // Translate wire index to signal name using symbol table
        const sym = this.symbolTable.find(s => s.wire_idx === idx);
        return sym ? sym.name : 'WIRE[' + idx + ']';
    },

    getFbTypeForWire(idx) {
        const sym = this.symbolTable.find(s => s.wire_idx === idx);
        return sym ? sym.fb_type : '-';
    },

    updateTable() {
        const tbody = document.getElementById('wire-table-body');
        if (!tbody) return;

        let html = '';
        const maxIdx = Math.max(this.wireData.length, this.symbolTable.length);
        for (let i = 0; i < Math.min(maxIdx, 64); i++) {
            const name = this.getNameForWire(i);
            const fb = this.getFbTypeForWire(i);
            const val = i < this.wireData.length ? this.wireData[i] : null;
            const valStr = val !== null ? val.toFixed(3) : '-';
            const isActive = val !== null && Math.abs(val) > 0.001;
            const isSubscribed = this.subscribedSignals.includes(i);

            html += '<tr>';
            html += '<td>' + name + '</td>';
            html += '<td>' + i + '</td>';
            html += '<td class="' + (isActive ? 'value-active' : 'value-zero') + '">' + valStr + '</td>';
            html += '<td>' + fb + '</td>';
            html += '<td><button class="btn-force" onclick="DclMonitor.forceWire(' + i + ')" title="Force value">F</button></td>';
            html += '<td><button class="btn-wave ' + (isSubscribed ? 'active' : '') + '" onclick="DclMonitor.toggleWave(' + i + ')" title="Toggle waveform">~</button></td>';
            html += '</tr>';
        }
        tbody.innerHTML = html;
    },

    updateChart() {
        if (!this.chart) return;

        // Build datasets from subscribed signals
        const datasets = [];
        let maxLen = 0;

        for (let si = 0; si < this.subscribedSignals.length; si++) {
            const idx = this.subscribedSignals[si];
            const history = this.waveHistory[idx] || [];
            const name = this.getNameForWire(idx);
            const color = this.signalColors[si % this.signalColors.length];

            if (history.length > maxLen) maxLen = history.length;

            datasets.push({
                label: name,
                data: history.map(h => ({ x: h.time, y: h.value })),
                borderColor: color,
                backgroundColor: 'transparent',
                borderWidth: 1.5,
                pointRadius: 0,
                tension: 0.1,
            });
        }

        this.chart.data.datasets = datasets;
        this.chart.update('none'); // no animation for performance
    },

    forceWire(idx) {
        const currentVal = idx < this.wireData.length ? this.wireData[idx] : 0;
        const input = prompt('Force ' + this.getNameForWire(idx) + ' (WIRE[' + idx + ']) to:', currentVal);
        if (input !== null && input !== '') {
            const val = parseFloat(input);
            if (!isNaN(val)) {
                DclUsb.writeWire(idx, val);
            }
        }
    },

    toggleWave(idx) {
        const pos = this.subscribedSignals.indexOf(idx);
        if (pos >= 0) {
            this.subscribedSignals.splice(pos, 1);
            delete this.waveHistory[idx];
        } else {
            if (this.subscribedSignals.length >= 10) {
                // Remove oldest subscription
                const removed = this.subscribedSignals.shift();
                delete this.waveHistory[removed];
            }
            this.subscribedSignals.push(idx);
            this.waveHistory[idx] = [];
        }
        this.updateTable();
    },

    _readLoop() {
        if (!this.active) return;
        DclUsb.readWires(0, Math.min(64, Math.max(this.wireData.length, this.symbolTable.length || 10)));
        this.timer = setTimeout(() => this._readLoop(), this.sampleRate);
    }
};