# DeepTrace ⚡ - Hardware-Aware Profiler

DeepTrace is a production-ready, interactive, and hardware-aware code profiling platform. It executes user-submitted code in sandboxed environments, gathers low-level execution and resource usage metrics, detects performance bottlenecks, and visualizes them in real-time.

---

## 🚀 Features

- **Interactive Micro-Metrics Dashboard**: Real-time charts showing memory allocations, heap usage over time, Garbage Collection (GC) events/pauses, and User vs. Kernel CPU distribution.
- **Dual-Panel Workstation**: A slick visual layout with a Monaco-powered code editor on the left and a live visualization dashboard on the right.
- **Multi-Language Support**: Support for profiling **Python**, **Java**, **C**, and **C++** code.
- **Side-by-Side Performance Comparison**: Compare performance metrics between code iterations (e.g., inefficient vs. optimized versions) and compute performance deltas.
- **Language-Specific Bottleneck Flagging**: Automated static and dynamic detection of high-overhead patterns (such as un-freed allocations in C/C++, recursion issues/blocking sleep in Python, and large GC pauses in Java).
- **Aesthetic Premium UI**: Modern glassmorphism UI styled with a curated CSS system, including smooth transitions, micro-animations, and a dark/light mode toggle.

---

## 📁 Project Structure

```text
deep by ds/
├── .gitignore                  # Excluded folders and files from git tracking
├── README.md                   # This documentation
├── prompt.md                   # Initial project requirements
└── deeptrace/
    ├── backend/                # FastAPI application
    │   ├── app/
    │   │   ├── main.py         # REST API endpoints & job orchestration
    │   │   ├── models.py       # Pydantic data schemas
    │   │   └── profiler.py     # Execution sandboxing and profiling logic
    │   └── requirements.txt    # Python backend dependencies
    ├── frontend/               # Single-page web app
    │   ├── index.html          # Dashboard structure and layout
    │   ├── styles.css          # Glassmorphic responsive styling & transitions
    │   └── script.js           # Monaco Editor config, API client, and Chart.js logic
    ├── test_samples/           # Test code snippets
    └── test_api.py             # CLI validation script for the API
```

---

## 🛠️ Technology Stack

- **Backend**: Python 3.11+, [FastAPI](https://fastapi.tiangolo.com/) (high-performance async framework), [Uvicorn](https://www.uvicorn.org/) (ASGI server), `psutil` (process & system monitoring).
- **Frontend**: HTML5, Vanilla CSS3 (custom variable system, responsive glassmorphism layout), Vanilla JavaScript.
- **Libraries**:
  - [Monaco Editor](https://microsoft.github.io/monaco-editor/) (VS Code editor core) for syntax highlighting and code editing.
  - [Chart.js](https://www.chartjs.org/) for beautiful, responsive metrics charts.
  - [GSAP](https://gsap.com/) for fluid entrance and micro-interactions.

---

## 💻 Setup & Running Locally

### 1. Backend Setup
1. Open a terminal and navigate to the backend directory:
   ```bash
   cd deeptrace/backend
   ```
2. Create and activate a Python virtual environment:
   ```bash
   python -m venv venv
   # On Windows:
   .\venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```
3. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Start the FastAPI development server:
   ```bash
   uvicorn app.main:app --reload
   ```
   The backend server will run at [http://127.0.0.1:8000](http://127.0.0.1:8000).

### 2. Frontend Access
The FastAPI backend serves the frontend statically. Once your backend is running, open your web browser and navigate to:
👉 **[http://127.0.0.1:8000/](http://127.0.0.1:8000/)** (which automatically redirects to `/static/index.html`).

---

## 🧪 Testing the API

To verify that the profiling engine is fully functional across Python, Java, C, and C++ metrics:
1. Ensure the FastAPI server is running.
2. Run the automated test script:
   ```bash
   python deeptrace/test_api.py
   ```
3. It will submit various code blocks to the API, poll for status, retrieve metrics, and log results.

---



~Author: Viswa AG Be/CSE

