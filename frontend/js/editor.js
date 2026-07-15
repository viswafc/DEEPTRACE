class CodeEditor {
    constructor(editorId, highlightId) {
        this.editor = document.getElementById(editorId);
        this.highlight = document.getElementById(highlightId);
        this.language = 'python';
        
        this.setupEvents();
        this.updateStats();
    }

    setupEvents() {
        this.editor.addEventListener('input', () => {
            this.syncCode();
            this.updateStats();
        });

        this.editor.addEventListener('scroll', () => {
            this.highlight.parentElement.scrollTop = this.editor.scrollTop;
            this.highlight.parentElement.scrollLeft = this.editor.scrollLeft;
        });

        this.editor.addEventListener('keydown', (e) => {
            if (e.key === 'Tab') {
                e.preventDefault();
                const start = this.editor.selectionStart;
                const end = this.editor.selectionEnd;
                
                this.editor.value = this.editor.value.substring(0, start) + '    ' + this.editor.value.substring(end);
                this.editor.selectionStart = this.editor.selectionEnd = start + 4;
                
                this.syncCode();
            }
        });
    }

    syncCode() {
        let code = this.editor.value;
        if (code.endsWith('\n')) {
            code += ' ';
        }
        
        const lang = Prism.languages[this.language] || Prism.languages.clike;
        const html = Prism.highlight(code, lang, this.language);
        this.highlight.innerHTML = html;
    }

    setLanguage(lang) {
        this.language = lang;
        this.highlight.className = `language-${lang}`;
        this.syncCode();
    }

    getCode() {
        return this.editor.value;
    }

    setCode(code) {
        this.editor.value = code;
        this.syncCode();
        this.updateStats();
    }

    updateStats() {
        const text = this.editor.value;
        const chars = text.length;
        const lines = text ? text.split('\n').length : 0;
        
        document.getElementById('char-count').textContent = `${chars} chars`;
        document.getElementById('line-count').textContent = `${lines} lines`;
    }

    loadSample(type) {
        const samples = {
            python: {
                inefficient: `# Inefficient Python Sample\n# O(n^2) bubble sort and excessive memory allocation\n\nN = 2000\ndata = list(range(N, 0, -1))\n\n# Bubble sort\nfor i in range(len(data)):\n    for j in range(len(data) - i - 1):\n        if data[j] > data[j+1]:\n            data[j], data[j+1] = data[j+1], data[j]\n\n# String concatenation in loop\nresult = ''\nfor x in data[:100]:\n    result = result + str(x) + ','\n\n# Memory pressure\nfor _ in range(1000):\n    temp = [x * 2 for x in data]\n    del temp\n\nprint(f"Done. Length: {len(result)}")`,
                optimized: `# Optimized Python Sample\n# Built-in sort and generator expressions\n\nN = 2000\ndata = list(range(N, 0, -1))\n\n# Built-in sort (Timsort)\ndata.sort()\n\n# String join\nresult = ','.join(str(x) for x in data[:100])\n\n# Generator expression instead of list\ntotal = sum(x * 2 for x in data)\n\nprint(f"Done. Length: {len(result)}")`
            },
            java: {
                inefficient: `// Inefficient Java Sample\n\npublic class Submission {\n    public static void main(String[] args) {\n        int N = 2000;\n        int[] data = new int[N];\n        for (int i = 0; i < N; i++) data[i] = N - i;\n\n        // Bubble sort\n        for (int i = 0; i < N; i++) {\n            for (int j = 0; j < N - i - 1; j++) {\n                if (data[j] > data[j+1]) {\n                    int t = data[j]; data[j] = data[j+1]; data[j+1] = t;\n                }\n            }\n        }\n\n        // String concatenation in loop\n        String result = "";\n        for (int i = 0; i < 100; i++) {\n            result += data[i] + ",";\n        }\n\n        // Memory pressure\n        for (int i = 0; i < 10000; i++) {\n            int[] temp = new int[100];\n            for (int j=0; j<100; j++) temp[j] = j*i;\n        }\n\n        System.out.println("Done");\n    }\n}`,
                optimized: `// Optimized Java Sample\nimport java.util.Arrays;\n\npublic class Submission {\n    public static void main(String[] args) {\n        int N = 2000;\n        int[] data = new int[N];\n        for (int i = 0; i < N; i++) data[i] = N - i;\n\n        // Built-in sort\n        Arrays.sort(data);\n\n        // StringBuilder\n        StringBuilder sb = new StringBuilder(N * 5);\n        for (int i = 0; i < 100; i++) {\n            sb.append(data[i]).append(',');\n        }\n\n        // Reuse array\n        int[] reusable = new int[100];\n        for (int i = 0; i < 10000; i++) {\n            for (int j=0; j<100; j++) reusable[j] = j*i;\n        }\n\n        System.out.println("Done");\n    }\n}`
            }
        };

        const sampleCode = samples[this.language][type];
        if (sampleCode) {
            this.setCode(sampleCode);
        }
    }
}

window.CodeEditor = CodeEditor;
