"""
Main application entry point for S.E.N.T.I.A.L.
Initializes PySide6 application, qasync event loop, logging, and launches the main window.
"""

import asyncio
import logging
import sys
import ctypes
from pathlib import Path

# Ensure project root is in sys.path so modules can always be imported regardless of execution cwd
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
import qasync

from Jarvis_UI import JarvisMainWindow
from utils import setup_logging

logger = logging.getLogger("main")


def set_windows_app_id():
    """Sets Windows AppUserModelID for proper taskbar icon handling."""
    if sys.platform == "win32":
        try:
            myappid = "sential.desktop.v2"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception as e:
            logger.debug(f"Could not set AppUserModelID: {e}")


def main():
    """Entry point for S.E.N.T.I.A.L."""
    setup_logging(level=logging.INFO)
    logger.info("Starting S.E.N.T.I.A.L. Desktop Application...")

    set_windows_app_id()

    # Create PySide6 Application
    app = QApplication(sys.argv)
    app.setApplicationName("S.E.N.T.I.A.L.")
    app.setOrganizationName("S.E.N.T.I.A.L.")

    # Integrate Qt event loop with asyncio via qasync
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    # Initialize Main Window
    window = JarvisMainWindow()
    window.show()

    logger.info("Application event loop started.")
    with loop:
        exit_code = loop.run_forever()
        logger.info(f"Application closed with exit code: {exit_code}")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
