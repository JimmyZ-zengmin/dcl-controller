window.DclEditor = {
    editor: null,

    DEFAULT_PROGRAM: `# DCL Sample - Temperature PID Control
SENSOR temp FROM ADC1_CH0
FILTER temp_f FROM temp LOWPASS a=0.1
PID heater FROM temp_f SP=60 KP=2.0 KI=0.1 KD=0.05 LIMIT 0 100
ALARM overheat FROM temp_f > 80
OUTPUT heat_pwm TO TIM1_CH1 FROM heater`,

    init(containerId) {
        require.config({ paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs' } });

        require(['vs/editor/editor.main'], () => {
            this._registerLanguage();
            this._registerTheme();
            this._createEditor(containerId);
            this._registerCompletion();
            this._registerKeybinding();
        });
    },

    _registerTheme() {
        monaco.editor.defineTheme('dcl-dark', {
            base: 'vs-dark',
            inherit: true,
            rules: [
                { token: 'keyword.fb', foreground: '569cd6' },
                { token: 'keyword.op', foreground: 'c586c0' },
                { token: 'keyword.param', foreground: '9cdcfe' },
                { token: 'string', foreground: 'ce9178' },
                { token: 'comment', foreground: '6a9955' },
                { token: 'number', foreground: 'b5cea8' },
                { token: 'identifier', foreground: 'dcdcaa' },
            ],
            colors: {
                'editor.background': '#1e1e1e',
                'editor.foreground': '#cccccc',
                'editorLineNumber.foreground': '#858585',
                'editorLineNumber.activeForeground': '#cccccc',
                'editor.selectionBackground': '#264f78',
                'editor.lineHighlightBackground': '#2a2d2e',
            }
        });
    },

    _createEditor(containerId) {
        this.editor = monaco.editor.create(document.getElementById(containerId), {
            value: this.DEFAULT_PROGRAM,
            language: 'dcl',
            theme: 'dcl-dark',
            fontSize: 14,
            fontFamily: "'Consolas', 'Courier New', monospace",
            minimap: { enabled: true },
            automaticLayout: true,
            scrollBeyondLastLine: false,
            renderWhitespace: 'selection',
            lineNumbers: 'on',
            roundedSelection: false,
            cursorStyle: 'line',
            tabSize: 4,
        });
    },

    _registerKeybinding() {
        this.editor.addAction({
            id: 'dcl-compile',
            label: 'Compile DCL',
            keybindings: [monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyB],
            run: () => {
                document.dispatchEvent(new CustomEvent('dcl-compile'));
            }
        });
    },

    _registerLanguage() {
        monaco.languages.register({ id: 'dcl' });

        monaco.languages.setMonarchTokensProvider('dcl', {
            keywords_fb: [
                'SENSOR', 'FILTER', 'PID', 'TIMER', 'COUNTER', 'LATCH',
                'ALARM', 'LOGIC', 'OUTPUT', 'RATE', 'DEADBAND', 'SCALE'
            ],
            keywords_op: [
                'FROM', 'TO', 'LOWPASS', 'LIMIT', 'RANGE', 'OR', 'AND', 'NOT'
            ],
            keywords_param: [
                'IN', 'PT', 'PV', 'SP', 'KP', 'KI', 'KD', 'CU', 'CD',
                'R', 'S1', 'R1', 'Q', 'Q1', 'CV', 'QU', 'QD', 'MODE'
            ],
            operators: ['=', '>', '<', '>=', '<=', '!=', '+', '-', '*', '/'],

            tokenizer: {
                root: [
                    { include: '@whitespace' },
                    { include: '@comments' },
                    [/\b[A-Z_][A-Z0-9_]*\b/, {
                        cases: {
                            '@keywords_fb': 'keyword.fb',
                            '@keywords_op': 'keyword.op',
                            '@keywords_param': 'keyword.param',
                            '@default': 'identifier'
                        }
                    }],
                    [/\b[a-z_][a-z0-9_]*\b/, 'identifier'],
                    [/\b\d+\.\d+\b/, 'number'],
                    [/\b\d+\b/, 'number'],
                    [/[=><!+\-*/]+/, 'operator'],
                ],
                whitespace: [
                    [/\s+/, 'white'],
                ],
                comments: [
                    [/#.*$/, 'comment'],
                    [/\/\/.*$/, 'comment'],
                ],
            }
        });
    },

    _registerCompletion() {
        const fbSnippets = {
            SENSOR: 'SENSOR ${1:name} FROM ${2:source}',
            FILTER: 'FILTER ${1:name} FROM ${2:input} LOWPASS a=${3:0.1}',
            PID: 'PID ${1:name} FROM ${2:input} SP=${3:50} KP=${4:1.0} KI=${5:0.1} KD=${6:0.05} LIMIT ${7:0} ${8:100}',
            TIMER: 'TIMER ${1:name} PT=${2:1000}',
            COUNTER: 'COUNTER ${1:name} CU=${2:input} R=${3:reset}',
            LATCH: 'LATCH ${1:name} S=${2:set} R1=${3:reset}',
            ALARM: 'ALARM ${1:name} FROM ${2:input} > ${3:threshold}',
            LOGIC: 'LOGIC ${1:name} ${2:AND|OR|NOT} ${3:inputs}',
            OUTPUT: 'OUTPUT ${1:name} TO ${2:target} FROM ${3:input}',
            RATE: 'RATE ${1:name} FROM ${2:input} MODE=${3:rising}',
            DEADBAND: 'DEADBAND ${1:name} FROM ${2:input} RANGE ${3:0} ${4:5}',
            SCALE: 'SCALE ${1:name} FROM ${2:input} RANGE ${3:0} ${4:100}',
        };

        // FB parameter hints for signature help
        const fbSignatures = {
            SENSOR: 'SENSOR name FROM source [SCALE k b] [RANGE lo hi]',
            FILTER: 'FILTER name FROM signal LOWPASS a=alpha',
            PID: 'PID name FROM signal SP=setpoint KP=kp KI=ki KD=kd LIMIT lo hi',
            TIMER: 'TIMER name PT=time [mode=TON|TOF|TP]',
            COUNTER: 'COUNTER name CU=signal PV=preset',
            LATCH: 'LATCH name S1=set R=reset',
            ALARM: 'ALARM name FROM signal > threshold',
            LOGIC: 'LOGIC name = signal1 AND|OR|NOT signal2',
            OUTPUT: 'OUTPUT name TO target FROM signal',
            RATE: 'RATE name FROM signal',
            DEADBAND: 'DEADBAND name FROM signal width',
            SCALE: 'SCALE name FROM signal RANGE lo hi',
        };

        monaco.languages.registerCompletionItemProvider('dcl', {
            triggerCharacters: [' ', '='],
            provideCompletionItems: (model, position) => {
                const word = model.getWordUntilPosition(position);
                const range = {
                    startLineNumber: position.lineNumber,
                    endLineNumber: position.lineNumber,
                    startColumn: word.startColumn,
                    endColumn: word.endColumn
                };

                // Get text before cursor on the current line
                const lineContent = model.getLineContent(position.lineNumber);
                const prefix = lineContent.substring(0, position.column - 1).trim();

                const suggestions = [];

                // After FROM → suggest signal names from current document
                if (/\bFROM\s*$/.test(prefix)) {
                    const signalNames = this._extractSignalNames();
                    for (const name of signalNames) {
                        suggestions.push({
                            label: name,
                            kind: monaco.languages.CompletionItemKind.Variable,
                            insertText: name,
                            detail: 'Signal',
                            range: range
                        });
                    }
                }

                // At line start or after FB keyword name → show FB snippets
                const fbKeys = Object.keys(fbSnippets);
                const startsWithFb = fbKeys.some(k => prefix.startsWith(k));
                if (!startsWithFb) {
                    for (const key of fbKeys) {
                        suggestions.push({
                            label: key,
                            kind: monaco.languages.CompletionItemKind.Snippet,
                            insertText: fbSnippets[key],
                            insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
                            documentation: fbSignatures[key],
                            detail: fbSignatures[key],
                            range: range
                        });
                    }
                }

                // After FB keyword + name → suggest parameter keywords
                const fbMatch = prefix.match(/^(SENSOR|FILTER|PID|TIMER|COUNTER|LATCH|ALARM|LOGIC|OUTPUT|RATE|DEADBAND|SCALE)\s+\w+\s*/);
                if (fbMatch) {
                    const fbKey = fbMatch[1];
                    // Context-dependent parameter suggestions
                    const paramMap = {
                        SENSOR: ['FROM', 'SCALE', 'RANGE'],
                        FILTER: ['FROM', 'LOWPASS'],
                        PID: ['FROM', 'SP', 'KP', 'KI', 'KD', 'LIMIT'],
                        TIMER: ['PT', 'MODE', 'IN'],
                        COUNTER: ['CU', 'CD', 'PV', 'R'],
                        LATCH: ['S1', 'R'],
                        ALARM: ['FROM'],
                        LOGIC: ['AND', 'OR', 'NOT'],
                        OUTPUT: ['TO', 'FROM'],
                        RATE: ['FROM'],
                        DEADBAND: ['FROM'],
                        SCALE: ['FROM', 'RANGE'],
                    };
                    const params = paramMap[fbKey] || [];
                    for (const p of params) {
                        suggestions.push({
                            label: p,
                            kind: monaco.languages.CompletionItemKind.Property,
                            insertText: p === 'LIMIT' || p === 'RANGE' || p === 'SCALE' ? p + ' ' : p + '=',
                            documentation: fbSignatures[fbKey],
                            detail: `${fbKey} parameter`,
                            range: range
                        });
                    }
                }

                // General keyword suggestions (when not in a specific context)
                if (!startsWithFb && !fbMatch && !/\bFROM\s*$/.test(prefix)) {
                    ['FROM', 'TO', 'LOWPASS', 'LIMIT', 'RANGE', 'OR', 'AND', 'NOT',
                     'IN', 'PT', 'PV', 'SP', 'KP', 'KI', 'KD', 'CU', 'CD',
                     'R', 'S1', 'R1', 'Q', 'Q1', 'CV', 'QU', 'QD', 'MODE'].forEach(kw => {
                        suggestions.push({
                            label: kw,
                            kind: monaco.languages.CompletionItemKind.Keyword,
                            insertText: kw,
                            range: range
                        });
                    });
                }

                return { suggestions };
            }
        });
    },

    getValue() {
        return this.editor ? this.editor.getValue() : '';
    },

    setValue(text) {
        if (this.editor) {
            this.editor.setValue(text);
        }
    },

    setMarkers(errors) {
        if (!this.editor) return;
        const model = this.editor.getModel();
        if (!model) return;

        const markers = [];
        for (const err of errors) {
            let line = 1;
            // Try Chinese pattern: 第5: or 第5行
            const cnMatch = String(err).match(/第(\d+)/);
            // Try English pattern: line 5
            const enMatch = String(err).match(/line\s+(\d+)/i);
            if (cnMatch) line = parseInt(cnMatch[1]);
            else if (enMatch) line = parseInt(enMatch[1]);

            markers.push({
                severity: monaco.MarkerSeverity.Error,
                message: String(err),
                startLineNumber: line,
                startColumn: 1,
                endLineNumber: line,
                endColumn: 1000,
            });
        }
        monaco.editor.setModelMarkers(model, 'dcl', markers);
    },

    clearMarkers() {
        if (!this.editor) return;
        monaco.editor.setModelMarkers(this.editor.getModel(), 'dcl', []);
    },

    _changeTimeout: null,

    onDidChangeContent(callback) {
        if (this.editor) {
            this.editor.onDidChangeModelContent(() => {
                this.clearMarkers();

                if (this._changeTimeout) clearTimeout(this._changeTimeout);
                this._changeTimeout = setTimeout(() => {
                    const src = this.getValue();
                    window.dispatchEvent(new CustomEvent('dcl-check', { detail: src }));
                    if (window.DclUsb) DclUsb.send({ cmd: 'set_source', source: src });
                }, 500);

                if (callback) callback();
            });
        }
    },

    onSourceChanged(source) {
        if (!this.editor) return;
        if (this.editor.getValue() !== source) {
            this.editor.setValue(source);
        }
    },

    _extractSignalNames() {
        const names = [];
        const fbPattern = /^(?:SENSOR|FILTER|PID|TIMER|COUNTER|LATCH|ALARM|LOGIC|OUTPUT|RATE|DEADBAND|SCALE)\s+(\w+)/;
        for (const line of this.getValue().split('\n')) {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith('#') || trimmed.startsWith('//')) continue;
            const m = trimmed.match(fbPattern);
            if (m) names.push(m[1]);
        }
        return names;
    }
};