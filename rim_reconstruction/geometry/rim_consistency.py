"""Detect angle-local rim indentations without changing or convexifying rims.

Outer axial intersections are used only as diagnostic signatures. Geometry
always comes from an entire observed, accepted rim path, never an envelope.
"""
import numpy as np


def _spread_angles(indices, angles):
    return any(min(abs(angles[i]-angles[j]),360-abs(angles[i]-angles[j])) >= 15
               for i in indices for j in indices if i != j)


def assess_rims(records, angles, cut, tolerance, intersections):
    upper=min(float(r["local"][:,1].max()) for r in records)
    upper-=max(upper*1e-4,1e-6)
    if upper <= cut:
        return [{"angle_degrees":a,"status":"needs_review","reason":"没有共同轮辋径向范围"} for a in angles]
    if not _spread_angles(list(range(len(angles))),angles):
        return [{"angle_degrees":a,"status":"needs_review","reason":"轮辋缺少两个分散角度的观测"} for a in angles]
    radii=np.linspace(cut,upper,257)
    values=[]
    for record in records:
        hits=intersections(record["local"],radii)
        values.append([[float(h[0]),float(h[-1])] if len(h)>=2 else [np.nan,np.nan] for h in hits])
    values=np.asarray(values).transpose(0,2,1)
    # Difference from a centered chord cancels axial translation/linear tilt;
    # real shared bead-seat/lip curvature remains in every angular signature.
    half_window=16
    signature=values[:,:,half_window:-half_window]-(values[:,:,:-2*half_window]+values[:,:,2*half_window:])/2
    with np.errstate(invalid="ignore"):
        center=np.nanmedian(signature,axis=0)
        residual=np.abs(signature-center)
        mad=np.nanmedian(residual,axis=0)
    minimum_signature=max(5*tolerance,.005*upper)
    gate=np.maximum(minimum_signature,4*1.4826*mad)
    result=[]
    sample_r=radii[half_window:-half_window]
    for i,angle in enumerate(angles):
        if not np.isfinite(values[i]).all():
            result.append({"angle_degrees":angle,"status":"needs_review","reason":"轮辋共同径向范围内缺少交点"})
            continue
        events=[]
        for side in range(2):
            bad=residual[i,side]>gate[side]
            supported=np.zeros(len(sample_r),dtype=bool)
            for k in np.flatnonzero(bad):
                refs=[j for j in range(len(records)) if j!=i and
                      np.isfinite(signature[j,side,k]) and residual[j,side,k]<=gate[side,k]]
                supported[k]=_spread_angles(refs,angles)
            for start,end in np.flatnonzero(np.diff(np.r_[False,supported,False])).reshape(-1,2):
                if end-start<3:
                    continue
                events.append({"side":side,"radial_interval_mm":[float(sample_r[start]),float(sample_r[end-1])],
                    "maximum_local_deviation_mm":float(residual[i,side,start:end].max()),
                    "supporting_angles":[angles[j] for j in range(len(records)) if j!=i and
                        np.isfinite(signature[j,side,start:end]).all() and
                        np.all(residual[j,side,start:end]<=gate[side,start:end])]})
        result.append({"angle_degrees":angle,"status":"local_outlier" if events else "consistent",
                       "local_events":events,"method":"angular_centered_chord_consensus",
                       "shape_disagreement_rms_mm":float(np.sqrt(np.nanmean(residual[i]**2))),
                       "minimum_detectable_signature_mm":minimum_signature,
                       "signature_half_window_mm":float(radii[half_window]-radii[0])})
    return result
