from PyQt4 import uic
from PyQt4 import Qt
from PyQt4.QtCore import pyqtSignal, QObject, QEvent
from PyQt4.QtGui import QMainWindow, QWidget, QHBoxLayout, QMenu, \
                        QMenuBar, QFrame, QLabel, QStackedLayout, \
                        QStackedWidget, qApp, QFileDialog, QKeySequence, QMessageBox, \
                        QStandardItemModel, QTreeWidgetItem, QTreeWidget, QFont, \
                        QBrush, QColor, QAbstractItemView, QProgressBar, QApplication
from PyQt4 import QtCore

import h5py
import traceback
import os
from functools import partial

from ilastik.versionManager import VersionManager
from ilastik.utility import bind
from ilastik.utility.gui import ThunkEvent, ThunkEventHandler
from lazyflow.graph import MultiOutputSlot

import sys
import logging
logger = logging.getLogger(__name__)
traceLogger = logging.getLogger("TRACE." + __name__)
from lazyflow.tracer import Tracer

import ilastik.ilastik_logging

from ilastik.applets.base.applet import Applet, ControlCommand, ShellRequest
from ilastik.applets.base.appletGuiInterface import AppletGuiInterface

from ilastik.shell.projectManager import ProjectManager

import platform
import numpy

import threading

class ShellActions(object):
    """
    The shell provides the applet constructors with access to his GUI actions.
    They are provided in this class.
    """
    def __init__(self):
        self.openProjectAction = None
        self.saveProjectAction = None
        self.importProjectAction = None
        self.QuitAction = None

class SideSplitterSizePolicy(object):
    Manual = 0
    AutoCurrentDrawer = 1
    AutoLargestDrawer = 2

class ProgressDisplayManager(QObject):
    """
    Manages progress signals from applets and displays them in the status bar.
    """
    
    # Instead of connecting to applet progress signals directly,
    # we forward them through this qt signal.
    # This way we get the benefits of a queued connection without 
    #  requiring the applet interface to be dependent on qt.
    dispatchSignal = pyqtSignal(int, int, "bool")
    
    def __init__(self, statusBar):
        """
        """
        super(ProgressDisplayManager, self).__init__( parent=statusBar.parent() )
        self.statusBar = statusBar
        self.appletPercentages = {} # applet_index : percent_progress
        self.progressBar = None

        # Route all signals we get through a queued connection, to ensure that they are handled in the GUI thread        
        self.dispatchSignal.connect(self.handleAppletProgressImpl)
        
    def addApplet(self, index, app):
        # Subscribe to progress updates from this applet,
        # and include the applet index in the signal parameters.
        app.progressSignal.connect( bind(self.handleAppletProgress, index) )
        
        # Also subscribe to this applet's serializer progress updates.
        # (Progress will always come from either the serializer or the applet itself; not both at once.)
        for serializer in app.dataSerializers:
            serializer.progressSignal.connect( bind( self.handleAppletProgress, index ) )
        
        

    def handleAppletProgress(self, index, percentage, cancelled=False):
        # Forward the signal to the handler via our qt signal, which provides a queued connection.
        self.dispatchSignal.emit( index, percentage, cancelled )

    def handleAppletProgressImpl(self, index, percentage, cancelled):
        # No need for locking; this function is always run from the GUI thread
        with Tracer(traceLogger, msg="from applet {}: {}%, cancelled={}".format(index, percentage, cancelled)):
            if cancelled:
                if index in self.appletPercentages.keys():
                    del self.appletPercentages[index]
            else:
                # Take max (never go back down)
                if index in self.appletPercentages:
                    oldPercentage = self.appletPercentages[index]
                    self.appletPercentages[index] = max(percentage, oldPercentage)
                # First percentage we get MUST be zero.
                # Other notifications are ignored.
                if index in self.appletPercentages or percentage == 0:
                    self.appletPercentages[index] = percentage
    
            numActive = len(self.appletPercentages)
            if numActive > 0:
                totalPercentage = numpy.sum(self.appletPercentages.values()) / numActive
            
            if numActive == 0 or totalPercentage == 100:
                if self.progressBar is not None:
                    self.statusBar.removeWidget(self.progressBar)
                    self.progressBar = None
                    self.appletPercentages.clear()
            else:
                if self.progressBar is None:
                    self.progressBar = QProgressBar()
                    self.statusBar.addWidget(self.progressBar)
                self.progressBar.setValue(totalPercentage)

