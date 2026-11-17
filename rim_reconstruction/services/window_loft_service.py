"""Compatibility errors for a reconstruction route rejected by the user.

No geometry may be generated through these legacy names. Kept only so cached
calls fail explicitly rather than silently taking another modeling route.
"""


def prepare_window_loft(parameters, **kwargs):
    raise ValueError('Disabled: use CadAgent four-surface fitting and intersection; loft preparation is not permitted')


def prepare_window_lofts(parameters, **kwargs):
    raise ValueError('Disabled: layered/loft reconstruction is not permitted')


def build_window_loft_cutter(parameters, **kwargs):
    raise ValueError('Disabled: loft cutters are not permitted; use four fitted surfaces and two caps')
