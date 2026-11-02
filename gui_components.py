"""
Futuristic Anime Cyberpunk GUI Styling and Components for S.E.N.T.I.A.L.
Deep midnight backgrounds (#0B0C10, #0F101A), crimson (#FF2A6D), cyan (#05D9E8), and violet (#9D4EDD) neon accents,
glowing glass card containers, and custom anime-inspired UI components.
"""

from PySide6.QtCore import Qt, QTimer, QTime, QDate
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QLabel, QWidget


class LiquidGlassBackground(QWidget):
    """
    Anime Cyberpunk Canvas: Deep midnight/navy background with subtle neon radial/linear gradients.
    Lightweight and non-gpu heavy.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Deep anime cyberpunk background gradient
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        gradient.setColorAt(0.0, QColor(11, 12, 16))    # #0B0C10 Deepest Midnight
        gradient.setColorAt(0.4, QColor(15, 16, 26))   # #0F101A Midnight Violet
        gradient.setColorAt(0.8, QColor(18, 19, 32))   # #121320 Dark Cyber Blue
        gradient.setColorAt(1.0, QColor(9, 10, 15))    # #090A0F Base Dark

        painter.fillRect(self.rect(), gradient)

        # Subtle neon ambient glow top-right and bottom-left
        accent_pen = QPen(QColor(255, 42, 109, 20), 1)  # Soft Crimson
        painter.setPen(accent_pen)
        painter.drawLine(0, 0, self.width(), 0)


class LiquidGlassCard(QFrame):
    """
    Anime Glassmorphism Card Component with glowing neon border accents (Crimson / Cyan / Violet).
    """

    def __init__(self, parent=None, corner_radius=16, border_color=None):
        super().__init__(parent)
        self.corner_radius = corner_radius
        self.border_color = border_color or QColor(5, 217, 232, 110)  # Default Cyan #05D9E8
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_border_accent(self, color: QColor):
        self.border_color = color
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, self.corner_radius, self.corner_radius)

        # Translucent glass fill with dark indigo tint
        glass_color = QColor(15, 16, 28, 200)  # Semi-transparent dark card
        painter.fillPath(path, QBrush(glass_color))

        # Neon accent gradient border
        border_gradient = QLinearGradient(0, 0, self.width(), self.height())
        border_gradient.setColorAt(0.0, self.border_color)
        border_gradient.setColorAt(0.5, QColor(255, 255, 255, 30))
        border_gradient.setColorAt(1.0, QColor(157, 78, 221, 100))  # Violet #9D4EDD

        pen = QPen()
        pen.setBrush(border_gradient)
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.drawPath(path)


# Cyberpunk Anime QSS Stylesheet
ANIME_CYBERPUNK_QSS = """
/* Base Application Style */
QWidget {
    font-family: 'Segoe UI', 'Consolas', 'Arial', sans-serif;
    color: #E2E8F0;
    font-size: 13px;
}

/* Sidebar Navigation */
QListWidget#navSidebar {
    background-color: rgba(11, 12, 16, 0.95);
    border: none;
    border-right: 1px solid rgba(5, 217, 232, 0.25);
    outline: none;
    padding: 12px 6px;
}

QListWidget#navSidebar::item {
    color: #A0AEC0;
    padding: 12px 16px;
    margin: 4px 6px;
    border-radius: 10px;
    font-weight: 600;
    font-size: 13px;
    border: 1px solid transparent;
}

QListWidget#navSidebar::item:hover {
    background-color: rgba(5, 217, 232, 0.12);
    color: #05D9E8;
    border: 1px solid rgba(5, 217, 232, 0.3);
}

QListWidget#navSidebar::item:selected {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(255, 42, 109, 0.25), stop:1 rgba(157, 78, 221, 0.2));
    color: #FF2A6D;
    font-weight: bold;
    border: 1px solid rgba(255, 42, 109, 0.6);
}

/* Buttons */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #121320, stop:1 #1A1C30);
    color: #05D9E8;
    border: 1px solid rgba(5, 217, 232, 0.4);
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: bold;
    font-size: 12px;
}

QPushButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(5, 217, 232, 0.2), stop:1 rgba(157, 78, 221, 0.2));
    border: 1px solid #05D9E8;
    color: #FFFFFF;
}

QPushButton:pressed {
    background-color: rgba(255, 42, 109, 0.3);
    border: 1px solid #FF2A6D;
    color: #FF2A6D;
}

QPushButton#btnPrimary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF2A6D, stop:1 #9D4EDD);
    color: #FFFFFF;
    border: 1px solid #FF2A6D;
}

QPushButton#btnPrimary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF437D, stop:1 #B25EFF);
    border: 1px solid #05D9E8;
}

QPushButton#btnSecondary {
    background: transparent;
    color: #A0AEC0;
    border: 1px solid rgba(160, 174, 192, 0.3);
}

QPushButton#btnSecondary:hover {
    color: #05D9E8;
    border: 1px solid #05D9E8;
}

/* Text & Input Controls */
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: rgba(15, 16, 26, 0.85);
    color: #F7FAFC;
    border: 1px solid rgba(5, 217, 232, 0.3);
    border-radius: 8px;
    padding: 8px;
    selection-background-color: #FF2A6D;
}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid #05D9E8;
    background-color: rgba(18, 19, 32, 0.95);
}

/* Scroll Bars */
QScrollBar:vertical {
    background: rgba(11, 12, 16, 0.5);
    width: 8px;
    margin: 0px;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    background: rgba(5, 217, 232, 0.4);
    min-height: 20px;
    border-radius: 4px;
}

QScrollBar::handle:vertical:hover {
    background: #FF2A6D;
}

/* Labels & Status Headers */
QLabel#hdrAnimeTitle {
    font-size: 22px;
    font-weight: bold;
    color: #FF2A6D;
    letter-spacing: 1px;
}

QLabel#hdrAnimeSub {
    font-size: 12px;
    color: #05D9E8;
    letter-spacing: 0.5px;
}

QLabel#badgeStatus {
    background-color: rgba(5, 217, 232, 0.15);
    color: #05D9E8;
    border: 1px solid rgba(5, 217, 232, 0.4);
    border-radius: 12px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: bold;
}
"""