class IlastikShell( QMainWindow ):
    """
    The GUI's main window.  Simply a standard 'container' GUI for one or more applets.
    """


    def __init__( self, workflow = [], parent = None, flags = QtCore.Qt.WindowFlags(0), sideSplitterSizePolicy=SideSplitterSizePolicy.Manual ):
        QMainWindow.__init__(self, parent = parent, flags = flags )

        # Register for thunk events (easy UI calls from non-GUI threads)
        self.thunkEventHandler = ThunkEventHandler(self)

        self._sideSplitterSizePolicy = sideSplitterSizePolicy
        
        import inspect, os
        ilastikShellFilePath = os.path.dirname(inspect.getfile(inspect.currentframe()))
        uic.loadUi( ilastikShellFilePath + "/ui/ilastikShell.ui", self )
        self._applets = []
        self.appletBarMapping = {}

        if 'Ubuntu' in platform.platform():
            # Native menus are prettier, but aren't working on Ubuntu at this time (Qt 4.7, Ubuntu 11)
            self.menuBar().setNativeMenuBar(False)

        (self._projectMenu, self._shellActions) = self._createProjectMenu()
        self.menuBar().addMenu(self._projectMenu)

        self.progressDisplayManager = ProgressDisplayManager(self.statusBar)

        for applet in workflow:
            self.addApplet(applet)

        self.appletBar.expanded.connect(self.handleAppleBarItemExpanded)
        self.appletBar.clicked.connect(self.handleAppletBarClick)
        self.appletBar.setVerticalScrollMode( QAbstractItemView.ScrollPerPixel )
        
        # By default, make the splitter control expose a reasonable width of the applet bar
        self.mainSplitter.setSizes([300,1])
        
        self.currentAppletIndex = 0

        self.projectManager = ProjectManager()
        self.currentImageIndex = -1
        self.populatingImageSelectionCombo = False
        self.imageSelectionCombo.currentIndexChanged.connect( self.changeCurrentInputImageIndex )
        
        self.enableWorkflow = False # Global mask applied to all applets
        self._controlCmds = []      # Track the control commands that have been issued by each applet so they can be popped.
        self._disableCounts = []    # Controls for each applet can be disabled by his peers.
                                    # No applet can be enabled unless his disableCount == 0

        
    def _createProjectMenu(self):
        # Create a menu for "General" (non-applet) actions
        menu = QMenu("Project", self)

        shellActions = ShellActions()

        # Menu item: New Project
        shellActions.newProjectAction = menu.addAction("&New Project...")
        shellActions.newProjectAction.triggered.connect(self.onNewProjectActionTriggered)

        # Menu item: Open Project 
        shellActions.openProjectAction = menu.addAction("&Open Project...")
        shellActions.openProjectAction.triggered.connect(self.onOpenProjectActionTriggered)

        # Menu item: Save Project
        shellActions.saveProjectAction = menu.addAction("&Save Project...")
        shellActions.saveProjectAction.triggered.connect(self.onSaveProjectActionTriggered)
        # Can't save until a project is loaded for the first time
        shellActions.saveProjectAction.setEnabled(False)

        # Menu item: Import Project
        shellActions.importProjectAction = menu.addAction("&Import Project...")
        shellActions.importProjectAction.triggered.connect(self.onImportProjectActionTriggered)

        # Menu item: Quit
        shellActions.quitAction = menu.addAction("&Quit")
        shellActions.quitAction.triggered.connect(self.onQuitActionTriggered)
        shellActions.quitAction.setShortcut( QKeySequence.Quit )
        
        return (menu, shellActions)
    
    def show(self):
        """
        Show the window, and enable/disable controls depending on whether or not a project file present.
        """
        super(IlastikShell, self).show()
        self.enableWorkflow = (self.projectManager.currentProjectFile is not None)
        self.updateAppletControlStates()
        if self._sideSplitterSizePolicy == SideSplitterSizePolicy.Manual:
            self.autoSizeSideSplitter( SideSplitterSizePolicy.AutoLargestDrawer )
        else:
            self.autoSizeSideSplitter( SideSplitterSizePolicy.AutoCurrentDrawer )

    def setImageNameListSlot(self, multiSlot):
        assert type(multiSlot) == MultiOutputSlot
        self.imageNamesSlot = multiSlot
        
        def insertImageName( index, slot ):
            self.imageSelectionCombo.setItemText( index, slot.value )
            if self.currentImageIndex == -1:
                self.changeCurrentInputImageIndex(index)

        def handleImageNameSlotInsertion(multislot, index):
            assert multislot == self.imageNamesSlot
            self.populatingImageSelectionCombo = True
            self.imageSelectionCombo.insertItem(index, "uninitialized")
            self.populatingImageSelectionCombo = False
            multislot[index].notifyDirty( bind( insertImageName, index) )

        multiSlot.notifyInserted( bind(handleImageNameSlotInsertion) )

        def handleImageNameSlotRemoval(multislot, index):
            # Simply remove the combo entry, which causes the currentIndexChanged signal to fire if necessary.
            self.imageSelectionCombo.removeItem(index)
            if len(multislot) == 0:
                self.changeCurrentInputImageIndex(-1)
        multiSlot.notifyRemove( bind(handleImageNameSlotRemoval) )

    def changeCurrentInputImageIndex(self, newImageIndex):
        if newImageIndex != self.currentImageIndex \
        and self.populatingImageSelectionCombo == False:
            if newImageIndex != -1:
                try:
                    # Accessing the image name value will throw if it isn't properly initialized
                    self.imageNamesSlot[newImageIndex].value
                except:
                    # Revert to the original image index.
                    if self.currentImageIndex != -1:
                        self.imageSelectionCombo.setCurrentIndex(self.currentImageIndex)
                    return

            # Alert each central widget and viewer control widget that the image selection changed
            for i in range( len(self._applets) ):
                self._applets[i].gui.setImageIndex(newImageIndex)
                
            self.currentImageIndex = newImageIndex


    def handleAppleBarItemExpanded(self, modelIndex):
        """
        The user wants to view a different applet bar item.
        """
        drawerIndex = modelIndex.row()
        self.setSelectedAppletDrawer(drawerIndex)
    
    def setSelectedAppletDrawer(self, drawerIndex):
        """
        Show the correct applet central widget, viewer control widget, and applet drawer widget for this drawer index.
        """
        if self.currentAppletIndex != drawerIndex:
            self.currentAppletIndex = drawerIndex
            # Collapse all drawers in the applet bar...
            self.appletBar.collapseAll()
            # ...except for the newly selected item.
            self.appletBar.expand( self.getModelIndexFromDrawerIndex(drawerIndex) )
            
            if len(self.appletBarMapping) != 0:
                # Determine which applet this drawer belongs to
                applet_index = self.appletBarMapping[drawerIndex]

                # Select the appropriate central widget, menu widget, and viewer control widget for this applet
                self.appletStack.setCurrentIndex(applet_index)
                self.viewerControlStack.setCurrentIndex(applet_index)
                self.menuBar().clear()
                self.menuBar().addMenu(self._projectMenu)
                for m in self._applets[applet_index].gui.menus():
                    self.menuBar().addMenu(m)
                
                self.autoSizeSideSplitter( self._sideSplitterSizePolicy )

    def getModelIndexFromDrawerIndex(self, drawerIndex):
        drawerTitleItem = self.appletBar.invisibleRootItem().child(drawerIndex)
        return self.appletBar.indexFromItem(drawerTitleItem)
                
    def autoSizeSideSplitter(self, sizePolicy):
        if sizePolicy == SideSplitterSizePolicy.Manual:
            # In manual mode, don't resize the splitter at all.
            return
        
        if sizePolicy == SideSplitterSizePolicy.AutoCurrentDrawer:
            # Get the height of the current applet drawer
            rootItem = self.appletBar.invisibleRootItem()
            appletDrawerItem = rootItem.child(self.currentAppletIndex).child(0)
            appletDrawerWidget = self.appletBar.itemWidget(appletDrawerItem, 0)
            appletDrawerHeight = appletDrawerWidget.frameSize().height()

        if sizePolicy == SideSplitterSizePolicy.AutoLargestDrawer:
            appletDrawerHeight = 0
            # Get the height of the largest drawer in the bar
            for drawerIndex in range( len(self.appletBarMapping) ):
                rootItem = self.appletBar.invisibleRootItem()
                appletDrawerItem = rootItem.child(drawerIndex).child(0)
                appletDrawerWidget = self.appletBar.itemWidget(appletDrawerItem, 0)
                appletDrawerHeight = max( appletDrawerHeight, appletDrawerWidget.frameSize().height() )
        
        # Get total height of the titles in the applet bar (not the widgets)
        firstItem = self.appletBar.invisibleRootItem().child(0)
        titleHeight = self.appletBar.visualItemRect(firstItem).size().height()
        numDrawers = len(self.appletBarMapping)
        totalTitleHeight = numDrawers * titleHeight    
    
        # Auto-size the splitter height based on the height of the applet bar.
        totalSplitterHeight = sum(self.sideSplitter.sizes())
        appletBarHeight = totalTitleHeight + appletDrawerHeight + 10 # Add a small margin so the scroll bar doesn't appear
        self.sideSplitter.setSizes([appletBarHeight, totalSplitterHeight-appletBarHeight])

    def handleAppletBarClick(self, modelIndex):
        # If the user clicks on a top-level item, automatically expand it.
        if modelIndex.parent() == self.appletBar.rootIndex():
            self.appletBar.expand(modelIndex)
        else:
            self.appletBar.setCurrentIndex( modelIndex.parent() )

    def addApplet( self, app ):
        assert isinstance( app, Applet ), "Applets must inherit from Applet base class."
        assert app.base_initialized, "Applets must call Applet.__init__ upon construction."

        assert issubclass( type(app.gui), AppletGuiInterface ), "Applet GUIs must conform to the Applet GUI interface."
        
        self._applets.append(app)
        applet_index = len(self._applets) - 1
        self.appletStack.addWidget( app.gui.centralWidget() )
        
        # Viewer controls are optional. If the applet didn't provide one, create an empty widget for him.
        if app.gui.viewerControlWidget() is None:
            self.viewerControlStack.addWidget( QWidget(parent=self) )
        else:
            self.viewerControlStack.addWidget( app.gui.viewerControlWidget() )

        # Add rows to the applet bar model
        rootItem = self.appletBar.invisibleRootItem()

        # Add all of the applet bar's items to the toolbox widget
        for controlName, controlGuiItem in app.gui.appletDrawers():
            appletNameItem = QTreeWidgetItem( self.appletBar, QtCore.QStringList( controlName ) )
            appletNameItem.setFont( 0, QFont("Ubuntu", 14) )
            drawerItem = QTreeWidgetItem(appletNameItem)
            drawerItem.setSizeHint( 0, controlGuiItem.frameSize() )
