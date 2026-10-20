# api/index.py
import sys
import os

# Add the parent directory to path so we can import app
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

# This is required for Vercel
handler = app