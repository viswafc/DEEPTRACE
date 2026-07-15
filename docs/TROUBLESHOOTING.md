# Troubleshooting

## Strace not available
**Symptom**: Syscall graphs are empty, backend reports `strace_available: false`.
**Fix**: `strace` is a Linux-only tool. On Windows/macOS, DeepTrace falls back to `psutil`. For full metrics, run DeepTrace in Docker or natively on Linux.

## Docker strace capability
**Symptom**: `strace` fails inside the Docker container with `ptrace: Operation not permitted`.
**Fix**: Ensure your `docker-compose.yml` includes:
```yaml
cap_add:
  - SYS_PTRACE
security_opt:
  - seccomp:unconfined
```

## Java not found
**Symptom**: Java submissions fail instantly.
**Fix**: Ensure `javac` and `java` are on your system `PATH`. DeepTrace requires OpenJDK 11+ for JFR support (OpenJDK 17 recommended).

## WebSocket Disconnects
**Symptom**: UI falls back to polling ("Connecting...").
**Fix**: If behind a reverse proxy (like Nginx), ensure `Upgrade` headers are passed. See `docker/nginx.conf` for the correct configuration.

## Permission Errors on Job Directory
**Symptom**: `Permission denied` creating temp files.
**Fix**: Ensure the DeepTrace process has read/write permissions to `/tmp/deeptrace_jobs` (or `%TEMP%\deeptrace_jobs` on Windows).