#            drawerItem.setBackground( 0, QBrush( QColor(224, 224, 224) ) )
#            drawerItem.setForeground( 0, QBrush( QColor(0,0,0) ) )
            self.appletBar.setItemWidget( drawerItem, 0, controlGuiItem )

            # Since each applet can contribute more than one applet bar item,
            #  we need to keep track of which applet this item is associated with
            self.appletBarMapping[rootItem.childCount()-1] = applet_index

        # Set up handling of GUI commands from this applet
        app.guiControlSignal.connect( bind(self.handleAppletGuiControlSignal, applet_index) )
        self._disableCounts.append(0)
        self._controlCmds.append( [] )

        # Set up handling of progress updates from this applet
        self.progressDisplayManager.addApplet(applet_index, app)
        
        # Set up handling of shell requests from this applet
        app.shellRequestSignal.connect( partial(self.handleShellRequest, applet_index) )

        self.projectManager.addApplet(app)
                
        return applet_index

    def handleAppletGuiControlSignal(self, applet_index, command=ControlCommand.DisableAll):
        """
        Applets fire a signal when they want other applet GUIs to be disabled.
        This function handles the signal.
        Each signal is treated as a command to disable other applets.
        A special command, Pop, undoes the applet's most recent command (i.e. re-enables the applets that were disabled).
        If an applet is disabled twice (e.g. by two different applets), then it won't become enabled again until both commands have been popped.
        """
        if command == ControlCommand.Pop:
            command = self._controlCmds[applet_index].pop()
            step = -1 # Since we're popping this command, we'll subtract from the disable counts
        else:
            step = 1
            self._controlCmds[applet_index].append( command ) # Push command onto the stack so we can pop it off when the applet isn't busy any more

        # Increase the disable count for each applet that is affected by this command.
        for index, count in enumerate(self._disableCounts):
            if (command == ControlCommand.DisableAll) \
            or (command == ControlCommand.DisableDownstream and index > applet_index) \
            or (command == ControlCommand.DisableUpstream and index < applet_index) \
            or (command == ControlCommand.DisableSelf and index == applet_index):
                self._disableCounts[index] += step

        # Update the control states in the GUI thread
        self.thunkEventHandler.post( self.updateAppletControlStates )

    def handleShellRequest(self, applet_index, requestAction):
        """
        An applet is asking us to do something.  Handle the request.
        """
        with Tracer(traceLogger):
            if requestAction == ShellRequest.RequestSave:
                # Call the handler directly to ensure this is a synchronous call (not queued to the GUI thread)
                self.onSaveProjectActionTriggered()

    def __len__( self ):
        return self.appletBar.count()

    def __getitem__( self, index ):
        return self._applets[index]
    
    def ensureNoCurrentProject(self):
        closeProject = True
        if self.projectManager.isProjectDataDirty():
            message = "Your current project is about to be closed, but it has unsaved changes which will be lost.\n"
            message += "Are you sure you want to proceed?"
            buttons = QMessageBox.Yes | QMessageBox.Cancel
            response = QMessageBox.warning(self, "Discard unsaved changes?", message, buttons, defaultButton=QMessageBox.Cancel)
            closeProject = (response == QMessageBox.Yes)

        if closeProject:
            self.closeCurrentProject()

        return closeProject

    def closeCurrentProject(self):
        for applet in self._applets:
            applet.gui.reset()
        self.projectManager.closeCurrentProject()
        self.enableWorkflow = False
        self.updateAppletControlStates()
    
    def onNewProjectActionTriggered(self):
        logger.debug("New Project action triggered")
        
        # Make sure the user is finished with the currently open project
        if not self.ensureNoCurrentProject():
            return
        
        h5File, projectFilePath = self.attemptCreateBlankProjectFile()
        
        if h5File is not None:
            self.loadProject(h5File, projectFilePath)

    def getProjectPathToCreate(self):
        """
        Ask the user where he would like to create a project file.
        """
        logger.debug("Creating blank project file")
        
        fileSelected = False
        while not fileSelected:
            projectFilePath = QFileDialog.getSaveFileName(
               self, "Create Ilastik Project", os.path.abspath(__file__), "Ilastik project files (*.ilp)")
            
            # If the user cancelled, stop now
            if projectFilePath.isNull():
                return None
    
            projectFilePath = str(projectFilePath)
            fileSelected = True
            
            # Add extension if necessary
            fileExtension = os.path.splitext(projectFilePath)[1].lower()
            if fileExtension != '.ilp':
                projectFilePath += ".ilp"
                if os.path.exists(projectFilePath):
                    # Since we changed the file path, we need to re-check if we're overwriting an existing file.
                    message = "A file named '" + projectFilePath + "' already exists in this location.\n"
                    message += "Are you sure you want to overwrite it with a blank project?"
                    buttons = QMessageBox.Yes | QMessageBox.Cancel
                    response = QMessageBox.warning(self, "Overwrite existing project?", message, buttons, defaultButton=QMessageBox.Cancel)
                    if response == QMessageBox.Cancel:
                        # Try again...
                        fileSelected = False

        return projectFilePath
    
    def onImportProjectActionTriggered(self):
        """
        Import an existing project into a new file.
        This involves opening the old file, saving it to a new file, and then opening the new file.
        """
        logger.debug("Import Project Action")

        if not self.ensureNoCurrentProject():
            return

        # Select the paths to the ilp to import and the name of the new one we'll create
        importedFilePath = self.getProjectPathToOpen()
        newProjectFilePath = self.getProjectPathToCreate()

        # If the user didn't cancel
        if importedFilePath is not None and newProjectFilePath is not None:
            newProjectFile = self.projectManager.createBlankProjectFile(newProjectFilePath)
            self.projectManager.importProject(importedFilePath, newProjectFile, newProjectFilePath)

        # Enable all the applet controls
        self.enableWorkflow = True
        self.updateAppletControlStates()
        
    def getProjectPathToOpen(self):
        """
        Return the path of the project the user wants to open (or None if he cancels).
        """
        projectFilePath = QFileDialog.getOpenFileName(
           self, "Open Ilastik Project", os.path.abspath(__file__), "Ilastik project files (*.ilp)")

        # If the user canceled, stop now        
        if projectFilePath.isNull():
            return None

        return str(projectFilePath)

    def onOpenProjectActionTriggered(self):
        logger.debug("Open Project action triggered")
        
        # Make sure the user is finished with the currently open project
        if not self.ensureNoCurrentProject():
            return

        projectFilePath = self.getProjectPathToOpen()
        if projectFilePath is not None:
            try:
                hdf5File = h5py.File(projectFilePath)
            except:
                QMessageBox.error(self, "Unable to open project file: " + projectFilePath)
                return
            
            try:
                self.loadProject(hdf5File, projectFilePath)
            except ProjectManager.ProjectVersionError,e:
                QMessageBox.error(self, "Could not open old project file: " + projectFilePath + ".\nPlease try 'Import Project' instead.")
                return
    
    def loadProject(self, hdf5File, projectFilePath):
        """
        Load the data from the given hdf5File (which should already be open).
        """
        self.projectManager.loadProject(hdf5File, projectFilePath)

        # Now that a project is loaded, the user is allowed to save
        self._shellActions.saveProjectAction.setEnabled(True)

        # Enable all the applet controls
        self.enableWorkflow = True
        self.updateAppletControlStates()
    
    def onSaveProjectActionTriggered(self):
        logger.debug("Save Project action triggered")
        self.projectManager.saveProject()
            
    def onQuitActionTriggered(self, force=False):
        """
        The user wants to quit the application.
        Check his project for unsaved data and ask if he really means it.
        """
        logger.info("Quit Action Triggered")
        
        if not force and self.projectManager.isProjectDataDirty():
            message = "Your project has unsaved data.  Are you sure you want to discard your changes and quit?"
            buttons = QMessageBox.Discard | QMessageBox.Cancel
            response = QMessageBox.warning(self, "Discard unsaved changes?", message, buttons, defaultButton=QMessageBox.Cancel)
            if response == QMessageBox.Cancel:
                return

        self.projectManager.closeCurrentProject()

        # Stop the thread that checks for log config changes.
        ilastik.ilastik_logging.stopUpdates()

        qApp.quit()

    
    def updateAppletControlStates(self):
        """
        Enable or disable all controls of all applets according to their disable count.
        """
        drawerIndex = 0
        for index, applet in enumerate(self._applets):
            enabled = self._disableCounts[index] == 0

            # Apply to the applet central widget
            applet.gui.centralWidget().setEnabled( enabled and self.enableWorkflow )
            
            # Apply to the applet bar drawers
            for appletName, appletGui in applet.gui.appletDrawers():
                appletGui.setEnabled( enabled and self.enableWorkflow )
            
                # Apply to the applet bar drawer headings, too
                drawerTitleItem = self.appletBar.invisibleRootItem().child(drawerIndex)
                if enabled and self.enableWorkflow:
                    drawerTitleItem.setFlags( QtCore.Qt.ItemIsEnabled )
                else:
                    drawerTitleItem.setFlags( QtCore.Qt.NoItemFlags )
                
                drawerIndex += 1


