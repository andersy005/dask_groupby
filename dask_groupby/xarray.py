import itertools
from typing import TYPE_CHECKING, Hashable, Iterable, Optional, Sequence, Tuple, Union

import dask
import numpy as np
import pandas as pd
import xarray as xr

from .aggregations import Aggregation, _atleast_1d
from .core import (
    factorize_,
    groupby_reduce,
    rechunk_for_blockwise,
    rechunk_for_cohorts as rechunk_array_for_cohorts,
    reindex_,
)
from .xrutils import isnull

if TYPE_CHECKING:
    from xarray import DataArray, Dataset, GroupBy, Resample


def _get_input_core_dims(group_names, dim, ds, to_group):
    input_core_dims = [[], []]
    for g in group_names:
        if g in dim:
            continue
        if g in ds.dims:
            input_core_dims[0].extend([g])
        if g in to_group.dims:
            input_core_dims[1].extend([g])
    input_core_dims[0].extend(dim)
    input_core_dims[1].extend(dim)
    return input_core_dims


def _restore_dim_order(result, obj, by):
    def lookup_order(dimension):
        if dimension == by.name and by.ndim == 1:
            (dimension,) = by.dims
        if dimension in obj.dims:
            axis = obj.get_axis_num(dimension)
        else:
            axis = 1e6  # some arbitrarily high value
        return axis

    new_order = sorted(result.dims, key=lookup_order)
    return result.transpose(*new_order)


