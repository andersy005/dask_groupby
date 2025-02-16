import dask
import numpy as np
import pandas as pd
import pytest
import xarray as xr

from dask_groupby.xarray import (
    rechunk_to_group_boundaries,
    resample_reduce,
    xarray_groupby_reduce,
    xarray_reduce,
)

from . import assert_equal, raise_if_dask_computes

dask.config.set(scheduler="sync")

try:
    # Should test against legacy xarray implementation
    xr.set_options(use_numpy_groupies=False)
except ValueError:
    pass


@pytest.mark.parametrize("backend", ["numpy", "numba"])
@pytest.mark.parametrize("min_count", [None, 1, 3])
@pytest.mark.parametrize("add_nan", [True, False])
@pytest.mark.parametrize("skipna", [True, False])
def test_xarray_reduce(skipna, add_nan, min_count, backend):
    arr = np.ones((4, 12))

    if add_nan:
        arr[1, ...] = np.nan
        arr[[0, 2], [3, 4]] = np.nan

    if skipna is False and min_count is not None:
        pytest.skip()

    labels = np.array(["a", "a", "c", "c", "c", "b", "b", "c", "c", "b", "b", "f"])
    labels = np.array(labels)
    labels2 = np.array([1, 2, 2, 1])

    da = xr.DataArray(
        arr, dims=("x", "y"), coords={"labels2": ("x", labels2), "labels": ("y", labels)}
    ).expand_dims(z=4)

    expected = da.groupby("labels").sum(skipna=skipna, min_count=min_count)
    actual = xarray_reduce(
        da, "labels", func="sum", skipna=skipna, min_count=min_count, backend=backend
    )
    assert_equal(expected, actual)

    # test dimension ordering
    # actual = xarray_reduce(
    #    da.transpose("y", ...), "labels", func="sum", skipna=skipna, min_count=min_count
    # )
    # assert_equal(expected, actual)


def test_xarray_groupby_reduce():
    arr = np.ones((4, 12))

    labels = np.array(["a", "a", "c", "c", "c", "b", "b", "c", "c", "b", "b", "f"])
    labels = np.array(labels)
    labels2 = np.array([1, 2, 2, 1])

    da = xr.DataArray(
        arr, dims=("x", "y"), coords={"labels2": ("x", labels2), "labels": ("y", labels)}
    ).expand_dims(z=4)

    grouped = da.groupby("labels")
    expected = grouped.mean()
    actual = xarray_groupby_reduce(grouped, "mean")
    assert_equal(expected, actual)

    actual = xarray_groupby_reduce(da.transpose("y", ...).groupby("labels"), "mean")
    assert_equal(expected, actual)

    # TODO: fails because of stacking
    # grouped = da.groupby("labels2")
    # expected = grouped.mean()
    # actual = xarray_groupby_reduce(grouped, "mean")
    # assert_equal(expected, actual)


def test_xarray_reduce_multiple_groupers():
    arr = np.ones((4, 12))

    labels = np.array(["a", "a", "c", "c", "c", "b", "b", "c", "c", "b", "b", "f"])
    labels = np.array(labels)
    labels2 = np.array([1, 2, 2, 1])

    da = xr.DataArray(
        arr, dims=("x", "y"), coords={"labels2": ("x", labels2), "labels": ("y", labels)}
    ).expand_dims(z=4)

    expected = xr.DataArray(
        [[4, 4], [10, 10], [8, 8], [2, 2]],
        dims=("labels", "labels2"),
        coords={"labels": ["a", "c", "b", "f"], "labels2": [1, 2]},
    ).expand_dims(z=4)

    actual = xarray_reduce(da, da.labels, da.labels2, func="count")
    xr.testing.assert_identical(expected, actual)

    actual = xarray_reduce(da, "labels", da.labels2, func="count")
    xr.testing.assert_identical(expected, actual)

    actual = xarray_reduce(da, "labels", "labels2", func="count")
    xr.testing.assert_identical(expected, actual)

    with raise_if_dask_computes():
        actual = xarray_reduce(da.chunk({"x": 2, "z": 1}), da.labels, da.labels2, func="count")
    xr.testing.assert_identical(expected, actual)

    with pytest.raises(NotImplementedError):
        actual = xarray_reduce(da.chunk({"x": 2, "z": 1}), "labels", "labels2", func="count")
    # xr.testing.assert_identical(expected, actual)


def test_xarray_reduce_single_grouper():

    # DataArray
    ds = xr.tutorial.open_dataset("rasm", chunks={"time": 9})
    actual = xarray_reduce(ds.Tair, ds.time.dt.month, func="mean")
    expected = ds.Tair.groupby("time.month").mean()
    xr.testing.assert_allclose(actual, expected)

    # Ellipsis reduction
    actual = xarray_reduce(ds.Tair, ds.time.dt.month, func="mean", dim=...)
    expected = ds.Tair.groupby("time.month").mean(...)
    xr.testing.assert_allclose(actual, expected)

    # Dataset
    expected = ds.groupby("time.month").mean()
    actual = xarray_reduce(ds, ds.time.dt.month, func="mean")
    xr.testing.assert_allclose(actual, expected)

    # add data var with missing grouper dim
    ds["foo"] = ("bar", [1, 2, 3])
    expected = ds.groupby("time.month").mean()
    actual = xarray_reduce(ds, ds.time.dt.month, func="mean")
    xr.testing.assert_allclose(actual, expected)
    del ds["foo"]

    # non-dim coord with missing grouper dim
    ds.coords["foo"] = ("bar", [1, 2, 3])
    expected = ds.groupby("time.month").mean()
    actual = xarray_reduce(ds, ds.time.dt.month, func="mean")
    xr.testing.assert_allclose(actual, expected)
    del ds["foo"]

    # unindexed dim
    by = ds.time.dt.month.drop_vars("time")
    ds = ds.drop_vars("time")
    expected = ds.groupby(by).mean()
    actual = xarray_reduce(ds, by, func="mean")
    xr.testing.assert_allclose(actual, expected)


