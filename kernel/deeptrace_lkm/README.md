# DeepTrace Linux Kernel Module (LKM)

The DeepTrace LKM is an optional kernel-level profiling component that uses kernel tracepoints to collect highly accurate, zero-overhead metrics on system calls, memory page allocations, and page frees.

**Note:** The DeepTrace backend automatically falls back to `strace` and `psutil` if this module is not loaded. This module is intended for advanced, production-grade deployments on controlled Linux environments.

## Prerequisites

- Linux kernel headers installed (`sudo apt install linux-headers-$(uname -r)`)
- Root privileges
- `make` and `gcc`

## Build and Load

1. **Build the module:**
   ```bash
   make
   ```

2. **Load the module** (specify the target PID to profile):
   ```bash
   sudo insmod deeptrace.ko target_pid=1234
   ```

3. **Verify it's loaded:**
   ```bash
   dmesg | tail
   ```

## Reading Metrics

The module exposes real-time metrics via procfs. You can read them directly:

```bash
cat /proc/deeptrace
```

**Example output:**
```json
{
  "target_pid": 1234,
  "syscalls": 45012,
  "page_allocs": 1502,
  "page_frees": 890
}
```

## Unload

When finished profiling, remove the module:

```bash
sudo rmmod deeptrace
make clean
```