#    def scrollToTop(self):
#        #self.appletBar.verticalScrollBar().setValue( 0 )
#
#        self.appletBar.setVerticalScrollMode( QAbstractItemView.ScrollPerPixel )
#        
#        from PyQt4.QtCore import QPropertyAnimation, QVariant
#        animation = QPropertyAnimation( self.appletBar.verticalScrollBar(), "value", self )
#        animation.setDuration(2000)
#        #animation.setStartValue( QVariant( self.appletBar.verticalScrollBar().minimum() ) )
#        animation.setEndValue( QVariant( self.appletBar.verticalScrollBar().maximum() ) )
#        animation.start()
#
#        #self.appletBar.setVerticalScrollMode( QAbstractItemView.ScrollPerItem )

#
# Simple standalone test for the IlastikShell
#
if __name__ == "__main__":
    #make the program quit on Ctrl+C
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    from PyQt4.QtGui import QApplication
    import sys
    from applet import Applet

    qapp = QApplication(sys.argv)
    
    # Create some simple applets to load
    defaultApplet = Applet()
    trackingApplet = Applet("Tracking")

    # Normally applets would provide their own menu items,
    # but for this test we'll add them here (i.e. from the outside).
    defaultApplet._menuWidget = QMenuBar()
    defaultApplet._menuWidget.setNativeMenuBar( False ) # Native menus are broken on Ubuntu at the moment
    defaultMenu = QMenu("Default Applet", defaultApplet._menuWidget)
    defaultMenu.addAction("Default Action 1")
    defaultMenu.addAction("Default Action 2")
    defaultApplet._menuWidget.addMenu(defaultMenu)
    
    trackingApplet._menuWidget = QMenuBar()
    trackingApplet._menuWidget.setNativeMenuBar( False ) # Native menus are broken on Ubuntu at the moment
    trackingMenu = QMenu("Tracking Applet", trackingApplet._menuWidget)
    trackingMenu.addAction("Tracking Options...")
    trackingMenu.addAction("Track...")
    trackingApplet._menuWidget.addMenu(trackingMenu)

    # Create a shell with our test applets    
    shell = IlastikShell( [defaultApplet, trackingApplet] )

    shell.show()
    qapp.exec_()

