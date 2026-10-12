"""QSS-controlled main-window title bar with native move/resize operations."""
from pathlib import Path
from PySide6.QtCore import Qt,QEvent,QObject
from PySide6.QtWidgets import QFrame,QHBoxLayout,QLabel,QPushButton,QApplication

STYLE_PATH=Path(__file__).parent/'resources'/'titlebar.qss'

def titlebar_style():
    return STYLE_PATH.read_text(encoding='utf-8')

class TitleBar(QFrame):
    def __init__(self,window):
        super().__init__(window)
        self.host=window;self.setObjectName('appTitleBar');self.setFixedHeight(40)
        row=QHBoxLayout(self);row.setContentsMargins(0,0,0,0);row.setSpacing(0)
        self.title=QLabel(window.windowTitle());self.title.setObjectName('appTitle')
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents);row.addWidget(self.title,1)
        window.windowTitleChanged.connect(self.title.setText)
        self.minimize=self._button('−','最小化','windowMinimize',window.showMinimized,row)
        self.maximize=self._button('□','最大化 / 还原','windowMaximize',self.toggle_maximized,row)
        self.close_button=self._button('×','关闭','windowClose',window.close,row)
        window.installEventFilter(self)
    def _button(self,text,tooltip,name,callback,row):
        button=QPushButton(text);button.setObjectName(name);button.setProperty('windowControl',True)
        button.setToolTip(tooltip);button.setAccessibleName(tooltip);button.setFocusPolicy(Qt.StrongFocus)
        button.clicked.connect(callback);row.addWidget(button);return button
    def toggle_maximized(self):
        self.host.showNormal() if self.host.isMaximized() else self.host.showMaximized()
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:
            handle=self.host.windowHandle()
            if handle:handle.startSystemMove()
            event.accept();return
        super().mousePressEvent(event)
    def mouseDoubleClickEvent(self,event):
        if event.button()==Qt.LeftButton:self.toggle_maximized();event.accept();return
        super().mouseDoubleClickEvent(event)
    def eventFilter(self,obj,event):
        if obj is getattr(self,'host',None):
            if event.type()==QEvent.WindowStateChange:
                self.maximize.setText('❐' if self.host.isMaximized() else '□')
            elif event.type() in (QEvent.WindowActivate,QEvent.WindowDeactivate):
                self.setProperty('inactive',event.type()==QEvent.WindowDeactivate)
                self.style().unpolish(self);self.style().polish(self);self.update()
        return False

class ResizeEdges(QObject):
    """Observe descendants so child widgets do not swallow the resize hit zone."""
    def __init__(self,window):
        super().__init__(window);self.host=window
        QApplication.instance().installEventFilter(self)
    def eventFilter(self,obj,event):
        if getattr(self,'host',None) is None:return False
        if event.type()!=QEvent.MouseButtonPress or not hasattr(obj,'window') or obj.window() is not self.host:return False
        if event.button()!=Qt.LeftButton or self.host.isMaximized() or self.host.isFullScreen():return False
        point=self.host.mapFromGlobal(event.globalPosition().toPoint());edges=Qt.Edges()
        if point.x()<5:edges|=Qt.LeftEdge
        elif point.x()>=self.host.width()-5:edges|=Qt.RightEdge
        if point.y()<5:edges|=Qt.TopEdge
        elif point.y()>=self.host.height()-5:edges|=Qt.BottomEdge
        handle=self.host.windowHandle()
        if edges and handle:return bool(handle.startSystemResize(edges))
        return False

def install_chrome(window,layout):
    window.setWindowFlag(Qt.FramelessWindowHint,True)
    bar=TitleBar(window);layout.insertWidget(0,bar)
    window.title_bar=bar;window.resize_edges=ResizeEdges(window)
    return bar
