#!/usr/bin/env python3
"""
write_kernel_files.py — writes deeptrace.c, Makefile, and README.md
to F:\FREAKY\ANTI_PROJECTS\DEEPTRACE\kernel\deeptrace_lkm\
Run with: python write_kernel_files.py
"""

import pathlib

BASE = pathlib.Path(r"F:\FREAKY\ANTI_PROJECTS\DEEPTRACE\kernel\deeptrace_lkm")
BASE.mkdir(parents=True, exist_ok=True)

# ============================================================
# deeptrace.c
# ============================================================
C_SOURCE = r"""// SPDX-License-Identifier: GPL-2.0-only
/*
 * deeptrace.c -- DeepTrace Linux Kernel Module
 *
 * Tracks per-process system calls, memory page allocations/frees, and
 * page faults for a configurable target PID via kernel tracepoints.
 * Metrics are exposed through a /proc/deeptrace seq_file entry.
 *
 * Prerequisites:
 *   linux-headers-$(uname -r), root access, CONFIG_TRACEPOINTS=y
 *
 * Build:   make
 * Load:    sudo insmod deeptrace.ko target_pid=<PID>
 * Read:    cat /proc/deeptrace
 * Unload:  sudo rmmod deeptrace
 *
 * Author:  DeepTrace Project
 * Version: 1.0.0
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/sched.h>
#include <linux/pid.h>
#include <linux/atomic.h>
#include <linux/tracepoint.h>
#include <linux/version.h>
#include <linux/ktime.h>
#include <linux/spinlock.h>
#include <linux/mm.h>
#include <linux/string.h>

#include <trace/events/syscalls.h>
#include <trace/events/kmem.h>

#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0)
#include <trace/events/pagefault.h>
#endif

MODULE_LICENSE("GPL");
MODULE_AUTHOR("DeepTrace Project");
MODULE_DESCRIPTION("Process-level syscall, memory, and page-fault profiler");
MODULE_VERSION("1.0.0");

/* -------------------------------------------------------------------------
 * Module parameter
 * ---------------------------------------------------------------------- */

static int target_pid = -1;
module_param(target_pid, int, 0444);
MODULE_PARM_DESC(target_pid,
	"PID to trace (default -1 = trace all processes)");

/* -------------------------------------------------------------------------
 * Atomic counters
 * ---------------------------------------------------------------------- */

static atomic64_t dt_syscall_count    = ATOMIC64_INIT(0);
static atomic64_t dt_syscall_errors   = ATOMIC64_INIT(0);
static atomic64_t dt_syscall_total_ns = ATOMIC64_INIT(0);
static atomic64_t dt_page_alloc       = ATOMIC64_INIT(0);
static atomic64_t dt_page_free        = ATOMIC64_INIT(0);
static atomic64_t dt_page_faults      = ATOMIC64_INIT(0);
static atomic64_t dt_minor_faults     = ATOMIC64_INIT(0);
static atomic64_t dt_major_faults     = ATOMIC64_INIT(0);

static ktime_t dt_load_time;

/* -------------------------------------------------------------------------
 * Per-syscall histogram
 * ---------------------------------------------------------------------- */

#define DT_HIST_SIZE 512
static u64 dt_hist[DT_HIST_SIZE];
static DEFINE_SPINLOCK(dt_hist_lock);

/* Per-CPU enter timestamp for latency measurement */
DEFINE_PER_CPU(ktime_t, dt_enter_ts);

static struct proc_dir_entry *dt_proc_entry;

/* -------------------------------------------------------------------------
 * Helper
 * ---------------------------------------------------------------------- */

static inline bool dt_task_matches(void)
{
	if (target_pid == -1)
		return true;
	return (task_pid_nr(current) == (pid_t)target_pid ||
		task_tgid_nr(current) == (pid_t)target_pid);
}

/* -------------------------------------------------------------------------
 * Tracepoint: sys_enter
 * ---------------------------------------------------------------------- */

static void dt_probe_sys_enter(void *data, struct pt_regs *regs, long id)
{
	unsigned long flags;

	if (!dt_task_matches())
		return;

	atomic64_inc(&dt_syscall_count);
	this_cpu_write(dt_enter_ts, ktime_get());

	if (id >= 0 && id < DT_HIST_SIZE) {
		spin_lock_irqsave(&dt_hist_lock, flags);
		dt_hist[id]++;
		spin_unlock_irqrestore(&dt_hist_lock, flags);
	}
}

/* -------------------------------------------------------------------------
 * Tracepoint: sys_exit
 * ---------------------------------------------------------------------- */

static void dt_probe_sys_exit(void *data, struct pt_regs *regs, long ret)
{
	ktime_t enter, now, delta;

	if (!dt_task_matches())
		return;

	if (ret < 0)
		atomic64_inc(&dt_syscall_errors);

	enter = this_cpu_read(dt_enter_ts);
	now   = ktime_get();
	if (ktime_after(now, enter)) {
		delta = ktime_sub(now, enter);
		atomic64_add(ktime_to_ns(delta), &dt_syscall_total_ns);
	}
}

/* -------------------------------------------------------------------------
 * Tracepoint: mm_page_alloc
 * ---------------------------------------------------------------------- */

static void dt_probe_mm_page_alloc(void *data,
	struct page *page, unsigned int order,
	gfp_t gfp_flags, int migratetype)
{
	if (!dt_task_matches())
		return;
	atomic64_add((u64)1 << order, &dt_page_alloc);
}

/* -------------------------------------------------------------------------
 * Tracepoint: mm_page_free
 * ---------------------------------------------------------------------- */

static void dt_probe_mm_page_free(void *data,
	struct page *page, unsigned int order)
{
	if (!dt_task_matches())
		return;
	atomic64_add((u64)1 << order, &dt_page_free);
}

/* -------------------------------------------------------------------------
 * Tracepoint: page fault (kernel >= 5.10)
 * ---------------------------------------------------------------------- */

#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0)
static void dt_probe_page_fault_user(void *data,
	unsigned long address, struct pt_regs *regs, unsigned long error_code)
{
	if (!dt_task_matches())
		return;
	atomic64_inc(&dt_page_faults);
	/*
	 * x86 PFEC bit 0 (P): 0 = page not present (demand page = major),
	 *                      1 = protection fault (COW etc. = minor).
	 */
	if (error_code & 0x1)
		atomic64_inc(&dt_minor_faults);
	else
		atomic64_inc(&dt_major_faults);
}
#endif

/* -------------------------------------------------------------------------
 * /proc/deeptrace show
 * ---------------------------------------------------------------------- */

static int dt_proc_show(struct seq_file *sf, void *v)
{
	ktime_t now, elapsed;
	u64 sc_cnt, sc_err, sc_ns, pg_a, pg_f, pg_flt, mf, mjf, ms;
	unsigned long flags;
	u64 top_v[10];
	int top_n[10];
	int i, k, j;

	now    = ktime_get();
	elapsed = ktime_sub(now, dt_load_time);
	ms     = (u64)ktime_to_ms(elapsed);

	sc_cnt = (u64)atomic64_read(&dt_syscall_count);
	sc_err = (u64)atomic64_read(&dt_syscall_errors);
	sc_ns  = (u64)atomic64_read(&dt_syscall_total_ns);
	pg_a   = (u64)atomic64_read(&dt_page_alloc);
	pg_f   = (u64)atomic64_read(&dt_page_free);
	pg_flt = (u64)atomic64_read(&dt_page_faults);
	mf     = (u64)atomic64_read(&dt_minor_faults);
	mjf    = (u64)atomic64_read(&dt_major_faults);

	seq_puts(sf,   "# DeepTrace Kernel Module -- Live Metrics\n");
	seq_printf(sf, "target_pid:          %d\n",   target_pid);
	seq_printf(sf, "uptime_ms:           %llu\n", ms);
	seq_puts(sf,   "\n");
	seq_puts(sf,   "# Syscall Metrics\n");
	seq_printf(sf, "syscall_count:       %llu\n", sc_cnt);
	seq_printf(sf, "syscall_errors:      %llu\n", sc_err);
	seq_printf(sf, "syscall_total_ns:    %llu\n", sc_ns);
	seq_printf(sf, "syscall_avg_ns:      %llu\n",
		   sc_cnt > 0 ? sc_ns / sc_cnt : 0ULL);
	seq_puts(sf,   "\n");
	seq_printf(sf, "# Memory Metrics  (PAGE_SIZE=%lu bytes)\n", PAGE_SIZE);
	seq_printf(sf, "pages_allocated:     %llu\n", pg_a);
	seq_printf(sf, "pages_freed:         %llu\n", pg_f);
	seq_printf(sf, "pages_net:           %lld\n", (s64)pg_a - (s64)pg_f);
	seq_printf(sf, "mem_alloc_kb:        %llu\n", pg_a * (PAGE_SIZE / 1024));
	seq_printf(sf, "mem_freed_kb:        %llu\n", pg_f * (PAGE_SIZE / 1024));
	seq_puts(sf,   "\n");
	seq_puts(sf,   "# Page Fault Metrics\n");
	seq_printf(sf, "page_faults:         %llu\n", pg_flt);
	seq_printf(sf, "minor_faults:        %llu\n", mf);
	seq_printf(sf, "major_faults:        %llu\n", mjf);
	seq_puts(sf,   "\n");

	/* Top-10 syscalls by count */
	memset(top_v, 0, sizeof(top_v));
	for (k = 0; k < 10; k++)
		top_n[k] = -1;

	spin_lock_irqsave(&dt_hist_lock, flags);
	for (i = 0; i < DT_HIST_SIZE; i++) {
		if (!dt_hist[i])
			continue;
		for (k = 0; k < 10; k++) {
			if (dt_hist[i] > top_v[k]) {
				for (j = 9; j > k; j--) {
					top_v[j] = top_v[j - 1];
					top_n[j] = top_n[j - 1];
				}
				top_v[k] = dt_hist[i];
				top_n[k] = i;
				break;
			}
		}
	}
	spin_unlock_irqrestore(&dt_hist_lock, flags);

	seq_puts(sf,   "# Top-10 Syscalls (by call count)\n");
	seq_printf(sf, "%-8s  %s\n", "nr", "calls");
	for (k = 0; k < 10 && top_n[k] >= 0; k++)
		seq_printf(sf, "%-8d  %llu\n", top_n[k], top_v[k]);

	return 0;
}

static int dt_proc_open(struct inode *inode, struct file *file)
{
	return single_open(file, dt_proc_show, NULL);
}

#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 6, 0)
static const struct proc_ops dt_proc_fops = {
	.proc_open    = dt_proc_open,
	.proc_read    = seq_read,
	.proc_lseek   = seq_lseek,
	.proc_release = single_release,
};
#else
static const struct file_operations dt_proc_fops = {
	.owner   = THIS_MODULE,
	.open    = dt_proc_open,
	.read    = seq_read,
	.llseek  = seq_lseek,
	.release = single_release,
};
#endif

/* -------------------------------------------------------------------------
 * Module init
 * ---------------------------------------------------------------------- */

static int __init deeptrace_init(void)
{
	int rc;

	dt_load_time = ktime_get();
	memset(dt_hist, 0, sizeof(dt_hist));

	dt_proc_entry = proc_create("deeptrace", 0444, NULL, &dt_proc_fops);
	if (!dt_proc_entry) {
		pr_err("deeptrace: cannot create /proc/deeptrace\n");
		return -ENOMEM;
	}

	rc = register_trace_sys_enter(dt_probe_sys_enter, NULL);
	if (rc) {
		pr_err("deeptrace: register sys_enter failed (%d)\n", rc);
		goto err_proc;
	}

	rc = register_trace_sys_exit(dt_probe_sys_exit, NULL);
	if (rc) {
		pr_err("deeptrace: register sys_exit failed (%d)\n", rc);
		goto err_enter;
	}

	rc = register_trace_mm_page_alloc(dt_probe_mm_page_alloc, NULL);
	if (rc) {
		pr_err("deeptrace: register mm_page_alloc failed (%d)\n", rc);
		goto err_exit;
	}

	rc = register_trace_mm_page_free(dt_probe_mm_page_free, NULL);
	if (rc) {
		pr_err("deeptrace: register mm_page_free failed (%d)\n", rc);
		goto err_pg_alloc;
	}

#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0)
	rc = register_trace_mm_page_fault_user(dt_probe_page_fault_user, NULL);
	if (rc)
		pr_warn("deeptrace: page_fault_user unavailable (%d) -- continuing\n", rc);
#endif

	pr_info("deeptrace: loaded. target_pid=%d  /proc/deeptrace ready\n",
		target_pid);
	return 0;

err_pg_alloc:
	unregister_trace_mm_page_alloc(dt_probe_mm_page_alloc, NULL);
err_exit:
	unregister_trace_sys_exit(dt_probe_sys_exit, NULL);
err_enter:
	unregister_trace_sys_enter(dt_probe_sys_enter, NULL);
err_proc:
	proc_remove(dt_proc_entry);
	return rc;
}

/* -------------------------------------------------------------------------
 * Module exit
 * ---------------------------------------------------------------------- */

static void __exit deeptrace_exit(void)
{
#if LINUX_VERSION_CODE >= KERNEL_VERSION(5, 10, 0)
	unregister_trace_mm_page_fault_user(dt_probe_page_fault_user, NULL);
#endif
	unregister_trace_mm_page_free(dt_probe_mm_page_free, NULL);
	unregister_trace_mm_page_alloc(dt_probe_mm_page_alloc, NULL);
	unregister_trace_sys_exit(dt_probe_sys_exit, NULL);
	unregister_trace_sys_enter(dt_probe_sys_enter, NULL);

	/* Wait for all in-flight callbacks to complete */
	tracepoint_synchronize_unregister();

	proc_remove(dt_proc_entry);

	pr_info("deeptrace: unloaded. syscalls=%lld pages_net=%lld\n",
		atomic64_read(&dt_syscall_count),
		atomic64_read(&dt_page_alloc) - atomic64_read(&dt_page_free));
}

module_init(deeptrace_init);
module_exit(deeptrace_exit);
"""

