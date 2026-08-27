"""
DCL Language Server — pygls implementation

Provides:
1. Real-time error checking via compiler_wrapper.check()
2. Auto-completion for FB keywords + signal names
3. Hover documentation for FBs
4. Go-to-definition for signal names
"""
import json
import logging
import asyncio
from typing import Optional

from pygls.server import LanguageServer
from lsprotocol import types as lsp

from server.compiler_wrapper import CompilerWrapper

logger = logging.getLogger('dcl-ide.lsp')

# DCL FB documentation for hover
DCL_FB_DOCS = {
    'SENSOR': 'SENSOR name FROM source [SCALE k b] [RANGE lo hi]\nRead hardware input (ADC/GPIO)',
    'FILTER': 'FILTER name FROM signal LOWPASS a=alpha\nFirst-order low-pass filter: y += a*(x-y)',
    'PID': 'PID name FROM signal SP=setpoint KP=kp KI=ki KD=kd LIMIT lo hi\nPID controller with output clamping',
    'TIMER': 'TIMER name: IN=signal, PT=time [mode=TON|TOF|TP] → Q=output [, ET=elapsed]\nIEC 61131-3 timer (TON/TOF/TP)',
    'COUNTER': 'COUNTER name: CU=signal, PV=preset → Q=output, CV=count\nIEC 61131-3 counter (CTU/CTD/CTUD)',
    'LATCH': 'LATCH name: S1=set, R=reset → Q1=output\nSR latch (set-dominant)',
    'ALARM': 'ALARM name FROM signal > threshold\nComparator alarm (>/<, >=/<=)',
    'LOGIC': 'LOGIC name = signal1 AND|OR|NOT signal2\nBoolean logic operation',
    'OUTPUT': 'OUTPUT name TO target FROM signal\nWrite to hardware output (PWM/GPIO)',
    'RATE': 'RATE name FROM signal\nRate of change (derivative)',
    'DEADBAND': 'DEADBAND name FROM signal, width\nDeadband filter',
    'SCALE': 'SCALE name FROM signal RANGE lo hi\nLinear scaling y=kx+b',
}

FB_KEYWORDS = ['SENSOR', 'FILTER', 'PID', 'TIMER', 'COUNTER', 'LATCH', 'ALARM', 'LOGIC', 'OUTPUT', 'RATE', 'DEADBAND', 'SCALE']

class DclLanguageServer:
    def __init__(self):
        self.server = LanguageServer('dcl-lsp', 'v1.0')
        self.compiler = CompilerWrapper()
        self._source = ''
        self._signal_names = []
        self._register_features()

    def _register_features(self):
        @self.server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
        def did_open(params: lsp.DidOpenTextDocumentParams):
            self._source = params.text_document.text
            self._update_diagnostics(params.text_document.uri)

        @self.server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
        def did_change(params: lsp.DidChangeTextDocumentParams):
            # Get latest content from changes
            for change in params.content_changes:
                self._source = change.text
            self._update_diagnostics(params.text_document.uri)

        @self.server.feature(
            lsp.TEXT_DOCUMENT_COMPLETION,
            lsp.CompletionOptions(trigger_characters=[' ', '=']),
        )
        def completions(params: lsp.CompletionParams):
            uri = params.text_document.uri
            line = params.position.line
            char = params.position.character

            # Get current line text
            lines = self._source.split('\n')
            if line >= len(lines):
                return lsp.CompletionList(is_incomplete=False, items=[])
            current_line = lines[line]
            prefix = current_line[:char].strip()

            items = []

            # At line start or after whitespace only → FB keywords
            if not prefix or prefix.split()[0] not in FB_KEYWORDS:
                if not any(prefix.startswith(kw) for kw in FB_KEYWORDS):
                    for kw in FB_KEYWORDS:
                        items.append(lsp.CompletionItem(
                            label=kw,
                            kind=lsp.CompletionItemKind.Keyword,
                            detail='DCL Function Block',
                        ))

            # After FROM → signal names
            if 'FROM' in prefix:
                for name in self._signal_names:
                    items.append(lsp.CompletionItem(
                        label=name,
                        kind=lsp.CompletionItemKind.Variable,
                        detail='Signal',
                    ))

            return lsp.CompletionList(is_incomplete=False, items=items)

        @self.server.feature(lsp.TEXT_DOCUMENT_HOVER)
        def hover(params: lsp.HoverParams):
            lines = self._source.split('\n')
            line_idx = params.position.line
            if line_idx >= len(lines):
                return None
            line = lines[line_idx]
            word_start = word_end = params.position.character
            while word_start > 0 and line[word_start-1].isalnum():
                word_start -= 1
            while word_end < len(line) and line[word_end].isalnum():
                word_end += 1
            word = line[word_start:word_end].upper()

            if word in DCL_FB_DOCS:
                return lsp.Hover(
                    contents=lsp.MarkupContent(
                        kind=lsp.MarkupKind.PlainText,
                        value=DCL_FB_DOCS[word],
                    )
                )
            return None

    def _update_diagnostics(self, uri: str):
        """Run compiler check and publish diagnostics"""
        diagnostics = self.compiler.check(self._source)
        self._extract_signal_names()

        lsp_diagnostics = []
        for d in diagnostics:
            lsp_diagnostics.append(lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(line=max(0, d.line - 1), character=0),
                    end=lsp.Position(line=max(0, d.line - 1), character=1000),
                ),
                message=d.message,
                severity=lsp.DiagnosticSeverity.Error,
                source='dcl-compiler',
            ))

        self.server.publish_diagnostics(uri, lsp_diagnostics)
        logger.info(f"Published {len(lsp_diagnostics)} diagnostics for {uri}")

    def _extract_signal_names(self):
        """Extract signal names from source for completion"""
        import re
        self._signal_names = []
        for line in self._source.split('\n'):
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('//'):
                continue
            # Match signal name patterns
            m = re.match(r'(?:SENSOR|FILTER|PID|TIMER|COUNTER|LATCH|ALARM|LOGIC|OUTPUT|RATE|DEADBAND|SCALE)\s+(\w+)', line)
            if m:
                self._signal_names.append(m.group(1))

    def start_ws(self, host='localhost', port=8767):
        """Start LSP server over WebSocket

        NOTE: pygls v2.x may not support start_ws() directly.
        If AttributeError occurs, use start_io() and add a WebSocket bridge.
        """
        try:
            logger.info(f"LSP server starting on ws://{host}:{port}")
            self.server.start_ws(host, port)
        except AttributeError:
            logger.warning("start_ws() not available in this pygls version, falling back to start_io()")
            logger.warning("A WebSocket bridge is needed for Monaco editor integration.")
            self.server.start_io()

    def start_io(self):
        """Start LSP server over stdio"""
        self.server.start_io()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    server = DclLanguageServer()
    # Default to stdio; use --ws flag for WebSocket mode
    import sys
    if '--ws' in sys.argv:
        port = 8766
        for i, arg in enumerate(sys.argv):
            if arg == '--port' and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
        server.start_ws(port=port)
    else:
        server.start_io()
