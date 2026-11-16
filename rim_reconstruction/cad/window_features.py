"""Persistent FreeCAD window tools. Proxies regenerate shapes on recompute."""
import json


class CutterPattern:
    def __init__(self,obj):
        obj.addProperty('App::PropertyLink','SourceCutter','Pattern')
        obj.addProperty('App::PropertyVector','AxisOrigin','Pattern')
        obj.addProperty('App::PropertyVector','AxisDirection','Pattern')
        obj.addProperty('App::PropertyFloatList','Angles','Pattern')
        obj.Proxy=self

    def execute(self,obj):
        import FreeCAD as App
        import Part
        shapes=[]
        if obj.SourceCutter is None:raise ValueError('Pattern source cutter is missing')
        for angle in obj.Angles:
            shape=obj.SourceCutter.Shape.copy()
            shape.rotate(obj.AxisOrigin,obj.AxisDirection,float(angle));shapes.append(shape)
        if not shapes:raise ValueError('Pattern has no instances')
        obj.Shape=Part.makeCompound(shapes)

    def dumps(self):return None
    def loads(self,state):pass
    def __getstate__(self):return None
    def __setstate__(self,state):pass


def solid(shape):
    return not shape.isNull() and shape.isValid() and len(shape.Solids)==1 and shape.Volume>0
