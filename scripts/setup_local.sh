#!/bin/bash
set -e

echo "=== DeepTrace Local Setup (Linux/macOS) ==="

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: Python 3 is required."
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "✅ Python $PY_VER found."

# Create virtual environment
echo "Creating virtual environment in backend/venv..."
python3 -m venv backend/venv

# Install requirements
echo "Installing Python dependencies..."
source backend/venv/bin/activate
pip install -r backend/requirements.txt

# Check Java
if ! command -v javac &> /dev/null; then
    echo "⚠️ Warning: 'javac' not found. Java profiling will be disabled."
else
    echo "✅ Java compiler found."
fi

# Check strace
if ! command -v strace &> /dev/null; then
    echo "⚠️ Warning: 'strace' not found. System call metrics will be estimated."
    echo "   On Ubuntu/Debian: sudo apt install strace"
else
    echo "✅ strace found."
fi

# Create jobs dir
mkdir -p /tmp/deeptrace_jobs
chmod 777 /tmp/deeptrace_jobs
echo "✅ Created /tmp/deeptrace_jobs for temporary profiling files."

echo ""
echo "=== Setup Complete ==="
echo "To run DeepTrace locally:"
echo ""
echo "1. Start the Backend:"
echo "   cd backend"
echo "   source venv/bin/activate"
echo "   uvicorn main:app --reload"
echo ""
echo "2. Start the Frontend (in a new terminal):"
echo "   cd frontend"
echo "   python3 -m http.server 3000"
echo ""
echo "Then open http://localhost:3000 in your browser."
