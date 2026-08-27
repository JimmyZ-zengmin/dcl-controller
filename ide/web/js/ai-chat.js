window.DclAI = {
    messages: [],

    init() {
        const input = document.getElementById('chat-input');
        const sendBtn = document.getElementById('chat-send');
        if (input) {
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.send();
                }
            });
        }
        if (sendBtn) {
            sendBtn.addEventListener('click', () => this.send());
        }
    },

    send() {
        const input = document.getElementById('chat-input');
        if (!input) return;
        const text = input.value.trim();
        if (!text) return;

        // Add user message
        this.addMessage('user', text);
        input.value = '';

        // Send to backend
        DclUsb.send({ cmd: 'ai_chat', message: text, source: DclEditor.getValue() });
    },

    addMessage(role, text) {
        this.messages.push({ role, text, time: new Date() });
        this.renderMessages();
    },

    renderMessages() {
        const container = document.getElementById('chat-messages');
        if (!container) return;

        let html = '';
        for (const msg of this.messages) {
            const cls = msg.role === 'user' ? 'msg user' : 'msg assistant';
            const escaped = this._escapeHtml(msg.text);
            // Simple markdown: code blocks
            const formatted = escaped.replace(/```([\s\S]*?)```/g, '<pre><code>$1</code></pre>')
                                     .replace(/`([^`]+)`/g, '<code>$1</code>');
            html += '<div class="' + cls + '">' + formatted + '</div>';
        }
        container.innerHTML = html;
        container.scrollTop = container.scrollHeight;
    },

    handleResponse(msg) {
        if (msg.success) {
            this.addMessage('assistant', msg.response);
        } else {
            this.addMessage('error', msg.response || 'AI error');
        }
    },

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
};
