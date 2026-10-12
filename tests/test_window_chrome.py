import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QMainWindow,QWidget,QVBoxLayout
from itmanager.window_chrome import install_chrome,STYLE_PATH

def test_qss_chrome_controls_and_title():
    app=QApplication.instance() or QApplication([])
    window=QMainWindow();root=QWidget();window.setCentralWidget(root)
    bar=install_chrome(window,QVBoxLayout(root))
    assert window.windowFlags() & Qt.FramelessWindowHint
    assert STYLE_PATH.exists() and '#appTitleBar' in STYLE_PATH.read_text(encoding='utf-8')
    window.setWindowTitle('IT管理测试');assert bar.title.text()=='IT管理测试'
    window.show();app.processEvents()
    bar.maximize.click();assert window.isMaximized()
    bar.maximize.click();assert not window.isMaximized()
    bar.minimize.click();assert window.isMinimized()
    window.showNormal();bar.close_button.click();assert not window.isVisible()
