"""Robust rotational surface registration; no geometry is created or filled."""
import numpy as np
from scipy.spatial import cKDTree
from scipy.optimize import least_squares
from ..adapters.cpp_worker_adapter import check_cancelled


def refine_symmetry_axis(triangles, axis, instance_count, *, sample_count=8000, cancel_event=None):
    tri=np.asarray(triangles);centers=tri.mean(axis=1)
    normals=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]);weights=np.linalg.norm(normals,axis=1)
    normals/=np.maximum(weights[:,None],1e-30)
    if weights.sum()<=0:raise ValueError('Degenerate mesh')
    origin=np.asarray(axis['origin']);direction=np.asarray(axis['direction']);direction=direction/np.linalg.norm(direction)
    basis=np.eye(3)[np.argmin(abs(direction))];u=np.cross(direction,basis);u/=np.linalg.norm(u);v=np.cross(direction,u)
    tree=cKDTree(centers);rng=np.random.default_rng(412)
    valid=np.flatnonzero(weights>0)
    ids=rng.choice(valid,min(sample_count,len(valid)//2),replace=False,p=weights[valid]/weights[valid].sum())
    remaining=np.setdiff1d(valid,ids)
    test=rng.choice(remaining,min(sample_count,len(remaining)),replace=False,p=weights[remaining]/weights[remaining].sum())
    angles=np.deg2rad(np.arange(1,instance_count)*360/instance_count)
    def move(p,points,angle):
        d=direction+p[0]*u+p[1]*v;d/=np.linalg.norm(d);o=origin+p[2]*u+p[3]*v;x=points-o
        return o+x*np.cos(angle)+np.cross(d,x)*np.sin(angle)+(x@d)[:,None]*d*(1-np.cos(angle))
    def errors(p,points,angle):
        moved=move(p,points,angle);distance,near=tree.query(moved,workers=2)
        return np.abs(np.sum((moved-centers[near])*normals[near],axis=1)),distance
    # Keep independent rotations in the objective; a single adjacent instance can
    # bias the axis towards a local pocket or valve feature.
    def objective(p):
        check_cancelled(cancel_event)
        return np.concatenate([errors(p,centers[ids],angle)[0] for angle in angles])
    radius=axis['outer_radius_mm']
    fit=least_squares(objective,np.zeros(4),bounds=([-.05,-.05,-.05*radius,-.05*radius],[.05,.05,.05*radius,.05*radius]),
                      loss='soft_l1',f_scale=.1,x_scale=[.01,.01,1,1],max_nfev=80,diff_step=1e-4)
    p=fit.x;d=direction+p[0]*u+p[1]*v;d/=np.linalg.norm(d);o=origin+p[2]*u+p[3]*v
    checks=[]
    for angle in angles:
        before,_=errors(np.zeros(4),centers[test],angle);after,distance=errors(p,centers[test],angle)
        checks.append({'angle_degrees':float(np.rad2deg(angle)),
            'before_p95_mm':float(np.percentile(before,95)),'after_p95_mm':float(np.percentile(after,95)),
            'after_median_mm':float(np.median(after)),'euclidean_p95_mm':float(np.percentile(distance,95))})
    improved=all(c['after_p95_mm']<c['before_p95_mm'] for c in checks)
    if not fit.success or not improved:raise ValueError('Symmetry refinement did not improve all held-out rotation checks')
    radial=tri.reshape(-1,3)-o;depth=radial@d
    outer=float(np.linalg.norm(radial-depth[:,None]*d,axis=1).max())
    return {'origin':o.tolist(),'direction':d.tolist(),'outer_radius_mm':outer,'confirmed':False,
            'method':'robust multi-angle point-to-plane symmetry registration'}, {
            'instance_count':instance_count,'fit_sample_count':len(ids),'validation_sample_count':len(test),
            'checks':checks,'axis_change_degrees':float(np.rad2deg(np.arccos(np.clip(d@direction,-1,1)))),
            'scope':'held-out surface samples; not engineering axis certification'}
