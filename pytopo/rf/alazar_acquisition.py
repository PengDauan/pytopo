import time
import numpy as np
import qcodes as qc

from qcodes.instrument_drivers.AlazarTech.ATS import AcquisitionController

class BaseAcqCtl(AcquisitionController):
    """
    The baseclass for all the controllers in this file. Implements the basic
    getting of data but does not implement any of the data shaping,
    demodulation or averaging.
    """
    MINSAMPLES = 384

    def __init__(self, name, alazar_name, **kwargs):
        self.acquisitionkwargs = {}
        self.number_of_channels = 2
        self.trigger_func = None
        self._average_buffers = False
        self._nbits = 12
        self._model = 'ATS9360'
        self._buffer_order = 'brsc'
        
        self.do_allocate_data = True
        self.data = None
        self.tvals = None

        super().__init__(name, alazar_name, **kwargs)

        if self._alazar is not None:
            alz = self._get_alazar()
            self.add_parameter('sample_rate', get_cmd=alz.sample_rate)
            self.add_parameter('samples_per_record', get_cmd=alz.samples_per_record)
            self.add_parameter('records_per_buffer', get_cmd=alz.records_per_buffer)
            self.add_parameter('buffers_per_acquisition', get_cmd=alz.buffers_per_acquisition)

            self.add_parameter('acq_time', get_cmd=None, set_cmd=None, unit='s', initial_value=None)
            self.add_parameter("acquisition", get_cmd=self.do_acquisition, snapshot_value=False)

            _idn = alz.IDN()
            self._nbits = _idn['bits_per_sample']
            self._model = _idn['model']
            if self._model == 'ATS9870':
                self._buffer_order = 'bcrs'

        else:
            self.add_parameter('sample_rate', set_cmd=None)
            self.add_parameter('samples_per_record', set_cmd=None)
            self.add_parameter('records_per_buffer', set_cmd=None)
            self.add_parameter('buffers_per_acquisition', set_cmd=None)

        if self._nbits == 8:
            self._datadtype = np.uint8
        elif self._nbits == 12:
            self._datadtype = np.uint16
        else:
            raise ValueError('Unsupported number of bits per samples:', self._nbits)


    def data_shape(self):
        """
        Implement this method to return the shape of this data produced
        by a a given subclass of this controller.
        Should be returned as a tuple of ints.
        """
        raise NotImplementedError

    def data_dims(self):
        """
        Implement this method to return the names of the dimensions
        of this data produced by a a given subclass of this controller.
        Should be returned as a tuple of strings.
        """
        raise NotImplementedError

    def process_buffer(self, buf):
        """
        Implement this method to perform averaging specific for this controller.
        This does not include averaging over buffers as this is performed directly
        in handle_buffer.
        """
        raise NotImplementedError

    def time2samples(self, t):
        alazar = self._get_alazar()
        nsamples_ideal = t * alazar.sample_rate()
        nsamples = int(nsamples_ideal // 128 * 128)
        if nsamples / alazar.sample_rate() < t:
            nsamples += 128
        return max(self.MINSAMPLES, nsamples)

    def allocate_data(self):
        alazar = self._get_alazar()
        self.tvals = np.arange(self.samples_per_record(), dtype=np.float32) / alazar.sample_rate()
        self.data = np.zeros(self.data_shape(), dtype=self._datadtype)

    def pre_start_capture(self):
        if self._buffer_order == 'brsc':
            self.buffer_shape = (self.records_per_buffer(),
                                 self.samples_per_record(),
                                 self.number_of_channels)
        elif self._buffer_order == 'bcrs':
            self.buffer_shape = (self.number_of_channels,
                                 self.records_per_buffer(),
                                 self.samples_per_record(),)
        else:
            raise ValueError('Unknown buffer order {}'.format(self._buffer_order))

        if self.do_allocate_data:
            self.allocate_data()

        self.handling_times = np.zeros(self.buffers_per_acquisition(), dtype=np.float64)

    def pre_acquire(self):
        if self.trigger_func:
            self.trigger_func(True)

    def post_acquire(self):
        if self.trigger_func:
            self.trigger_func(False)

        return self.data

    def handle_buffer(self, data, buffer_number=None):
        t0 = time.perf_counter()
        data.shape = self.buffer_shape
        if self._buffer_order == 'bcrs':
            data = data.transpose((1,2,0))

        if buffer_number is None or self._average_buffers:
            self.data += self.process_buffer(data)
            self.handling_times[0] = (time.perf_counter() - t0) * 1e3
        else:
            self.data[buffer_number] = self.process_buffer(data)
            self.handling_times[buffer_number] = (time.perf_counter() - t0) * 1e3


    def update_acquisitionkwargs(self, **kwargs):
        if self.acq_time() and 'samples_per_record' not in kwargs:
            kwargs['samples_per_record'] = self.time2samples(self.acq_time())
        self.acquisitionkwargs.update(**kwargs)


    def do_acquisition(self):
        if self._alazar is not None:
            value = self._get_alazar().acquire(acquisition_controller=self, **self.acquisitionkwargs)
        else:
            value = None
        return value


class RawAcqCtl(BaseAcqCtl):
    """
    A controller that returns the data as received from the Alazar card in
    a 4 dimensional array. Buffers x Records x Samples X Channels. No postprocessing
    is performed.
    """
    def data_shape(self):
        """
        Shape of the data that this controller will produce

        Returns:
            A tuple of the sizes of the data dimensions.
        """
        shp = (self.buffers_per_acquisition(),
               self.records_per_buffer(),
               self.samples_per_record(),
               self.number_of_channels)

        if not self._average_buffers:
            return shp
        else:
            return shp[1:]

    def data_dims(self):
        """
        Dimensions of the data produced

        Returns:
             A tuple of the names of dimensions of the data returned
        """
        dims = ('buffers', 'records', 'samples', 'channels')

        if not self._average_buffers:
            return dims
        else:
            return dims[1:]

    def process_buffer(self, buf):
        """
        Return data as is without any averaging.
        """
        return buf

    def post_acquire(self):
        data = super().post_acquire()
        if self._nbits == 12:
            data = np.right_shift(self.data, 4)

        return (data.astype(np.float32) / (2**self._nbits)) - 0.5


class DemodCtl(BaseAcqCtl):
    """
    A controller that demodulates the data from the Alazar.
    Returns buffers x records x demod_samples x channels.
    """

    def __init__(self, *arg, **kw):
        super().__init__(*arg, **kw)
        self.add_parameter('demod_frq', set_cmd=None, unit='Hz')

    def data_shape(self):
        """
        Shape of the data that this controller will produce

        Returns:
            A tuple of the sizes of the data dimensions.
        """
        self.period = int(self.sample_rate() / self.demod_frq() + 0.5)
        self.demod_samples = self.samples_per_record() // self.period
        self.demod_tvals = self.tvals[::self.period][:self.demod_samples]
        self.cosarr = (np.cos(2*np.pi*self.demod_frq()*self.tvals).reshape(1,1,-1,1))
        self.sinarr = (np.sin(2*np.pi*self.demod_frq()*self.tvals).reshape(1,1,-1,1))

        shp = (
            self.buffers_per_acquisition(), 
            self.records_per_buffer(), 
            self.demod_samples,
            self.number_of_channels
            )

        return shp

    def pre_start_capture(self):
        super().pre_start_capture()
        shp = (
            self.buffers_per_acquisition(), 
            self.records_per_buffer(), 
            self.samples_per_record(),
            self.number_of_channels
        )
        self.data = np.zeros(shp, dtype=self._datadtype)

    
    def data_dims(self):
        """
        Dimensions of the data produced

        Returns:
             A tuple of the names of dimensions of the data returned
        """
        dims = ('buffers', 'records', 'IF_periods', 'channels')
        return dims

    
    def process_buffer(self, buf):
        """
        Return data as is without any averaging.
        """
        return buf

    def post_acquire(self):
        data = super().post_acquire()
        if self._nbits == 12:
            data = np.right_shift(data, 4)
        data = (data.astype(np.float32) / (2**self._nbits)) - 0.5

        real = (data * 2 * self.cosarr)[:,:,:self.demod_samples*self.period,:].reshape(
            self.buffers_per_acquisition(), -1, 
            self.demod_samples, self.period, self.number_of_channels).mean(axis=-2)
        imag = (data * 2 * self.sinarr)[:,:,:self.demod_samples*self.period,:].reshape(
            self.buffers_per_acquisition(), -1, 
            self.demod_samples, self.period, self.number_of_channels).mean(axis=-2)
        
        return real + 1j * imag



class AvgBufCtl(BaseAcqCtl):
    """
    A controller that averages over buffers. The data returned has the shape
    of Records x Samples x Channels
    """
    def __init__(self, *arg, **kw):
        super().__init__(*arg, **kw)
        self._average_buffers = True

        if self._nbits == 8:
            self._datadtype = np.uint16
        elif self._nbits == 12:
            self._datadtype = np.uint32


    def data_shape(self):
        """
        Shape of the data that this controller will produce.

        Returns:
            A tuple of the sizes of the data dimensions.
        """
        shp = (self.records_per_buffer(),
               self.samples_per_record(),
               self.number_of_channels)
        return shp

    def data_dims(self):
        """
        Dimensions of the data produced

        Returns:
             A tuple of the names of dimensions of the data returned
        """
        dims = ('records', 'samples', 'channels')
        return dims

    def process_buffer(self, buf):
        """As we are only averaging over buffers this function is a noop"""
        return buf

    def post_acquire(self):
        data = super().post_acquire()
        if self._nbits == 12:
            data = np.right_shift(self.data, 4)

        return (data.astype(np.float32) / (2**self._nbits)) - 0.5


class AvgRecCtl(BaseAcqCtl):
    """
    A controller that averages over records. The data returned has the shape
    of Buffers x Samples x Channels
    """
    def __init__(self, *arg, **kw):
        super().__init__(*arg, **kw)
        self._average_records = True

        if self._nbits == 8:
            self._datadtype = np.uint16
        elif self._nbits == 12:
            self._datadtype = np.uint32


    def data_shape(self):
        """
        Shape of the data that this controller will produce

        Returns:
            A tuple of the sizes of the data dimensions.
        """
        shp = (self.buffers_per_acquisition(),
               self.samples_per_record(),
               self.number_of_channels)
        return shp

    def data_dims(self):
        """
        Dimensions of the data produced

        Returns:
             A tuple of the names of dimensions of the data returned
        """
        dims = ('buffers', 'samples', 'channels')
        return dims

    def process_buffer(self, buf):
        """Average over records. For an individual buffer this is
        the first dimension"""
        return np.mean(buf, axis=0)

    def post_acquire(self):
        data = super().post_acquire()
        if self._nbits == 12:
            data = np.right_shift(self.data, 4)

        return (data.astype(np.float32) / (2**self._nbits)) - 0.5


class AvgDemodCtl(AvgBufCtl):
    """
    A controller that averages over buffers and subsequently
    demodulates the averaged data. The data returned has the format
    Records x Demodulated Samples x Channels.

    The demodulated samples are averaged over a period of
    sample_rate//demod_frq rounded up to nearest integer compared to
    the samples in the time series.
    """
    def __init__(self, *arg, **kw):
        super().__init__(*arg, **kw)
        self.add_parameter('demod_frq', set_cmd=None, unit='Hz')

    def data_shape(self):
        """
        Shape of the data that this controller will produce

        Returns:
            A tuple of the sizes of the data dimensions.
        """
        self.period = int(self.sample_rate() / self.demod_frq() + 0.5)
        self.demod_samples = self.samples_per_record() // self.period
        self.demod_tvals = self.tvals[::self.period][:self.demod_samples]
        self.cosarr = (np.cos(2*np.pi*self.demod_frq()*self.tvals).reshape(1,-1,1))
        self.sinarr = (np.sin(2*np.pi*self.demod_frq()*self.tvals).reshape(1,-1,1))

        return (self.records_per_buffer(),
                self.demod_samples,
                self.number_of_channels)

    def data_dims(self):
        """
        Dimensions of the data produced

        Returns:
             A tuple of the names of dimensions of the data returned
        """
        return ('records', 'IF_periods', 'channels')

    def pre_start_capture(self):
        super().pre_start_capture()
        self.data = np.zeros((
            self.records_per_buffer(),
            self.samples_per_record(),
            self.number_of_channels,
        )).astype(self._datadtype)

    def post_acquire(self):
        """Demodulate the data and average over period of
        sample_rate//demod_frq rounded up to nearest integer"""
        data = super().post_acquire()
        real = (data * 2 * self.cosarr)[:,:self.demod_samples*self.period,:].reshape(
            -1, self.demod_samples, self.period, self.number_of_channels).mean(axis=-2)
        imag = (data * 2 * self.sinarr)[:,:self.demod_samples*self.period,:].reshape(
            -1, self.demod_samples, self.period, self.number_of_channels).mean(axis=-2)
        return real + 1j * imag


class AvgRecDemodCtl(AvgRecCtl):
    """
    A controller that averages over records and subsequently
    demodulates the averaged data. The data returned has the format
    Buffers x Demodulated Samples x Channels.

    The demodulated samples are averaged over a period of
    sample_rate//demod_frq rounded up to nearest integer compared to
    the samples in the time series.
    """
    def __init__(self, *arg, **kw):
        super().__init__(*arg, **kw)
        self.add_parameter('demod_frq', set_cmd=None, unit='Hz')

    def data_shape(self):
        """
        Shape of the data that this controller will produce

        Returns:
            A tuple of the sizes of the data dimensions.
        """
        self.period = int(self.sample_rate() / self.demod_frq() + 0.5)
        self.demod_samples = self.samples_per_record() // self.period
        self.demod_tvals = self.tvals[::self.period][:self.demod_samples]
        self.cosarr = (np.cos(2*np.pi*self.demod_frq()*self.tvals).reshape(1,-1,1))
        self.sinarr = (np.sin(2*np.pi*self.demod_frq()*self.tvals).reshape(1,-1,1))

        return (self.records_per_buffer(),
                self.demod_samples,
                self.number_of_channels)

    def data_dims(self):
        """
        Dimensions of the data produced

        Returns:
             A tuple of the names of dimensions of the data returned
        """
        return ('buffers', 'IF_periods', 'channels')

    def pre_start_capture(self):
        super().pre_start_capture()
        self.data = np.zeros((
            self.buffers_per_acquisition(),
            self.samples_per_record(),
            self.number_of_channels,
        )).astype(self._datadtype)

    def post_acquire(self):
        """Demodulate the data and average over period of
        sample_rate//demod_frq rounded up to nearest integer"""
        data = super().post_acquire()
        real = (data * 2 * self.cosarr)[:,:self.demod_samples*self.period,:].reshape(
            -1, self.demod_samples, self.period, self.number_of_channels).mean(axis=-2)
        imag = (data * 2 * self.sinarr)[:,:self.demod_samples*self.period,:].reshape(
            -1, self.demod_samples, self.period, self.number_of_channels).mean(axis=-2)
        return real + 1j * imag


class AvgIQCtl(AvgDemodCtl):
    """
    A controller that averages over buffers and subsequently
    demodulates the averaged data and finally averages over all demodulated
    samples. The data returned has the format Records x Channels.

    The demodulated samples are averaged over a period of
    sample_rate//demod_frq rounded up to nearest integer
    and subsequently averaged over all the periods.
    """
    def data_shape(self):
        """
        Shape of the data that this controller will produce

        Returns:
            A tuple of the sizes of the data dimensions.
        """
        # really call super so that cos and sin arrays are setup correctly
        # for demodulation
        super().data_shape()
        return (self.records_per_buffer(),
                self.number_of_channels)

    def data_dims(self):
        """
        Dimensions of the data produced

        Returns:
             A tuple of the names of dimensions of the data returned
        """
        return ('records', 'channels')

    def post_acquire(self):
        """
        Average data from super method over all periods.
        """
        return super().post_acquire().mean(axis=1)


class AvgRecIQCtl(AvgRecDemodCtl):
    """
    A controller that averages over records and subsequently
    demodulates the averaged data and finally averages over all demodulated
    samples. The data returned has the format Buffers x Channels.

    The demodulated samples are averaged over a period of
    sample_rate//demod_frq rounded up to nearest integer
    and subsequently averaged over all the periods.
    """
    def data_shape(self):
        """
        Shape of the data that this controller will produce

        Returns:
            A tuple of the sizes of the data dimensions.
        """
        # really call super so that cos and sin arrays are setup correctly
        # for demodulation
        super().data_shape()
        return (self.buffers_per_acquisition(),
                self.number_of_channels)

    def data_dims(self):
        """
        Dimensions of the data produced

        Returns:
             A tuple of the names of dimensions of the data returned
        """
        return ('buffers', 'channels')

    def post_acquire(self):
        """
        Average data from super method over all periods.
        """
        return super().post_acquire().mean(axis=1)





"""
####
#### OLDER, CURRENTLY NOT WORKING CONTROLLERS. NEEDS TO BE FIXED.
####

class DemodAcqCtl(BaseAcqCtl):

    DATADTYPE = np.complex64

    def __init__(self, *arg, **kw):
        super().__init__(*arg, **kw)
        self.add_parameter('demod_frq', set_cmd=None, unit='Hz')

    def data_shape(self):
        alazar = self._get_alazar()
        self.period = int(alazar.sample_rate() / self.demod_frq() + 0.5)
        self.demod_samples = self.samples_per_record() // self.period
        self.demod_tvals = self.tvals[::self.period][:self.demod_samples]
        self.cosarr = (np.cos(2*np.pi*self.demod_frq()*self.tvals).reshape(1,1,-1,1))
        self.sinarr = (np.sin(2*np.pi*self.demod_frq()*self.tvals).reshape(1,1,-1,1))

        return (self.buffers_per_acquisition(),
                self.records_per_buffer(),
                self.demod_samples,
                self.number_of_channels)

    def data_dims(self):
        return ('buffers', 'records', 'IF_periods', 'channels')

    def process_buffer(self, buf):
        real_data = (buf * self.cosarr)[:, :, :self.demod_samples*self.period, :]
        real_data = real_data.reshape(-1, self.demod_samples, self.period, 2).mean(axis=-2) / self.RANGE

        imag_data = (buf * self.sinarr)[:, :, :self.demod_samples*self.period, :]
        imag_data = imag_data.reshape(-1, self.demod_samples, self.period, 2).mean(axis=-2) / self.RANGE

        return real_data + 1j * imag_data


class DemodRelAcqCtl(DemodAcqCtl):

    REFCHAN = 0
    SIGCHAN = 1

    def data_shape(self):
        ds = list(super().data_shape())
        return tuple(ds[:-1])

    def data_dims(self):
        return ('buffers', 'records', 'IF_periods')

    def process_buffer(self, buf):
        data = super().process_buffer(buf)
        phi = np.angle(data[:, :, self.REFCHAN])
        return data[:, :, self.SIGCHAN] * np.exp(-1j*phi)


class IQAcqCtl(BaseAcqCtl):

    DATADTYPE = np.complex64

    def __init__(self, *arg, **kw):
        super().__init__(*arg, **kw)

        self.add_parameter('demod_frq', set_cmd=None, unit='Hz')

    def data_shape(self):
        alazar = self._get_alazar()
        self.period = int(alazar.sample_rate() / self.demod_frq() + 0.5)
        self.cosarr = (np.cos(2*np.pi*self.demod_frq()*self.tvals).reshape(1,1,-1,1))
        self.sinarr = (np.sin(2*np.pi*self.demod_frq()*self.tvals).reshape(1,1,-1,1))

        return (self.buffers_per_acquisition(),
                self.records_per_buffer(),
                self.number_of_channels)

    def data_dims(self):
        return ('buffers', 'records', 'channels')

    def process_buffer(self, buf):
        real_data = np.tensordot(buf, self.cosarr, axes=(-2, -2)).reshape(self.records_per_buffer(), 2) / self.RANGE / self.samples_per_record()
        imag_data = np.tensordot(buf, self.sinarr, axes=(-2, -2)).reshape(self.records_per_buffer(), 2) / self.RANGE / self.samples_per_record()
        return real_data + 1j * imag_data


class IQRelAcqCtl(IQAcqCtl):

    REFCHAN = 0
    SIGCHAN = 1

    def data_shape(self):
        ds = list(super().data_shape())
        return tuple(ds[:-1])

    def data_dims(self):
        return ('buffers', 'records')

    def process_buffer(self, buf):
        data = super().process_buffer(buf)
        phi = np.angle(data[..., self.REFCHAN])
        return data[..., self.SIGCHAN] * np.exp(-1j*phi)
"""