def xarray_reduce(
    obj: Union["Dataset", "DataArray"],
    *by: Union["DataArray", Iterable[str], Iterable["DataArray"]],
    func: Union[str, Aggregation],
    expected_groups=None,
    isbin: Union[bool, Sequence[bool]] = False,
    dim: Hashable = None,
    split_out: int = 1,
    fill_value=None,
    method: str = "mapreduce",
    backend: str = "numpy",
    keep_attrs: bool = True,
    skipna: Optional[bool] = None,
    min_count: Optional[int] = None,
    **finalize_kwargs,
):
    """GroupBy reduce operations on xarray objects using numpy-groupies

    Parameters
    ----------
    obj : Union["Dataset", "DataArray"]
        Xarray object to reduce
    *by : Union["DataArray", Iterable[str], Iterable["DataArray"]]
        Variables with which to group by `obj`
    func : Union[str, Aggregation]
        Reduction method
    expected_groups : Dict[str, Sequence]
        expected group labels corresponding to each `by` variable
    isbin : Iterable[bool]
        If True, corresponding entry in `expected_groups` are bin edges.
        If False, the entry in `expected_groups` is treated as a simple label.
    dim : Hashable
        dimension name along which to reduce. If None, reduces across all
        dimensions of `by`
    split_out : int
        Number of output chunks along grouped dimension in output.
    fill_value :
        Value used for missing groups in the output i.e. when one of the labels
        in `expected_groups` is not actually present in `by`
    method: {"mapreduce", "blockwise", "cohorts"}, optional
        Strategy for reduction. Applies to dask arrays only

          * "mapreduce" : First apply the reduction blockwise on ``array``, then
                          combine a few newighbouring blocks, apply the reduction.
                          Continue until finalizing. Usually, ``func`` will need
                          to be an Aggregation instance for this method to work.
                          Common aggregations are implemented.
          * "blockwise" : Only reduce using blockwise and avoid aggregating blocks
                          together. Useful for resampling-style reductions where group
                          members are always together. The array is rechunked so that
                          chunk boundaries line up with group boundaries
                          i.e. each block contains all members of any group present
                          in that block.
          * "cohorts" : Finds group labels that tend to occur together ("cohorts"),
                        indexes out cohorts and reduces that subset using "mapreduce",
                        repeat for all cohorts. This works well for many time groupings
                        where the group labels repeat at regular intervals like 'hour',
                        'month', dayofyear' etc. Optimize chunking ``array`` for this
                        method by first rechunking using ``rechunk_for_cohorts``.

    backend: {"numpy", "numba"}, optional
        Backend for numpy_groupies
    keep_attrs: bool, optional
        Preserve attrs?
    skipna: bool, optional
        If True, skip missing values (as marked by NaN). By default, only
        skips missing values for float dtypes; other dtypes either do not
        have a sentinel missing value (int) or ``skipna=True`` has not been
        implemented (object, datetime64 or timedelta64).
    min_count : int, default: None
        The required number of valid values to perform the operation. If
        fewer than min_count non-NA values are present the result will be
        NA. Only used if skipna is set to True or defaults to True for the
        array's dtype.
    finalize_kwargs: dict, optional
        kwargs passed to the finalize function, like ddof for var, std.

    Raises
    ------
    NotImplementedError
    ValueError

    Examples
    --------
    FIXME: Add docs.
    """

    for b in by:
        if isinstance(b, xr.DataArray) and b.name is None:
            raise ValueError("Cannot group by unnamed DataArrays.")

    # eventually  drop the variables we are grouping by
    maybe_drop = [b for b in by if isinstance(b, str)]
    unindexed_dims = tuple(
        b for b in by if isinstance(b, str) and b in obj.dims and b not in obj.indexes
    )

    by: Tuple["DataArray"] = tuple(obj[g] if isinstance(g, str) else g for g in by)  # type: ignore

    if len(by) > 1 and any(dask.is_dask_collection(by_) for by_ in by):
        raise NotImplementedError("Grouping by multiple variables will compute dask variables.")

    if isinstance(isbin, bool):
        isbin = (isbin,) * len(by)

    grouper_dims = set(itertools.chain(*tuple(g.dims for g in by)))

    if isinstance(obj, xr.DataArray):
        ds = obj._to_temp_dataset()
    else:
        ds = obj

    ds = ds.drop_vars([var for var in maybe_drop if var in ds.variables])
    if dim is Ellipsis:
        dim = tuple(obj.dims)
        if by[0].name in ds.dims:
            dim = tuple(d for d in dim if d != by[0].name)
        dim = tuple(dim)

    # TODO: do this for specific reductions only
    bad_dtypes = tuple(
        k for k in ds.variables if k not in ds.dims and ds[k].dtype.kind in ("S", "U")
    )

    # broadcast all variables against each other along all dimensions in `by` variables
    # don't exclude `dim` because it need not be a dimension in any of the `by` variables!
    # in the case where dim is Ellipsis, and by.ndim < obj.ndim
    # then we also broadcast `by` to all `obj.dims`
    # TODO: avoid this broadcasting
    exclude_dims = set(ds.dims) - grouper_dims
    if dim is not None:
        exclude_dims -= set(dim)
    ds, *by = xr.broadcast(ds, *by, exclude=exclude_dims)

    if dim is None:
        dim = tuple(by[0].dims)
    else:
        dim = _atleast_1d(dim)

    if any(d not in grouper_dims and d not in obj.dims for d in dim):
        raise ValueError(f"cannot reduce over dimensions {dim}")

    dims_not_in_groupers = tuple(d for d in dim if d not in grouper_dims)
    if dims_not_in_groupers == dim:
        # reducing along a dimension along which groups do not vary
        # This is really just a normal reduction.
        if skipna:
            dsfunc = func[3:]
        else:
            dsfunc = func
        result = getattr(ds, dsfunc)(dim=dim)
        if isinstance(obj, xr.DataArray):
            return obj._from_temp_dataset(result)
        else:
            return result

    axis = tuple(range(-len(dim), 0))

    group_names = tuple(g.name for g in by)
    # ds = ds.drop_vars(tuple(g for g in group_names))

    if len(by) > 1:
        group_idx, expected_groups, group_shape, _, _, _ = factorize_(
            tuple(g.data for g in by),
            axis,
            expected_groups,
            isbin,
        )
        to_group = xr.DataArray(group_idx, dims=dim, coords={d: by[0][d] for d in by[0].indexes})
    else:
        if expected_groups is None and isinstance(by[0].data, np.ndarray):
            expected_groups = (np.unique(by[0].data),)
        if expected_groups is None:
            raise NotImplementedError(
                "Please provide expected_groups if not grouping by a numpy-backed DataArray"
            )
        if isinstance(expected_groups, np.ndarray):
            expected_groups = (expected_groups,)
        if isbin[0]:
            group_shape = (len(expected_groups[0]) - 1,)
        else:
            group_shape = (len(expected_groups[0]),)
        to_group = by[0]

    group_sizes = dict(zip(group_names, group_shape))

    def wrapper(*args, **kwargs):
        result, groups = groupby_reduce(*args, **kwargs)
        if len(by) > 1:
            # all groups need not be present. reindex here
            # TODO: add test
            reindexed = reindex_(
                result,
                from_=groups,
                to=np.arange(np.prod(group_shape)),
                fill_value=fill_value,
                axis=-1,
            )
            result = reindexed.reshape(result.shape[:-1] + group_shape)
        else:
            # TODO: migrate this to core.groupby_reduce
            # index out NaN or NaT groups; these should be last
            if np.any(isnull(groups)):
                result = result[..., :-1]
                groups = groups[:-1]

        return result

    # These data variables do not have any of the core dimension,
    # take them out to prevent errors.
    # apply_ufunc can handle non-dim coordinate variables without core dimensions
    missing_dim = {}
    if isinstance(obj, xr.Dataset):
        # broadcasting means the group dim gets added to ds, so we check the original obj
        for k, v in obj.data_vars.items():
            if k in bad_dtypes:
                continue
            is_missing_dim = not (any(d in v.dims for d in dim))
            if is_missing_dim:
                missing_dim[k] = v

    actual = xr.apply_ufunc(
        wrapper,
        ds.drop_vars(tuple(missing_dim) + bad_dtypes),
        to_group,
        input_core_dims=_get_input_core_dims(group_names, dim, ds, to_group),
        # for xarray's test_groupby_duplicate_coordinate_labels
        exclude_dims=set(dim),
        output_core_dims=[group_names],
        dask="allowed",
        dask_gufunc_kwargs=dict(output_sizes=group_sizes),
        keep_attrs=keep_attrs,
        kwargs={
            "func": func,
            "axis": axis,
            "split_out": split_out,
            "fill_value": fill_value,
            "method": method,
            "min_count": min_count,
            "skipna": skipna,
            "backend": backend,
            # The following mess exists becuase for multiple `by`s I factorize eagerly
            # here before passing it on; this means I have to handle the
            # "binning by single by variable" case explicitly where the factorization
            # happens later allowing `by` to  be a dask variable.
            # Another annoyance is that for resampling expected_groups is "disconnected"
            # from "by" so we need the isbin part of the condition
            "expected_groups": expected_groups[0] if len(by) == 1 and isbin[0] else None,
            "isbin": isbin[0] if len(by) == 1 else False,
            "finalize_kwargs": finalize_kwargs,
        },
    )

    for name, expect, isbin_ in zip(group_names, expected_groups, isbin):
        if isbin_:
            expect = [pd.Interval(left, right) for left, right in zip(expect[:-1], expect[1:])]
        if isinstance(actual, xr.Dataset) and name in actual:
            actual = actual.drop_vars(name)
        actual[name] = expect

    # if grouping by multi-indexed variable, then restore it
    for name, index in ds.indexes.items():
        if name in actual.indexes and isinstance(index, pd.MultiIndex):
            actual[name] = index

    if unindexed_dims:
        actual = actual.drop_vars(unindexed_dims)

    if len(by) == 1:
        for var in actual:
            if isinstance(obj, xr.DataArray):
                template = obj
            else:
                template = obj[var]
            actual[var] = _restore_dim_order(actual[var], template, by[0])

    if missing_dim:
        for k, v in missing_dim.items():
            missing_group_dims = {
                dim: size for dim, size in group_sizes.items() if dim not in v.dims
            }
            # The expand_dims is for backward compat with xarray's questionable behaviour
            if missing_group_dims:
                actual[k] = v.expand_dims(missing_group_dims)
            else:
                actual[k] = v

    if isinstance(obj, xr.DataArray):
        return obj._from_temp_dataset(actual)
    else:
        return actual


