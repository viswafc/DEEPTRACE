class ApiClient {
    constructor(baseUrl = 'http://localhost:8000/api', wsUrl = 'ws://localhost:8000/ws') {
        this.baseUrl = baseUrl;
        this.wsUrl = wsUrl;
        this.ws = null;
    }

    async request(endpoint, options = {}) {
        try {
            const response = await fetch(`${this.baseUrl}${endpoint}`, {
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers
                }
            });
            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.detail || `HTTP Error ${response.status}`);
            }
            return await response.json();
        } catch (error) {
            console.error(`API Error on ${endpoint}:`, error);
            throw error;
        }
    }

    async submit(code, language) {
        return this.request('/submit', {
            method: 'POST',
            body: JSON.stringify({ code, language })
        });
    }

    async getStatus(jobId) {
        return this.request(`/status/${jobId}`);
    }

    async getResults(jobId) {
        return this.request(`/results/${jobId}`);
    }

    async compare(jobIdA, jobIdB) {
        return this.request('/compare', {
            method: 'POST',
            body: JSON.stringify({ job_id_a: jobIdA, job_id_b: jobIdB })
        });
    }

    exportMetrics(jobId, format = 'json') {
        window.location.href = `${this.baseUrl}/export/${jobId}?format=${format}`;
    }

    connectWebSocket(jobId, onMessage, onClose) {
        if (this.ws) {
            this.ws.close();
        }
        
        return new Promise((resolve, reject) => {
            try {
                this.ws = new WebSocket(`${this.wsUrl}/${jobId}`);
                
                this.ws.onopen = () => {
                    console.log('WebSocket connected for job', jobId);
                    resolve(this.ws);
                };
                
                this.ws.onmessage = (event) => {
                    try {
                        const data = JSON.parse(event.data);
                        onMessage(data);
                    } catch (e) {
                        console.error('WebSocket message parse error:', e);
                    }
                };
                
                this.ws.onerror = (err) => {
                    console.error('WebSocket error:', err);
                    reject(err);
                };
                
                this.ws.onclose = () => {
                    console.log('WebSocket closed');
                    if (onClose) onClose();
                };
            } catch (err) {
                reject(err);
            }
        });
    }

    async pollUntilDone(jobId, onUpdate, pollInterval = 2000) {
        while (true) {
            const status = await this.getStatus(jobId);
            onUpdate(status);
            if (status.status === 'DONE' || status.status === 'ERROR') {
                if (status.status === 'DONE') {
                    return this.getResults(jobId);
                }
                throw new Error(status.error || 'Job failed');
            }
            await new Promise(resolve => setTimeout(resolve, pollInterval));
        }
    }
}

window.api = new ApiClient();
