# DeepTrace — Hardware-Aware Code Profiler

![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)
![Chart.js](https://img.shields.io/badge/Chart.js-4.x-FF6384.svg)
![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)

DeepTrace is a production-ready hardware-aware code profiler platform. Submit Python or Java code and receive interactive visualizations of low-level hardware metrics: memory allocation patterns, garbage collection events, system call overhead, and I/O bottlenecks.

## Features

- 📊 **Real-time Metrics:** Memory timeline, GC pauses, heap usage, syscall distribution, and allocation rate.
- ⚖️ **Side-by-Side Comparison:** Refactor your code and instantly see overlay graphs showing delta improvements (%).
- 🛡️ **Secure Sandboxing:** Automatic AST-based and Regex-based safety scans, strict timeouts, and memory limits.
- 🚀 **Multi-Language:** Supports Python (cProfile/tracemalloc/strace) and Java (JFR/jstat/strace).
- 🔌 **LKM Support:** Optional Linux Kernel Module for highly accurate kernel metrics.

## Quick Start (Docker)

The recommended way to run DeepTrace is using Docker, which automatically configures the necessary Linux tools (like `strace`) for full metric capture.

```bash
docker-compose up --build
```

Then open `http://localhost:3000` in your browser.

## Documentation

- [Setup Guide](docs/SETUP.md) - For local and cloud deployment.
- [Architecture Overview](docs/ARCHITECTURE.md) - Component design and data flow.
- [API Reference](docs/API.md) - HTTP and WebSocket endpoints.
- [Troubleshooting](docs/TROUBLESHOOTING.md) - Common issues and fixes.

## License
MIT
