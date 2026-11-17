# Measured spoke-window reconstruction

The reconstruction tools use source-global millimetres. Axial depths are signed
distances from the recorded axis origin, not global Z coordinates.

## Workflow

1. Inspect the STL, estimate the axis, process axial sections, analyze and fit
   a complete base profile, then create the native FreeCAD revolution.
2. Analyze transverse sections with `rim_analyze_window_regions`. A spoke
   opening must span a hub-to-rim radial band. Small central holes are rejected
   even when a layer contains no larger openings. Angular phases use polygon
   area centroids, not the average of tessellation vertices.
3. If a PCA axis cannot match an observed periodic group, use
   `rim_refine_axis_symmetry` with its observed instance count. It checks
   held-out mesh samples at multiple rotations. Recompute the profile, blank
   and windows from the returned axis; never mix old and new axes.
4. Four independently fitted wall patches remain available. If their extended
   supports cannot enclose a cutter following the samples, use
   `rim_prepare_window_lofts` and `rim_build_window_loft_cutter`. The latter
   fits periodic closed wires to actual source sections and creates a bounded
   solid loft. The default section-point fitting tolerance is 0.5 mm.
5. Import the loft cutter, then call
   `rim_extend_window_cutter_through_disc` with the actual blank. It derives
   a radial separation from the blank's section and checks an entire annular
   band with native Booleans. Only the inner back-cap region is extended;
   the rear rim must remain outside this clearance region. A missing empty
   separator is an error, not permission to guess a radius.
6. A shared template may be rotated only after all target windows pass
   `rim_validate_window_pattern`. If rigid copies fail, fit individual actual
   windows and group their cutters with `rim_create_window_cutter_set`.
   Each member is checked at its actual position within 0.5 mm and previewed
   with a native single-window cut. The group is a `Part::Compound` of measured
   variants, not identical polar copies.
7. `rim_cut_spoke_windows` creates transactional native `Part::Cut` features.
   The result must be one valid connected solid with reduced volume. The GUI
   hides intermediate objects and fits the view to the final result.

## Verification limits

Section fitting checks every supplied section vertex against the closed wire.
Window matching samples observed section boundaries. Neither is a continuous
whole-mesh Hausdorff bound. Symmetry registration is geometric evidence, not
engineering axis certification. Strength and minimum wall thickness require
separate engineering checks.

For the recorded wheel regression, reconstruction additionally checks axial
probes in all ten openings and compares native Boolean volumes in the protected
rear-rim region before and after cutting. An editable result is saved as FCStd;
STEP is optional and is not required for native FreeCAD Boolean operations.
