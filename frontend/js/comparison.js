class ComparisonMode {
    constructor(dashboard) {
        this.dashboard = dashboard;
        this.baselineMetrics = null;
        this.modal = document.getElementById('compare-modal');
        this.closeBtn = document.getElementById('btn-close-modal');
        
        this.closeBtn.addEventListener('click', () => this.hideModal());
    }

    setBaseline(metrics) {
        this.baselineMetrics = metrics;
    }

    hasBaseline() {
        return this.baselineMetrics !== null;
    }

    renderComparison(metricsB) {
        if (!this.baselineMetrics) return;

        const metricsA = this.baselineMetrics;
        const tbody = document.getElementById('compare-table-body');
        tbody.innerHTML = '';

        // Helper to format values
        const fmt = (v) => typeof v === 'number' ? (v % 1 === 0 ? v : v.toFixed(2)) : v;

        // Calculate and add rows
        const rows = [
            { name: 'Runtime (ms)', a: metricsA.runtime_ms, b: metricsB.runtime_ms, lowerIsBetter: true },
            { name: 'Peak Memory (MB)', a: metricsA.memory.peak_mb, b: metricsB.memory.peak_mb, lowerIsBetter: true },
            { name: 'Total Allocations', a: metricsA.memory.allocations, b: metricsB.memory.allocations, lowerIsBetter: true },
            { name: 'GC Total Pause (ms)', a: metricsA.gc.total_pause_ms, b: metricsB.gc.total_pause_ms, lowerIsBetter: true },
            { name: 'Syscalls Total', a: metricsA.syscalls.total, b: metricsB.syscalls.total, lowerIsBetter: true },
            { name: 'User Time %', a: metricsA.syscalls.user_time_pct, b: metricsB.syscalls.user_time_pct, lowerIsBetter: false }
        ];

        rows.forEach(row => {
            const tr = document.createElement('tr');
            
            let deltaText = 'N/A';
            let deltaClass = '';
            
            if (row.a > 0) {
                const deltaPct = ((row.b - row.a) / row.a) * 100;
                const sign = deltaPct > 0 ? '+' : '';
                deltaText = `${sign}${deltaPct.toFixed(1)}%`;
                
                if (Math.abs(deltaPct) > 1) { // 1% threshold
                    if ((deltaPct < 0 && row.lowerIsBetter) || (deltaPct > 0 && !row.lowerIsBetter)) {
                        deltaClass = 'delta-good';
                    } else {
                        deltaClass = 'delta-bad';
                    }
                }
            } else if (row.a === 0 && row.b === 0) {
                deltaText = '0%';
            }

            tr.innerHTML = `
                <td>${row.name}</td>
                <td class="numeric">${fmt(row.a)}</td>
                <td class="numeric">${fmt(row.b)}</td>
                <td class="numeric ${deltaClass}">${deltaText}</td>
            `;
            tbody.appendChild(tr);
        });

        this.showModal();

        // Also update dashboard with current (B) metrics
        this.dashboard.render(metricsB);
    }

    showModal() {
        this.modal.classList.remove('hidden');
    }

    hideModal() {
        this.modal.classList.add('hidden');
    }
}

window.ComparisonMode = ComparisonMode;
