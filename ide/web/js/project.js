window.DclProject = {
    files: [{ name: 'main.dcl', content: null }],
    activeFile: 'main.dcl',

    init() {
        this.renderTree();
    },

    renderTree() {
        const tree = document.getElementById('project-tree');
        if (!tree) return;

        let html = '';
        for (const file of this.files) {
            const isActive = file.name === this.activeFile;
            html += `<div class="tree-item${isActive ? ' active' : ''}" onclick="DclProject.openFile('${file.name}')">
                <span class="file-icon">*</span>${file.name}
            </div>`;
        }
        tree.innerHTML = html;
    },

    openFile(name) {
        const file = this.files.find(f => f.name === name);
        if (!file) return;

        this.activeFile = name;

        if (file.content !== null) {
            DclEditor.setValue(file.content);
        }

        document.querySelectorAll('.tree-item').forEach(el => {
            el.classList.toggle('active', el.textContent.trim().endsWith(name));
        });

        document.querySelectorAll('.tab').forEach(el => {
            el.classList.toggle('active', el.dataset.file === name);
        });
    },

    saveCurrentFile() {
        const file = this.files.find(f => f.name === this.activeFile);
        if (file) {
            file.content = DclEditor.getValue();
        }
    }
};