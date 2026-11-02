"""
Pytest configuration and environment setup for G-AI-ASSISTANT tests.
"""

from pathlib import Path
import sys

# Ensure project root is at the front of sys.path for test imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
