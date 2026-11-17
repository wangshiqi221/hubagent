# CadAgent fitted-surface window reconstruction

Only CadAgent's registered tools and its geometry worker may reconstruct this
model. CADPilot and direct external modeling scripts are not permitted.

## Required construction

1. Inspect the STL, estimate/refine the actual hub axis, and rebuild the base
   through CadAgent's section, profile-fit and revolution tools.
2. Use transverse sections for window analysis and automatic wall-region
   selection. Reject central holes and small pockets as spoke-window templates.
3. Fit each of the actual three or four main wall regions with `rim_fit_surface_patch`.
   Start with 0.5 mm sample fitting tolerance and up to 64 iterations. Only a genuine numerical 0.5 mm failure of the same region authorizes retrying at up to 1.0 mm, using `retry_from_call_directory`. Record which surfaces use the fallback. Keep each surface's provenance.
4. Pass the three or four verified reports to `rim_build_window_cutting_solid`. The worker
   intersects and trims the actual supports with two cap planes. Each side must
   retain its original fitted surface, with C1 continuity in both surface
   parameter directions. A surface may have multiple trimmed regions; these
   must share the same original support, not be independently lofted layers. It checks the original wall samples against the
   corresponding trimmed face, not the volume's interior. The separately
   authorized `boundary_tolerance_mm` may be explicitly set to 1.0 mm; it does
   not change the individual surface fitting tolerances. Both are reported.
5. Cap depths may be specified by `axial_depth_range_mm`, based on actual sample
   coverage and the intended cutting region. The worker must not append prisms,
   loft sections, or substitute layered surfaces to make the result close.
6. Import only cutters certified as `four_fitted_surface_intersection`, validate
   all target instances, create the requested arrays, and use CadAgent's native
   FreeCAD Boolean tool. A failed check is a failure, not a reason to switch to
   an unapproved construction method.

A short tip transition can indicate a triangular opening. The three-wall hypothesis
must be supported by the observed arc lengths; the tool cannot omit an arbitrary
wall. Target validation still checks the full observed boundary, including that
transition, so dropping a fit does not hide a geometric mismatch.

## Disabled routes

The previous multi-section loft tools and added disc-clearance prisms are
explicitly disabled, removed from the advertised tool set, and rejected even
when requested through a cached conversation. Legacy loft cutters cannot be
imported or reused in the current Boolean workflow. Hiding edges does not count
as fixing geometry.

Existing loft-generated FCStd files are retained for traceability only and are
not accepted deliverables under this requirement.

## Verification scope

A closed solid alone is insufficient. The report records three or four fitted side
surfaces, the actual trimmed-face count, two caps, side-surface continuity, and maximum boundary-sample deviation.
The constructor uses up to 64 samples per fitted wall for trimmed-boundary
checks, in addition to the full selected-point fitting check. These are sampled
geometric checks, not continuous whole-model or engineering-strength guarantees.