def xarray_groupby_reduce(
    groupby: "GroupBy",
    func: Union[str, Aggregation],
    split_out: int = 1,
    method: str = "mapreduce",
    keep_attrs: bool = True,
):
    """Apply on an existing Xarray groupby object for convenience."""

    def wrapper(*args, **kwargs):
        result, _ = groupby_reduce(*args, **kwargs)
        return result

    groups = list(groupby.groups.keys())
    outdim = groupby._unique_coord.name
    groupdim = groupby._group_dim

    actual = xr.apply_ufunc(
        wrapper,
        groupby._obj,
        groupby._group,
        input_core_dims=[[groupdim], [groupdim]],
        # for xarray's test_groupby_duplicate_coordinate_labels
        exclude_dims=set(groupdim),
        output_core_dims=[[outdim]],
        dask="allowed",
        dask_gufunc_kwargs=dict(output_sizes={outdim: len(groups)}),
        keep_attrs=keep_attrs,
        kwargs={
            "func": func,
            "axis": -1,
            "split_out": split_out,
            "expected_groups": groups,
            "method": method,
        },
    )
    actual[outdim] = groups

    return actual


def rechunk_for_cohorts(
    obj: Union["DataArray", "Dataset"],
    dim: str,
    labels: "DataArray",
    force_new_chunk_at,
    chunksize: Optional[int] = None,
):
    """
    Rechunks array so that each new chunk contains groups that always occur together.

    Parameters
    ----------
    array: DataArray or Dataset
        array to rechunk
    dim: str
        Dimension to rechunk
    labels: DataArray
        1D Group labels to align chunks with. This routine works
        well when ``labels`` has repeating patterns: e.g.
        ``1, 2, 3, 1, 2, 3, 4, 1, 2, 3`` though there is no requirement
        that the pattern must contain sequences.
    force_new_chunk_at:
        label at which we always start a new chunk. For
        the example ``labels`` array, this would be `1``.
    chunksize: int, optional
        nominal chunk size. Chunk size is exceded when the label
        in ``force_new_chunk_at`` is less than ``chunksize//2`` elements away.
        If None, uses median chunksize along ``dim``.
    Returns
    -------
    dask.array.Array
        rechunked array
    """
    return _rechunk(
        rechunk_array_for_cohorts,
        obj,
        dim,
        labels,
        force_new_chunk_at=force_new_chunk_at,
        chunksize=chunksize,
    )


