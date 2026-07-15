Chart.defaults.color = '#94A3B8';
Chart.defaults.font.family = 'Inter, sans-serif';
Chart.defaults.plugins.tooltip.backgroundColor = 'rgba(26, 26, 36, 0.9)';
Chart.defaults.plugins.tooltip.titleColor = '#F8FAFC';
Chart.defaults.plugins.tooltip.bodyColor = '#F8FAFC';
Chart.defaults.plugins.tooltip.borderColor = 'rgba(255,255,255,0.1)';
Chart.defaults.plugins.tooltip.borderWidth = 1;
Chart.defaults.plugins.tooltip.padding = 10;
Chart.defaults.plugins.tooltip.cornerRadius = 8;

const chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 600, easing: 'easeOutQuart' },
    plugins: {
        legend: { display: false },
        zoom: {
            zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' },
            pan: { enabled: true, mode: 'x' }
        }
    },
    scales: {
        x: { grid: { color: 'rgba(255,255,255,0.05)' } },
        y: { grid: { color: 'rgba(255,255,255,0.05)' }, beginAtZero: true }
    }
};

class Dashboard {
    constructor() {
        this.charts = {};
        this.initCharts();
    }

    initCharts() {
        // 1. Memory Timeline
        const ctxMem = document.getElementById('chart-memory').getContext('2d');
        this.charts.memory = new Chart(ctxMem, {
            type: 'line',
            data: { datasets: [{ label: 'Memory (MB)', data: [], borderColor: '#7C3AED', backgroundColor: 'rgba(124, 58, 237, 0.1)', fill: true, tension: 0.4 }] },
            options: { ...chartOptions, scales: { ...chartOptions.scales, x: { type: 'linear', title: { display: true, text: 'Time (ms)' } } } }
        });

        // 2. GC Events
        const ctxGc = document.getElementById('chart-gc').getContext('2d');
        this.charts.gc = new Chart(ctxGc, {
            type: 'scatter',
            data: { datasets: [{ label: 'GC Pause (ms)', data: [], backgroundColor: '#EF4444', pointRadius: 5 }] },
            options: { ...chartOptions, scales: { ...chartOptions.scales, x: { type: 'linear', title: { display: true, text: 'Time (ms)' } }, y: { title: { display: true, text: 'Pause Duration (ms)' } } } }
        });

        // 3. Heap Usage (synthetic or real if java)
        const ctxHeap = document.getElementById('chart-heap').getContext('2d');
        this.charts.heap = new Chart(ctxHeap, {
            type: 'line',
            data: { datasets: [{ label: 'Heap Used (MB)', data: [], borderColor: '#F59E0B', backgroundColor: 'rgba(245, 158, 11, 0.1)', fill: true, tension: 0.1 }] },
            options: { ...chartOptions, scales: { ...chartOptions.scales, x: { type: 'linear', title: { display: true, text: 'Time (ms)' } } } }
        });

        // 4. Syscalls (Doughnut)
        const ctxSyscalls = document.getElementById('chart-syscalls').getContext('2d');
        this.charts.syscalls = new Chart(ctxSyscalls, {
            type: 'doughnut',
            data: { labels: [], datasets: [{ data: [], backgroundColor: ['#7C3AED', '#06B6D4', '#F59E0B', '#EF4444', '#10B981', '#3B82F6'], borderWidth: 0 }] },
            options: { ...chartOptions, scales: {}, plugins: { legend: { display: true, position: 'right' } }, cutout: '70%' }
        });

        // 5. CPU Time Split
        const ctxCpu = document.getElementById('chart-cpu').getContext('2d');
        this.charts.cpu = new Chart(ctxCpu, {
            type: 'bar',
            data: {
                labels: ['CPU Time'],
                datasets: [
                    { label: 'User %', data: [0], backgroundColor: '#7C3AED' },
                    { label: 'Kernel %', data: [0], backgroundColor: '#06B6D4' }
                ]
            },
            options: { ...chartOptions, indexAxis: 'y', scales: { x: { stacked: true, max: 100 }, y: { stacked: true } }, plugins: { legend: { display: true } } }
        });

        // 6. Allocation Rate (Mocked for now from allocations/runtime)
        const ctxAlloc = document.getElementById('chart-alloc').getContext('2d');
        this.charts.alloc = new Chart(ctxAlloc, {
            type: 'bar',
            data: { labels: [], datasets: [{ label: 'Allocations / bucket', data: [], backgroundColor: '#10B981' }] },
            options: { ...chartOptions }
        });
    }

    clear() {
        Object.values(this.charts).forEach(chart => {
            chart.data.labels = [];
            chart.data.datasets.forEach(ds => ds.data = []);
            chart.update();
        });
        document.getElementById('bottlenecks-panel').classList.add('hidden');
        document.getElementById('bottlenecks-list').innerHTML = '';
    }

    render(metrics) {
        if (!metrics) return;

        // Memory Timeline
        if (metrics.memory && metrics.memory.timeline) {
            this.charts.memory.data.datasets[0].data = metrics.memory.timeline.map(p => ({ x: p.t, y: p.bytes / 1024 / 1024 }));
            this.charts.memory.update();
            
            // Build pseudo heap chart from memory timeline
            this.charts.heap.data.datasets[0].data = metrics.memory.timeline.map(p => ({ x: p.t, y: (p.bytes / 1024 / 1024) * 0.8 }));
            this.charts.heap.update();
        }

        // GC Events
        if (metrics.gc && metrics.gc.events) {
            this.charts.gc.data.datasets[0].data = metrics.gc.events.map(e => ({ x: e.t, y: e.pause_ms }));
            this.charts.gc.update();
        }

        // Syscalls
        if (metrics.syscalls && metrics.syscalls.by_type) {
            const types = Object.keys(metrics.syscalls.by_type).sort((a,b) => metrics.syscalls.by_type[b] - metrics.syscalls.by_type[a]).slice(0, 6);
            this.charts.syscalls.data.labels = types;
            this.charts.syscalls.data.datasets[0].data = types.map(t => metrics.syscalls.by_type[t]);
            this.charts.syscalls.update();
        }

        // CPU
        if (metrics.syscalls) {
            this.charts.cpu.data.datasets[0].data = [metrics.syscalls.user_time_pct];
            this.charts.cpu.data.datasets[1].data = [metrics.syscalls.kernel_time_pct];
            this.charts.cpu.update();
        }

        // Allocations (Generate synthetic buckets based on total allocations)
        if (metrics.memory && metrics.memory.allocations > 0) {
            const buckets = 10;
            const labels = Array.from({length: buckets}, (_, i) => `${i*10}%`);
            const avg = metrics.memory.allocations / buckets;
            const data = Array.from({length: buckets}, () => avg * (0.8 + Math.random() * 0.4));
            this.charts.alloc.data.labels = labels;
            this.charts.alloc.data.datasets[0].data = data;
            this.charts.alloc.update();
        }

        this.highlightBottlenecks(metrics.bottlenecks);
    }

    highlightBottlenecks(bottlenecks) {
        const panel = document.getElementById('bottlenecks-panel');
        const list = document.getElementById('bottlenecks-list');
        list.innerHTML = '';

        if (!bottlenecks || bottlenecks.length === 0) {
            panel.classList.add('hidden');
            return;
        }

        bottlenecks.forEach(b => {
            const li = document.createElement('li');
            li.className = `bottleneck-${b.severity}`;
            li.textContent = `[${b.type.toUpperCase()}] ${b.message}`;
            list.appendChild(li);
        });

        panel.classList.remove('hidden');
    }
}

window.Dashboard = Dashboard;