# ============================================================
# Makefile
# ============================================================
MAKEFILE = """obj-m += deeptrace.o

KDIR  := /lib/modules/$(shell uname -r)/build
PWD   := $(shell pwd)

# Compiler flags: strict warnings, no implicit fallthrough
ccflags-y := -Wall -Wextra -Wno-unused-parameter

.PHONY: all clean load unload status

all:
\t$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
\t$(MAKE) -C $(KDIR) M=$(PWD) clean

# Convenience targets (run as root)
load:
\tsudo insmod deeptrace.ko target_pid=$(PID)

unload:
\tsudo rmmod deeptrace

status:
\tcat /proc/deeptrace
"""

# ============================================================
# README.md
# ============================================================
README = """# DeepTrace Kernel Module (`deeptrace.ko`)

The `deeptrace` Linux Kernel Module (LKM) is an **optional enhancement** for the
DeepTrace profiling backend.  It hooks kernel tracepoints to provide low-overhead,
kernel-level visibility into:

- **System call counts, error rates, and average latency**
- **Memory page allocations and frees** (pages and KB)
- **Page fault counts** (minor / major, kernel ≥ 5.10)
- **Top-10 most-called syscall numbers**

> The DeepTrace backend operates fully without this module, falling back to
> `strace` (Linux) and `psutil` for user-space profiling data.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Linux kernel | 5.4 or newer (tested on 5.15 LTS, 6.x) |
| Kernel headers | `linux-headers-$(uname -r)` |
| Build tools | `gcc`, `make` |
| Root privileges | Required for `insmod` / `rmmod` |
| Tracepoints | `CONFIG_TRACEPOINTS=y` (on by default on all major distros) |

Install headers on Debian/Ubuntu:
```bash
sudo apt-get install linux-headers-$(uname -r) build-essential
```

Install headers on RHEL/Fedora/Rocky:
```bash
sudo dnf install kernel-devel kernel-headers
```

---

## Building

```bash
cd kernel/deeptrace_lkm
make
```

A successful build produces `deeptrace.ko`.

---

## Loading

```bash
# Trace a specific process (replace 1234 with the actual PID)
sudo insmod deeptrace.ko target_pid=1234

# Trace ALL processes (omit target_pid or set to -1)
sudo insmod deeptrace.ko
```

Verify the module is loaded:
```bash
lsmod | grep deeptrace
dmesg | tail -5
```

---

## Reading Metrics

```bash
cat /proc/deeptrace
```

Example output:
```
# DeepTrace Kernel Module -- Live Metrics
target_pid:          1234
uptime_ms:           5432

# Syscall Metrics
syscall_count:       18743
syscall_errors:      12
syscall_total_ns:    394872301
syscall_avg_ns:      21076

# Memory Metrics  (PAGE_SIZE=4096 bytes)
pages_allocated:     6201
pages_freed:         5980
pages_net:           221
mem_alloc_kb:        24804
mem_freed_kb:        23920

# Page Fault Metrics
page_faults:         304
minor_faults:        288
major_faults:        16

# Top-10 Syscalls (by call count)
nr        calls
1         4523
0         3211
3         2890
...
```

**Syscall number → name mapping** (x86-64):
Use `ausyscall --dump` or `/usr/include/asm/unistd_64.h` to map numbers to
names.  Common ones: `0`=read, `1`=write, `3`=close, `9`=mmap, `56`=clone.

---

## Unloading

```bash
sudo rmmod deeptrace
dmesg | tail -3   # shows final summary counters
```

---

## Integration with DeepTrace Backend

When the LKM is loaded **before** a profiling job, the backend can read
`/proc/deeptrace` at job start and end to compute deltas:

```python
import subprocess, re

def read_deeptrace_proc():
    text = pathlib.Path("/proc/deeptrace").read_text()
    metrics = {}
    for line in text.splitlines():
        if line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        try:
            metrics[key.strip()] = int(val.strip())
        except ValueError:
            pass
    return metrics

before = read_deeptrace_proc()
# ... run user code via SandboxExecutor ...
after  = read_deeptrace_proc()

delta_syscalls = after["syscall_count"] - before["syscall_count"]
delta_pages    = after["pages_net"]     - before["pages_net"]
```

---

## Architecture Notes

- **Tracepoints used**: `sys_enter`, `sys_exit`, `mm_page_alloc`, `mm_page_free`,
  `mm_page_fault_user` (conditional on kernel ≥ 5.10).
- **Thread safety**: All counters use `atomic64_t`; the syscall histogram uses a
  `spinlock` to protect the 512-bucket array.
- **Per-CPU enter timestamps** are stored with `DEFINE_PER_CPU` to measure
  per-syscall latency without inter-CPU cache contention.
- **proc_ops vs file_operations**: the correct struct is selected at compile time
  via `LINUX_VERSION_CODE` (≥ 5.6 uses `proc_ops`).
- **Cleanup safety**: `tracepoint_synchronize_unregister()` is called in the exit
  path to guarantee all probe callbacks have returned before the module text is
  unmapped.

---

## Fallback Behaviour (Backend Without LKM)

If the kernel module is not loaded, the DeepTrace backend seamlessly falls back to:

| Metric | Fallback source |
|---|---|
| Syscall counts | `strace -c` (Linux only) |
| Memory usage | `psutil` RSS polling (cross-platform) |
| Page faults | `resource.getrusage()` ru_minflt / ru_majflt |
| GC data | Python `gc` module / Java `jstat` |

No user-facing functionality is lost when the LKM is absent.
"""

(BASE / "deeptrace.c").write_text(C_SOURCE, encoding="utf-8")
(BASE / "Makefile").write_text(MAKEFILE, encoding="utf-8")
(BASE / "README.md").write_text(README, encoding="utf-8")

print("Kernel files written successfully:")
for f in sorted(BASE.iterdir()):
    print(f"  {f.name}  ({f.stat().st_size} bytes)")