def rechunk_to_group_boundaries(obj: Union["DataArray", "Dataset"], dim: str, labels: "DataArray"):
    """
    Rechunks array so that group boundaries line up with chunk boundaries, allowing
    parallel group reductions.

    This only works when the groups are sequential (e.g. labels = [0,0,0,1,1,1,1,2,2]).
    Such patterns occur when using ``.resample``.
    """

    return _rechunk(rechunk_for_blockwise, obj, dim, labels)


def _rechunk(func, obj, dim, labels, **kwargs):
    """Common logic for rechunking xarray objects."""
    obj = obj.copy(deep=True)

    if isinstance(obj, xr.Dataset):
        for var in obj:
            if obj[var].chunks is not None:
                obj[var] = obj[var].copy(
                    data=func(
                        obj[var].data, axis=obj[var].get_axis_num(dim), labels=labels.data, **kwargs
                    )
                )
    else:
        if obj.chunks is not None:
            obj = obj.copy(
                data=func(obj.data, axis=obj.get_axis_num(dim), labels=labels.data, **kwargs)
            )

    return obj


def resample_reduce(
    resampler: "Resample",
    func: Union[str, Aggregation],
    keep_attrs: bool = True,
):

    obj = resampler._obj
    dim = resampler._group_dim

    # this creates a label DataArray since resample doesn't do that somehow
    tostack = []
    for idx, slicer in enumerate(resampler._group_indices):
        if slicer.stop is None:
            stop = resampler._obj.sizes[dim]
        else:
            stop = slicer.stop
        tostack.append(idx * np.ones((stop - slicer.start,), dtype=np.int32))
    by = xr.DataArray(np.hstack(tostack), dims=(dim,), name="__resample_dim__")

    result = (
        xarray_reduce(
            obj,
            by,
            func=func,
            method="blockwise",
            expected_groups=(resampler._unique_coord.data,),
            keep_attrs=keep_attrs,
        )
        .rename({"__resample_dim__": dim})
        .transpose(dim, ...)
    )
    return result
