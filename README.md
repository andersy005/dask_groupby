[![GitHub Workflow CI Status](https://img.shields.io/github/workflow/status/dcherian/dask_groupby/CI?logo=github&style=for-the-badge)](https://github.com/dcherian/dask_groupby/actions)[![GitHub Workflow Code Style Status](https://img.shields.io/github/workflow/status/dcherian/dask_groupby/code-style?label=Code%20Style&style=for-the-badge)](https://github.com/dcherian/dask_groupby/actions)[![image](https://img.shields.io/codecov/c/github/dcherian/dask_groupby.svg?style=for-the-badge)](https://codecov.io/gh/dcherian/dask_groupby)

# dask_groupby

(See a
[presentation](https://docs.google.com/presentation/d/1muj5Yzjw-zY8c6agjyNBd2JspfANadGSDvdd6nae4jg/edit?usp=sharing)
about this package).

## API

There are three functions
1.  `groupby_reduce(dask_array, by_dask_array, "mean")`
    "pure" dask array interface
2.  `xarray_groupby_reduce(groupby_object, "mean")`
    xarray groupby interface that accepts a GroupBy object for convenience
3.  `xarray_reduce(xarray_object, by_dataarray, "mean")`
    "pure" xarray interface

## Implementation

This repo explores strategies for a distributed GroupBy with dask
arrays. It was motivated by

1.  Dask Dataframe GroupBy
    [blogpost](https://blog.dask.org/2019/10/08/df-groupby)
2.  numpy_groupies in Xarray
    [issue](https://github.com/pydata/xarray/issues/4473)

The core GroupBy operation is outsourced to
[numpy_groupies](https://github.com/ml31415/numpy-groupies). The GroupBy
reduction is first applied blockwise. Those intermediate results are
combined by concatenating to form a new array which is then reduced
again. The combining of intermediate results uses dask\'s `_tree_reduce`
till all group results are in one block. At that point the result is
\"finalized\" and returned to the user. Here is an example of writing a
custom Aggregation (again inspired by dask.dataframe)

``` python
    mean = Aggregation(
        # name used for dask tasks
        name="mean",
        # blockwise reduction
        chunk=("sum", "count"),
        # combine intermediate results: sum the sums, sum the counts
        combine=("sum", "sum"),
        # generate final result as sum / count
        finalize=lambda sum_, count: sum_ / count,
        # Used when "reindexing" at combine-time
        fill_value=0,
    )
```

Using `_tree_reduce` complicates the implementation. An
alternative simpler implementation would be to use the "tensordot"
[trick](https://github.com/dask/dask/blob/ac1bd05cfd40207d68f6eb8603178d7ac0ded922/dask/array/routines.py#L295-L310).
But this requires knowledge of "expected group labels" at
compute-time.