def test_xarray_reduce_errors():

    da = xr.DataArray(np.ones((12,)), dims="x")
    by = xr.DataArray(np.ones((12,)), dims="x")

    with pytest.raises(ValueError, match="group by unnamed"):
        xarray_reduce(da, by, func="mean")

    by.name = "by"
    with pytest.raises(ValueError, match="cannot reduce over"):
        xarray_reduce(da, by, func="mean", dim="foo")

    with pytest.raises(NotImplementedError, match="provide expected_groups"):
        xarray_reduce(da, by.chunk(), func="mean")


@pytest.mark.parametrize("isdask", [True, False])
@pytest.mark.parametrize("dataarray", [True, False])
@pytest.mark.parametrize("chunklen", [27, 4 * 31 + 1, 4 * 31 + 20])
def test_xarray_resample(chunklen, isdask, dataarray):
    ds = xr.tutorial.open_dataset("air_temperature", chunks={"time": chunklen})
    if not isdask:
        ds = ds.compute()

    if dataarray:
        ds = ds.air

    resampler = ds.resample(time="M")
    actual = resample_reduce(resampler, "mean")
    expected = resampler.mean()
    xr.testing.assert_allclose(actual, expected)


def test_xarray_resample_dataset_multiple_arrays():
    # regression test for #35
    times = pd.date_range("2000", periods=5)
    foo = xr.DataArray(range(5), dims=["time"], coords=[times], name="foo")
    bar = xr.DataArray(range(1, 6), dims=["time"], coords=[times], name="bar")
    ds = xr.merge([foo, bar]).chunk({"time": 4})

    resampler = ds.resample(time="4D")
    # The separate computes are necessary here to force xarray
    # to compute all variables in result at the same time.
    expected = resampler.mean().compute()
    result = resample_reduce(resampler, "mean").compute()
    xr.testing.assert_allclose(expected, result)


@pytest.mark.parametrize(
    "inchunks, expected",
    [
        [(1,) * 10, (3, 2, 2, 3)],
        [(2,) * 5, (3, 2, 2, 3)],
        [(3, 3, 3, 1), (3, 2, 5)],
        [(3, 1, 1, 2, 1, 1, 1), (3, 2, 2, 3)],
        [(3, 2, 2, 3), (3, 2, 2, 3)],
        [(4, 4, 2), (3, 4, 3)],
        [(5, 5), (5, 5)],
        [(6, 4), (5, 5)],
        [(7, 3), (7, 3)],
        [(8, 2), (7, 3)],
        [(9, 1), (10,)],
        [(10,), (10,)],
    ],
)
def test_rechunk_to_group_boundaries(inchunks, expected):
    labels = np.array([1, 1, 1, 2, 2, 3, 3, 5, 5, 5])

    da = xr.DataArray(dask.array.ones((10,), chunks=inchunks), dims="x", name="foo")
    rechunked = rechunk_to_group_boundaries(da, "x", xr.DataArray(labels, dims="x"))
    assert rechunked.chunks == (expected,)

    da = xr.DataArray(dask.array.ones((5, 10), chunks=(-1, inchunks)), dims=("y", "x"), name="foo")
    rechunked = rechunk_to_group_boundaries(da, "x", xr.DataArray(labels, dims="x"))
    assert rechunked.chunks == ((5,), expected)

    ds = da.to_dataset()
    rechunked = rechunk_to_group_boundaries(ds, "x", xr.DataArray(labels, dims="x"))
    assert rechunked.foo.chunks == ((5,), expected)


# everything below this is copied from xarray's test_groupby.py
# TODO: chunk these
# TODO: dim=None, dim=Ellipsis, groupby unindexed dim


def test_groupby_duplicate_coordinate_labels():
    # fix for http://stackoverflow.com/questions/38065129
    array = xr.DataArray([1, 2, 3], [("x", [1, 1, 2])])
    expected = xr.DataArray([3, 3], [("x", [1, 2])])
    actual = xarray_reduce(array, array.x, func="sum")
    assert_equal(expected, actual)


def test_multi_index_groupby_sum():
    # regression test for xarray GH873
    ds = xr.Dataset(
        {"foo": (("x", "y", "z"), np.ones((3, 4, 2)))},
        {"x": ["a", "b", "c"], "y": [1, 2, 3, 4]},
    )
    expected = ds.sum("z")
    stacked = ds.stack(space=["x", "y"])
    actual = xarray_reduce(stacked, "space", dim="z", func="sum")
    assert_equal(expected, actual.unstack("space"))


@pytest.mark.parametrize("chunks", (None, 2))
def test_xarray_groupby_bins(chunks):
    array = xr.DataArray([1, 1, 1, 1, 1], dims="x")
    labels = xr.DataArray([1, 1.5, 1.9, 2, 3], dims="x", name="labels")

    if chunks:
        array = array.chunk({"x": chunks})
        labels = labels.chunk({"x": chunks})

    with raise_if_dask_computes():
        actual = xarray_reduce(
            array,
            labels,
            dim="x",
            func="count",
            expected_groups=np.array([1, 2, 4, 5]),
            isbin=True,
            fill_value=0,
        )
    expected = xr.DataArray(
        np.array([3, 2, 0]),
        dims="labels",
        coords={"labels": [pd.Interval(1, 2), pd.Interval(2, 4), pd.Interval(4, 5)]},
    )
    xr.testing.assert_equal(actual, expected)
