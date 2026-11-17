"""Display source-coordinate geometry without changing its placement."""


def show_objects(doc, objects, *, translucent=()):
    import FreeCAD as App
    if not App.GuiUp:
        return False
    import FreeCADGui as Gui
    App.setActiveDocument(doc.Name)
    for obj in objects:
        obj.ViewObject.Visibility = True
        if getattr(obj, 'Tip', None) is not None:
            obj.Tip.ViewObject.Visibility = True
        if obj.Name in translucent:
            obj.ViewObject.ShapeColor = (0.0, 0.8, 0.9)
            obj.ViewObject.Transparency = 65
    Gui.Selection.clearSelection()
    view = Gui.activeDocument().activeView()
    view.viewAxonometric()
    view.fitAll()
    view.redraw()
    return True
