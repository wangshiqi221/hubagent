import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rim_reconstruction.cad.view_utils import show_objects


class ReconstructionViewTests(unittest.TestCase):
    def test_fits_camera_and_keeps_object_placement(self):
        app=SimpleNamespace(GuiUp=True,setActiveDocument=Mock())
        view=Mock();gui=SimpleNamespace(Selection=Mock(),activeDocument=lambda:SimpleNamespace(activeView=lambda:view))
        placement=object()
        body=SimpleNamespace(Name='RimBody',Placement=placement,ViewObject=SimpleNamespace(Visibility=False),
                             Tip=SimpleNamespace(ViewObject=SimpleNamespace(Visibility=False)))
        cutter=SimpleNamespace(Name='Cutter',ViewObject=SimpleNamespace(Visibility=False))
        with patch.dict(sys.modules,{'FreeCAD':app,'FreeCADGui':gui}):
            self.assertTrue(show_objects(SimpleNamespace(Name='Doc'),[body,cutter],translucent=['Cutter']))
        self.assertIs(body.Placement,placement)
        self.assertTrue(body.ViewObject.Visibility)
        self.assertTrue(body.Tip.ViewObject.Visibility)
        self.assertEqual(cutter.ViewObject.Transparency,65)
        view.viewAxonometric.assert_called_once()
        view.fitAll.assert_called_once()
        view.redraw.assert_called_once()

    def test_headless_does_not_claim_display(self):
        with patch.dict(sys.modules,{'FreeCAD':SimpleNamespace(GuiUp=False)}):
            self.assertFalse(show_objects(SimpleNamespace(Name='Doc'),[]))
