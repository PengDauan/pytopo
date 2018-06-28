import numpy as np
from warnings import warn

from qcodes.dataset.data_export import get_data_by_id
from qcodes.dataset.plotting import plot_by_id

from pytopo.sweep.base import (
    ParameterSweep, ParameterWrapper, Nest, Chain,
    BaseSweepObject
)

from pytopo.sweep.param_table import ParamTable
from pytopo.sweep.measurement import SweepMeasurement


class _ConvenienceWrapper(BaseSweepObject):
    def __init__(self, sweep_object):
        super().__init__()
        self._sweep_object = sweep_object

    def _generator_factory(self):
        return self._sweep_object._generator_factory()

    def __call__(self, *sweep_objects):
        return Nest(self._sweep_object, Chain(*sweep_objects))


class CallSweepObject(BaseSweepObject):
    def __init__(self, call_function, *args, **kwargs):
        super().__init__()
        self._caller = lambda: call_function(*args, **kwargs)
        self._parameter_table = ParamTable([])

    def _generator_factory(self):
        self._caller()
        yield


def sweep(parameter, set_points):

    if not callable(set_points):
        sweep_object = ParameterSweep(parameter, lambda: set_points)
    else:
        sweep_object = ParameterSweep(parameter, set_points)

    return _ConvenienceWrapper(sweep_object)


def measure(parameter):
    return ParameterWrapper(parameter)


def call(call_function, *args, **kwargs):
    return CallSweepObject(call_function, *args, **kwargs)


class _DataExtractor:
    """
    A convenience class to quickly extract data from a data saver instance
    """
    def __init__(self, datasaver):
        self._run_id = datasaver.run_id
        self._dataset = datasaver.dataset

    def __getitem__(self, layout):

        def is_subset(smaller, larger):
            return smaller == larger[:len(smaller)]

        layout = sorted(layout.split(","))
        all_data = get_data_by_id(self._run_id)
        data_layouts = [sorted([d["name"] for d in ad]) for ad in all_data]

        i = np.array(
            [is_subset(layout, data_layout) for data_layout in data_layouts]
        )

        ind = np.flatnonzero(i)
        if len(ind) == 0:
            raise ValueError(f"No such layout {layout}")

        data = all_data[ind[0]]
        return {d["name"]: d["data"] for d in data}

    def plot(self):
        plot_by_id(self._run_id)

    @property
    def run_id(self):
        return self._run_id


def do_experiment(sweep_object, setup=None, cleanup=None, experiment=None,
                  station=None, live_plot=False):

    def add_actions(action, callables):
        if callables is None:
            return

        for cabble in np.atleast_1d(callables):
            if not isinstance(cabble, tuple):
                cabble = (cabble, ())

            action(*cabble)

    if live_plot:
        try:
            from plottr.qcodes_dataset import QcodesDatasetSubscriber
            from plottr.tools import start_listener

            start_listener()

        except ImportError:
            warn("Cannot perform live plots, plottr not installed")
            live_plot = False

    meas = SweepMeasurement(exp=experiment, station=station)
    meas.register_sweep(sweep_object)

    add_actions(meas.add_before_run, setup)
    add_actions(meas.add_after_run, cleanup)

    with meas.run() as datasaver:

        if live_plot:
            datasaver.dataset.subscribe(
                QcodesDatasetSubscriber(datasaver.dataset),
                state=[], min_wait=0, min_count=1
            )

        for data in sweep_object:
            datasaver.add_result(*data.items())

    return _DataExtractor(datasaver)