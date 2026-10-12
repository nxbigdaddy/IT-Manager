"""Light/dark palettes derived from the single light stylesheet in workspace_ui.

Dark mode keeps every structural rule (radius, padding, arrow image) and only swaps
the palette tokens, so the two themes cannot drift apart in layout.
"""
from __future__ import annotations

# token -> replacement used when building the dark stylesheet
DARK_TOKENS = {
    '#f5f7fb': '#161b23',   # page background
    '#ffffff': '#1f2734',   # surfaces and inputs
    '#243249': '#e6edf6',   # main text
    '#174d8e': '#8ec2f5',   # brand headings
    '#173b65': '#8ec2f5',
    '#dde4ee': '#33404f',   # borders
    '#cfd9e6': '#3b4a5c',
    '#d5deeb': '#3b4a5c',
    '#dce3ee': '#33404f',
    '#e8edf4': '#33404f',
    '#eef3fa': '#25303d',   # table headers
    '#dceaff': '#2b3f57',   # selected tree item background
    '#edf4ff': '#25364a',   # hover surfaces
    '#f0f2f6': '#232c38',   # disabled surfaces
    '#9da7b5': '#7f8b9a',   # disabled text
    '#86abe0': '#4d7fb5',
    '#8ba9cf': '#4d7fb5',
    '#3977c5': '#6aa8ea',
    '#e8f1ff': '#2b3f57',   # selection background
    '#eef1f6': '#2a3543',   # gridlines
    '#fafcff': '#212936',   # alternate rows
    '#e7edf5': '#2a3543',   # splitter
    '#b8c7da': '#4a5b70',   # scrollbar handle
    '#f4f7fb': '#1c232e',
    '#eaf2fd': '#22304a',   # hint blocks
    '#fbfdff': '#212b39',
    '#e0e6ee': '#333c4a',
    '#94a3b8': '#7f8b9a',
    '#dce3ea': '#33404f',
    '#cfdaea': '#3b4a5c',
}
# Highlight colours used by primary buttons and stat cards keep enough contrast.
DARK_EXTRA = """
QPushButton[primary="true"] { background:#2f6bb0; color:#f2f7ff; }
QPushButton[stat="true"] { background:#1f2734; color:#e6edf6; border:1px solid #33404f; }
QLabel { color:#e6edf6; }
QFrame[card="true"] { background:#1f2734; border:1px solid #33404f; }
QHeaderView::section { color:#cfe0f2; }
QTableWidget { color:#e6edf6; }
QTreeWidget { color:#e6edf6; }
QTextBrowser { color:#e6edf6; background:#1f2734; }
QPlainTextEdit { background:#1f2734; color:#e6edf6; border:1px solid #33404f; border-radius:6px; }
QCheckBox { color:#e6edf6; }
QFrame#appTitleBar { background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #1b2a3d,stop:1 #24405f);
 border-bottom:1px solid #33506f; }
"""


def dark_stylesheet(base):
    """Return a dark variant of the light stylesheet without touching structure."""
    result = base
    for light, dark in DARK_TOKENS.items():
        result = result.replace(light, dark)
    return result + DARK_EXTRA


def scale_stylesheet(base, scale):
    """Apply a font scale (0.85–1.45) as one extra rule; layout stays untouched."""
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = 1.0
    scale = min(1.45, max(0.85, scale))
    if abs(scale - 1.0) < 0.001:
        return base
    px = round(13 * scale)
    small = max(10, px - 1)
    return base + (f'\nQWidget {{ font-size:{px}px; }}'
                   f'\nQHeaderView::section {{ font-size:{small}px; }}'
                   f'\nQPushButton[stat="true"] {{ font-size:{px + 2}px; }}\n')


def build_qss(base, theme='light', scale=1.0):
    """Compose the effective stylesheet for one theme + font scale."""
    result = dark_stylesheet(base) if theme == 'dark' else base
    return scale_stylesheet(result, scale)
