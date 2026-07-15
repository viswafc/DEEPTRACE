class App {
    constructor() {
        this.api = window.api;
        this.editor = new CodeEditor('code-editor', 'code-highlight');
        this.dashboard = new Dashboard();
        this.comparison = new ComparisonMode(this.dashboard);
        
        this.currentJobId = null;
        this.jobHistory = JSON.parse(localStorage.getItem('deeptrace_history') || '[]');
        
        this.bindEvents();
        this.checkHealth();
        this.updateHistoryDropdown();
    }

    bindEvents() {
        document.getElementById('language-select').addEventListener('change', (e) => {
            this.editor.setLanguage(e.target.value);
        });

        document.getElementById('btn-sample-inefficient').addEventListener('click', () => {
            this.editor.loadSample('inefficient');
        });

        document.getElementById('btn-sample-optimized').addEventListener('click', () => {
            this.editor.loadSample('optimized');
        });

        document.getElementById('btn-submit').addEventListener('click', () => {
            this.submitCode(false);
        });

        document.getElementById('btn-compare').addEventListener('click', () => {
            this.submitCode(true);
        });

        document.getElementById('btn-export').addEventListener('click', () => {
            if (this.currentJobId) {
                this.api.exportMetrics(this.currentJobId, 'json');
            }
        });

        document.getElementById('job-history').addEventListener('change', async (e) => {
            if (e.target.value) {
                await this.loadJob(e.target.value);
            }
        });
    }

    async checkHealth() {
        try {
            const health = await this.api.request('/health');
            const ind = document.getElementById('connection-status');
            ind.className = 'status-indicator connected';
            ind.querySelector('.text').textContent = 'Connected';
        } catch (e) {
            const ind = document.getElementById('connection-status');
            ind.className = 'status-indicator disconnected';
            ind.querySelector('.text').textContent = 'Disconnected';
            setTimeout(() => this.checkHealth(), 5000);
        }
    }

    showToast(message, type = 'success') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;
        container.appendChild(toast);
        
        setTimeout(() => {
            toast.style.animation = 'slideIn 0.3s ease reverse';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }

    updateStatus(jobId, status, runtime = 0) {
        document.getElementById('status-job-id').textContent = jobId || '-';
        document.getElementById('status-runtime').textContent = `${runtime}ms`;
        
        const badge = document.getElementById('status-badge');
        badge.className = `badge badge-${status.toLowerCase()}`;
        badge.textContent = status;

        if (status === 'RUNNING') {
            document.getElementById('loading-overlay').classList.remove('hidden');
        } else {
            document.getElementById('loading-overlay').classList.add('hidden');
        }
    }

    addToHistory(jobId) {
        if (!this.jobHistory.includes(jobId)) {
            this.jobHistory.unshift(jobId);
            if (this.jobHistory.length > 5) this.jobHistory.pop();
            localStorage.setItem('deeptrace_history', JSON.stringify(this.jobHistory));
            this.updateHistoryDropdown();
        }
    }

    updateHistoryDropdown() {
        const select = document.getElementById('job-history');
        select.innerHTML = '<option value="">Recent Jobs...</option>';
        this.jobHistory.forEach(id => {
            const opt = document.createElement('option');
            opt.value = id;
            opt.textContent = id.substring(0, 8) + '...';
            select.appendChild(opt);
        });
    }

    async submitCode(isCompare = false) {
        const code = this.editor.getCode();
        const lang = this.editor.language;

        if (!code.trim()) {
            this.showToast('Code cannot be empty', 'error');
            return;
        }

        try {
            document.getElementById('btn-submit').disabled = true;
            document.getElementById('btn-compare').disabled = true;
            this.dashboard.clear();
            
            const res = await this.api.submit(code, lang);
            const jobId = res.job_id;
            this.currentJobId = jobId;
            this.addToHistory(jobId);
            
            this.updateStatus(jobId, 'RUNNING');

            try {
                // Try WebSocket first
                await new Promise((resolve, reject) => {
                    this.api.connectWebSocket(jobId, (msg) => {
                        if (msg.type === 'progress') {
                            document.getElementById('progress-bar').style.width = `${msg.data.progress}%`;
                        } else if (msg.type === 'done') {
                            this.handleJobComplete(msg.data, isCompare);
                            resolve();
                        } else if (msg.type === 'error') {
                            reject(new Error(msg.data.error));
                        }
                    }, () => {
                        // closed
                    }).catch(err => {
                        console.log("WebSocket failed, falling back to polling", err);
                        // Fallback to polling
                        this.api.pollUntilDone(jobId, (status) => {
                            if (status.progress) {
                                document.getElementById('progress-bar').style.width = `${status.progress}%`;
                            }
                        }).then(metrics => {
                            this.handleJobComplete(metrics, isCompare);
                            resolve();
                        }).catch(reject);
                    });
                });
            } catch (err) {
                this.updateStatus(jobId, 'ERROR');
                this.showToast(`Execution failed: ${err.message}`, 'error');
            }
            
        } catch (err) {
            this.showToast(`Submission failed: ${err.message}`, 'error');
        } finally {
            document.getElementById('btn-submit').disabled = false;
        }
    }

    async loadJob(jobId) {
        try {
            this.dashboard.clear();
            this.updateStatus(jobId, 'RUNNING');
            const metrics = await this.api.getResults(jobId);
            this.currentJobId = jobId;
            this.handleJobComplete(metrics, false);
        } catch (err) {
            this.showToast(`Failed to load job: ${err.message}`, 'error');
            this.updateStatus(jobId, 'ERROR');
        }
    }

    handleJobComplete(metrics, isCompare) {
        this.updateStatus(metrics.job_id, 'DONE', metrics.runtime_ms);
        document.getElementById('btn-export').disabled = false;
        
        if (isCompare) {
            this.comparison.renderComparison(metrics);
        } else {
            this.dashboard.render(metrics);
            this.comparison.setBaseline(metrics);
            document.getElementById('btn-compare').disabled = false;
        }
    }
}

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    window.app = new App();
});
