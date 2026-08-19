#!/bin/bash

# Ensure this script is executable
if [ ! -x "$0" ]; then
    chmod +x "$0"
fi

# Navigate to the script's directory
cd "$(dirname "$0")"

# Start your Python virtual environment if you use one, then run the server
# (Uncomment the source line below if your venv is in venv/ or .venv/)
# source .venv/bin/activate

.venv/bin/python server.py