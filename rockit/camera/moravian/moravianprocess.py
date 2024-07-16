#
# This file is part of the Robotic Observatory Control Kit (rockit)
#
# rockit is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# rockit is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with rockit.  If not, see <http://www.gnu.org/licenses/>.

"""Helper process for interfacing with the Moravian SDK"""

# pylint: disable=too-many-arguments
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-return-statements
# pylint: disable=too-many-branches
# pylint: disable=too-many-statements

from ctypes import byref, CDLL, c_bool, c_double, c_float, c_int, c_size_t, c_uint8, c_uint16, c_uint, c_void_p
from ctypes import create_string_buffer
import json
import pathlib
import sys
import threading
import time
import traceback
from astropy.time import Time
import astropy.units as u
import Pyro4
from rockit.common import log
from .constants import CommandStatus, CameraStatus


class GXCCDIntegerParameter:
    ChipWidth = 1
    ChipHeight = 2
    FirmwareVersion = 128
    DriverVersion = 131
    FlashVersion = 134


class GXCCDFloatParameter:
    ChipTemperature = 0
    HeatsinkTemperature = 1
    SupplyVoltage = 10
    CoolerPower = 11
    ADCGain = 20


class MoravianInterface:
    def __init__(self, config, processing_queue,
                 processing_framebuffer, processing_framebuffer_offsets,
                 processing_stop_signal):
        self._config = config

        self._handle = c_void_p()
        self._driver = None
        self._driver_lock = threading.Lock()

        self._camera_firmware_version = ''
        self._sdk_version = ''
        self._camera_description = ''

        self._readout_width = 0
        self._readout_height = 0

        self._stream_frames = config.stream

        # Crop output data to detector coordinates
        self._window_region = [0, 0, 0, 0]
        self._binning = config.binning

        self._polled_condition = threading.Condition()
        self._polled_state = {}
        self._cooler_setpoint = config.cooler_setpoint

        self._exposure_time = 1
        self._gain = config.gain

        # Limit and number of frames acquired during the next sequence
        # Set to 0 to run continuously
        self._sequence_frame_limit = 0

        # Number of frames acquired this sequence
        self._sequence_frame_count = 0

        # Time that the latest frame in the exposure was started
        self._sequence_exposure_start_time = Time.now()

        # Information for building the output filename
        self._output_directory = pathlib.Path(config.output_path)
        self._output_frame_prefix = config.output_prefix

        # Persistent frame counters
        self._counter_filename = config.expcount_path
        try:
            with open(self._counter_filename, 'r', encoding='ascii') as infile:
                data = json.load(infile)
                self._exposure_count = data['exposure_count']
                self._exposure_count_reference = data['exposure_reference']
        except Exception:
            now = Time.now().strftime('%Y-%m-%d')
            self._exposure_count = 0
            self._exposure_count_reference = now

        # Thread that runs the exposure sequence
        # Initialized by start() method
        self._acquisition_thread = None

        # Signal that the exposure sequence should be terminated
        # at end of the current frame
        self._stop_acquisition = False

        # Subprocess for processing acquired frames
        self._processing_queue = processing_queue
        self._processing_stop_signal = processing_stop_signal

        # A large block of shared memory for sending frame data to the processing workers
        self._processing_framebuffer = processing_framebuffer

        # A queue of memory offsets that are available to write frame data into
        # Offsets are popped from the queue as new frames are written into the
        # frame buffer, and pushed back on as processing is complete
        self._processing_framebuffer_offsets = processing_framebuffer_offsets

        # Camera has been disconnected/powered off or the driver has crashed
        # Detected by way of the temperature/cooler queries returning 0xFFFFFFFF
        self._driver_lost_camera = False

        threading.Thread(target=self.__status_thread, daemon=True).start()

        self._polled_state = {
            'chip_temp': 0,
            'heatsink_temp': 0,
            'supply_voltage': 0,
            'cooler_power': 0,
            'gps_fix': False,
            'gps_satellites': 0
        }


    @property
    def last_error(self):
        error = create_string_buffer(2048)
        self._driver.gxccd_get_last_error(self._handle, error, 2047)
        return error.value.decode('ascii')

    @property
    def is_acquiring(self):
        return self._acquisition_thread is not None and self._acquisition_thread.is_alive()

    @property
    def driver_lost_camera(self):
        return self._driver_lost_camera

    def __status_thread(self):
        """Polls and updates cooler status"""
        unused1 = byref(c_double(-1.0))
        unused2 = byref(c_int(-1))
        while True:
            with self._driver_lock:
                if self._driver is not None:
                    satellites = c_uint(0)
                    fix = c_bool()
                    self._driver.gxccd_get_gps_data(self._handle, unused1, unused1, unused1, unused2, unused2, unused2,
                                                    unused2, unused2, unused1, byref(satellites), byref(fix))
                    state = {
                        'gps_fix': fix.value,
                        'gps_satellites': satellites.value
                    }

                    params = [
                        ('chip_temp', GXCCDFloatParameter.ChipTemperature),
                        ('heatsink_temp', GXCCDFloatParameter.HeatsinkTemperature),
                        ('cooler_power', GXCCDFloatParameter.CoolerPower),
                        ('supply_voltage', GXCCDFloatParameter.SupplyVoltage),
                    ]

                    for key, param in params:
                        value = c_float()

                        if self._driver.gxccd_get_value(self._handle, param, byref(value)):
                            print(f'Error polling camera: {self.last_error}')
                            self._driver_lost_camera = True
                            break

                        state[key] = value.value

                    self._polled_state = state

            with self._polled_condition:
                self._polled_condition.wait(self._config.cooler_update_delay)

    def __run_exposure_sequence(self, quiet):
        """Worker thread that acquires frames and their times.
           Tagged frames are pushed to the acquisition queue
           for further processing on another thread"""
        framebuffer_slots = 0
        try:
            # Prepare the framebuffer offsets
            if not self._processing_framebuffer_offsets.empty():
                log.error(self._config.log_name, 'Frame buffer offsets queue is not empty!')
                return

            offset = 0
            frame_size = 2 * self._readout_width * self._readout_height
            while offset + frame_size <= len(self._processing_framebuffer):
                self._processing_framebuffer_offsets.put(offset)
                offset += frame_size
                framebuffer_slots += 1

            ready = c_bool()

            if self._stream_frames:
                # Start first exposure - subsequent exposures are triggered automatically.
                with self._driver_lock:
                    if self._driver.gxccd_start_exposure(self._handle, c_double(self._exposure_time), c_bool(True),
                                                         0, 0, self._readout_width, self._readout_height):
                        log.error(self._config.log_name, f'Failed to start exposure sequence: {self.last_error}')
                        return

            while not self._stop_acquisition and not self._processing_stop_signal.value:
                self._sequence_exposure_start_time = Time.now()

                if not self._stream_frames:
                    # Start every individual exposure
                    with self._driver_lock:
                        if self._driver.gxccd_start_exposure(self._handle, c_double(self._exposure_time), c_bool(True),
                                                             0, 0, self._readout_width, self._readout_height):
                            log.error(self._config.log_name, f'Failed to start exposure sequence: {self.last_error}')
                            return

                while True:
                    with self._driver_lock:
                        self._driver.gxccd_image_ready(self._handle, byref(ready))
                    if ready.value or self._stop_acquisition or self._processing_stop_signal.value:
                        break

                    time.sleep(0.01)

                framebuffer_offset = self._processing_framebuffer_offsets.get()
                cdata = (c_uint8 * frame_size).from_buffer(self._processing_framebuffer, framebuffer_offset)

                gps_start_time = None
                if not self._stop_acquisition and not self._processing_stop_signal.value:
                    year = c_int(-1)
                    month = c_int(-1)
                    day = c_int(-1)
                    hour = c_int(-1)
                    minute = c_int(-1)
                    second = c_double(-1.0)

                    with self._driver_lock:
                        if self._stream_frames:
                            self._driver.gxccd_read_image_exposure(self._handle, byref(cdata), frame_size)
                        else:
                            self._driver.gxccd_read_image(self._handle, byref(cdata), frame_size)

                        gps_start_time = None
                        read_end_time = Time.now()

                        if self._config.use_gps:
                            res = self._driver.gxccd_get_image_time_stamp(
                                self._handle, byref(year), byref(month), byref(day),
                                byref(hour), byref(minute), byref(second))

                            if res == 0:
                                gps_date = f'{year.value:04d}-{month.value:02d}-{day.value:02d}'
                                gps_time = f'{hour.value:02d}:{minute.value:02d}:{second.value:06.3f}'
                                gps_start_time = Time(f'{gps_date}T{gps_time}')
                            else:
                                print('error querying timestamp:', self.last_error)

                if self._stop_acquisition or self._processing_stop_signal.value:
                    # Return unused slot back to the queue to simplify cleanup
                    self._processing_framebuffer_offsets.put(framebuffer_offset)
                    break

                metadata = {
                    'data_offset': framebuffer_offset,
                    'data_width': self._readout_width,
                    'data_height': self._readout_height,
                    'requested_exposure': float(self._exposure_time),
                    'row_period': self._config.row_period_us * 1e-6,
                    'exposure': float(self._exposure_time),
                    'gain': self._gain,
                    'stream': self._stream_frames,
                    'camera_description': self._camera_description,
                    'gps_start_time': gps_start_time,
                    'read_end_time': read_end_time,
                    'sdk_version': self._sdk_version,
                    'firmware_version': self._camera_firmware_version,
                    'window_region': self._window_region,
                    'image_region': self._window_region,
                    'binning': self._binning,
                    'exposure_count': self._exposure_count,
                    'exposure_count_reference': self._exposure_count_reference,
                    'cooler_setpoint': self._cooler_setpoint,
                }

                metadata.update(self._polled_state)
                self._processing_queue.put(metadata)

                self._exposure_count += 1
                self._sequence_frame_count += 1

                # Continue exposure sequence?
                if 0 < self._sequence_frame_limit <= self._sequence_frame_count:
                    self._stop_acquisition = True
        finally:
            with self._driver_lock:
                self._driver.gxccd_abort_exposure(self._handle, c_bool(False))

            # Save updated counts to disk
            with open(self._counter_filename, 'w', encoding='ascii') as outfile:
                json.dump({
                    'exposure_count': self._exposure_count,
                    'exposure_reference': self._exposure_count_reference,
                }, outfile)

            # Wait for processing to complete
            for _ in range(framebuffer_slots):
                self._processing_framebuffer_offsets.get()

            if not quiet:
                log.info(self._config.log_name, 'Exposure sequence complete')
            self._stop_acquisition = False

    @Pyro4.expose
    def initialize(self):
        """Connects to the camera driver"""
        print('initializing driver')
        with self._driver_lock:
            driver = CDLL('/usr/lib64/libgxccd.so')

            driver.gxccd_initialize_usb.argtypes = [c_int]
            driver.gxccd_initialize_usb.restype = c_void_p
            driver.gxccd_release.argtypes = [c_void_p]
            driver.gxccd_get_value.argtypes = [c_void_p, c_int, c_void_p]
            driver.gxccd_get_integer_parameter.argtypes = [c_void_p, c_int, c_void_p]
            driver.gxccd_get_boolean_parameter.argtypes = [c_void_p, c_int, c_void_p]
            driver.gxccd_get_string_parameter.argtypes = [c_void_p, c_int, c_void_p, c_size_t]
            driver.gxccd_set_gain.argtypes = [c_void_p, c_uint16]
            driver.gxccd_set_temperature.argtypes = [c_void_p, c_float]
            driver.gxccd_start_exposure.argtypes = [c_void_p, c_double, c_bool, c_int, c_int, c_int, c_int]
            driver.gxccd_start_exposure.restype = c_int
            driver.gxccd_image_ready.argtypes = [c_void_p, c_void_p]
            driver.gxccd_read_image.argtypes = [c_void_p, c_void_p, c_size_t]
            driver.gxccd_read_image_exposure.argtypes = [c_void_p, c_void_p, c_size_t]
            driver.gxccd_abort_exposure.argtypes = [c_void_p, c_bool]
            driver.gxccd_get_last_error.argtypes = [c_void_p, c_void_p, c_size_t]
            driver.gxccd_get_gps_data.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p, c_void_p, c_void_p, c_void_p,
                                                  c_void_p, c_void_p, c_void_p, c_void_p, c_void_p]
            driver.gxccd_get_image_time_stamp.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p, c_void_p, c_void_p,
                                                          c_void_p]

            handle = None
            initialized = False

            # Note: can't use property because self._driver and self._handle haven't yet been set.
            def get_error():
                error = create_string_buffer(2048)
                driver.gxccd_get_last_error(handle, error, 2047)
                return error.value.decode('ascii')

            try:
                handle = driver.gxccd_initialize_usb(self._config.camera_serial)
                if handle is None:
                    print(f'camera {self._config.camera_serial} was not found')
                    return CommandStatus.CameraNotFound

                buf = create_string_buffer(64)
                if driver.gxccd_get_string_parameter(handle, 0, buf, 63):
                    print(f'failed to query camera description: {get_error()}')
                    return CommandStatus.Failed

                def build_version_string(param):
                    components = []
                    for i in range(3):
                        v = c_int(-1)
                        if driver.gxccd_get_integer_parameter(handle, param + i, byref(v)):
                            print(f'failed to query version information: {get_error()}')
                            return CommandStatus.Failed
                        components.append(v.value)
                    return '.'.join(f'{c:02d}' for c in components)

                self._sdk_version = build_version_string(GXCCDIntegerParameter.DriverVersion)
                self._camera_firmware_version = build_version_string(GXCCDIntegerParameter.FirmwareVersion) + ' / ' + \
                    build_version_string(GXCCDIntegerParameter.FlashVersion)
                self._camera_description = f'{buf.value.decode("ascii").strip()} ({self._config.camera_serial})'

                image_width = c_int(-1)
                image_height = c_int(-1)

                if driver.gxccd_get_integer_parameter(handle, GXCCDIntegerParameter.ChipWidth, byref(image_width)):
                    print(f'failed to query sensor width: {get_error()}')
                    return CommandStatus.Failed

                if driver.gxccd_get_integer_parameter(handle, GXCCDIntegerParameter.ChipHeight, byref(image_height)):
                    print(f'failed to query sensor width: {get_error()}')
                    return CommandStatus.Failed

                self._readout_width = image_width.value
                self._readout_height = image_height.value

                self._gain = self._config.gain
                if driver.gxccd_set_gain(handle, c_uint16(self._gain)):
                    print(f'failed to set default gain: {get_error()}')
                    return CommandStatus.Failed

                if driver.gxccd_set_temperature(handle, c_float(self._config.cooler_setpoint)):
                    print(f'failed to set default temperature: {get_error()}')
                    return CommandStatus.Failed

                # Regions are 0-indexed x1,x2,y1,2
                # These are converted to 1-indexed when writing fits headers
                self._window_region = [
                    0,
                    image_width.value - 1,
                    0,
                    image_height.value - 1
                ]

                self._driver = driver
                self._handle = handle
                initialized = True
                print(f'{self._camera_description} initialized')

                return CommandStatus.Succeeded
            except Exception as e:
                print(e)
                return CommandStatus.Failed
            finally:
                # Clean up on failure
                if not initialized:
                    if driver is not None and handle is not None:
                        driver.gxccd_release(handle)

                    log.error(self._config.log_name, 'Failed to initialize camera')
                else:
                    log.info(self._config.log_name, 'Initialized camera')
                    with self._polled_condition:
                        self._polled_condition.notify()

    def set_target_temperature(self, temperature, quiet):
        """Set the target camera temperature"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        if temperature is not None and (temperature < -20 or temperature > 30):
            return CommandStatus.TemperatureOutsideLimits

        with self._driver_lock:
            if self._driver.gxccd_set_temperature(self._handle, c_float(temperature)):
                print(f'Failed to set target temperature: {self.last_error}')
                return CommandStatus.Failed

            self._cooler_setpoint = temperature
        with self._polled_condition:
            self._polled_condition.notify()

        if not quiet:
            log.info(self._config.log_name, f'Target temperature set to {temperature}')

        return CommandStatus.Succeeded

    def set_gain(self, gain, quiet):
        """Set the camera gain"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        if gain < 0 or gain >= 4096:
            return CommandStatus.GainOutsideLimits

        with self._driver_lock:
            if self._driver.gxccd_set_gain(self._handle, c_uint16(self._gain)):
                print(f'Failed to set gain: {self.last_error}')
                return CommandStatus.Failed
            self._gain = gain

        if not quiet:
            log.info(self._config.log_name, f'Gain set to {gain}')

        return CommandStatus.Succeeded

    def set_frame_streaming(self, stream, quiet):
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        if self._stream_frames == stream:
            return CommandStatus.Succeeded

        with self._driver_lock:
            self._stream_frames = stream

            if not quiet:
                log.info(self._config.log_name, f'Streaming set to {stream}')

            return CommandStatus.Succeeded


    def set_exposure(self, exposure, quiet):
        """Set the camera exposure time"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        self._exposure_time = exposure
        if not quiet:
            log.info(self._config.log_name, f'Exposure time set to {exposure:.3f}s')

        return CommandStatus.Succeeded

    def set_window(self, window, quiet):
        """Set the camera crop window"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        if window is None:
            self._window_region = [0, self._readout_width - 1, 0, self._readout_height - 1]
            return CommandStatus.Succeeded

        if len(window) != 4:
            return CommandStatus.Failed
        if window[0] < 1 or window[0] > self._readout_width:
            return CommandStatus.WindowOutsideSensor
        if window[1] < window[0] or window[1] > self._readout_width:
            return CommandStatus.WindowOutsideSensor
        if window[2] < 1 or window[2] > self._readout_height:
            return CommandStatus.WindowOutsideSensor
        if window[3] < window[2] or window[3] > self._readout_height:
            return CommandStatus.WindowOutsideSensor

        # Convert from 1-indexed to 0-indexed
        self._window_region = [x - 1 for x in window]
        return CommandStatus.Succeeded


    def set_binning(self, binning, quiet):
        """Set the camera binning"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        if binning is None:
            binning = self._config.binning

        if not isinstance(binning, int) or binning < 1:
            return CommandStatus.Failed

        self._binning = binning

        if not quiet:
            log.info(self._config.log_name, f'Binning set to {binning}')

        return CommandStatus.Succeeded

    @Pyro4.expose
    def start_sequence(self, count, quiet):
        """Starts an exposure sequence with a set number of frames, or 0 to run until stopped"""
        if self.is_acquiring:
            return CommandStatus.CameraNotIdle

        self._sequence_frame_limit = count
        self._sequence_frame_count = 0
        self._stop_acquisition = False
        self._processing_stop_signal.value = False

        self._acquisition_thread = threading.Thread(
            target=self.__run_exposure_sequence,
            args=(quiet,), daemon=True)
        self._acquisition_thread.start()

        if not quiet:
            count_msg = 'until stopped'
            if count == 1:
                count_msg = '1 frame'
            elif count > 1:
                count_msg = f'{count} frames'

            log.info(self._config.log_name, f'Starting exposure sequence ({count_msg})')

        return CommandStatus.Succeeded

    @Pyro4.expose
    def stop_sequence(self, quiet):
        """Stops any active exposure sequence"""
        if not self.is_acquiring or self._stop_acquisition:
            return CommandStatus.CameraNotAcquiring

        if not quiet:
            log.info(self._config.log_name, 'Aborting exposure sequence')

        self._sequence_frame_count = 0
        self._stop_acquisition = True

        return CommandStatus.Succeeded

    def report_status(self):
        """Returns a dictionary containing the current camera state"""
        # Estimate the current frame progress based on the time delta
        exposure_progress = 0
        sequence_frame_count = self._sequence_frame_count
        state = CameraStatus.Idle

        if self.is_acquiring:
            state = CameraStatus.Acquiring
            if self._stop_acquisition:
                state = CameraStatus.Aborting
            else:
                if self._sequence_exposure_start_time is not None:
                    exposure_progress = (Time.now() - self._sequence_exposure_start_time).to(u.s).value

        data = {
            'state': state,
            'exposure_time': self._exposure_time,
            'exposure_progress': exposure_progress,
            'window': self._window_region,
            'binning': self._binning,
            'sequence_frame_limit': self._sequence_frame_limit,
            'sequence_frame_count': sequence_frame_count,
            'cooler_setpoint': self._cooler_setpoint,
            'temperature_locked': False, # TODO
            'stream': self._stream_frames
        }

        data.update(self._polled_state)
        return data

    def shutdown(self):
        """Disconnects from the camera driver"""
        # Complete the current exposure
        if self._acquisition_thread is not None:
            with self._driver_lock:
                self._driver.gxccd_abort_exposure(self._handle, False)
            print('shutdown: waiting for acquisition to complete')
            self._stop_acquisition = True
            self._acquisition_thread.join()

        with self._driver_lock:
            print('shutdown: disconnecting driver')
            self._driver.gxccd_release(self._handle)
            self._driver = None

        log.info(self._config.log_name, 'Shutdown camera')
        return CommandStatus.Succeeded


def moravian_process(camd_pipe, config, processing_queue, processing_framebuffer, processing_framebuffer_offsets,
                     stop_signal):
    cam = MoravianInterface(config, processing_queue, processing_framebuffer, processing_framebuffer_offsets,
                            stop_signal)
    ret = cam.initialize()
    camd_pipe.send(ret)
    if ret != CommandStatus.Succeeded:
        return

    try:
        while True:
            if camd_pipe.poll(timeout=1):
                c = camd_pipe.recv()
                command = c['command']
                args = c['args']

                if command == 'temperature':
                    camd_pipe.send(cam.set_target_temperature(args['temperature'], args['quiet']))
                elif command == 'gain':
                    camd_pipe.send(cam.set_gain(args['gain'], args['quiet']))
                elif command == 'stream':
                    camd_pipe.send(cam.set_frame_streaming(args['stream'], args['quiet']))
                elif command == 'exposure':
                    camd_pipe.send(cam.set_exposure(args['exposure'], args['quiet']))
                elif command == 'window':
                    camd_pipe.send(cam.set_window(args['window'], args['quiet']))
                elif command == 'binning':
                    camd_pipe.send(cam.set_binning(args['binning'], args['quiet']))
                elif command == 'start':
                    camd_pipe.send(cam.start_sequence(args['count'], args['quiet']))
                elif command == 'stop':
                    camd_pipe.send(cam.stop_sequence(args['quiet']))
                elif command == 'status':
                    camd_pipe.send(cam.report_status())
                elif command == 'shutdown':
                    break
                else:
                    print(f'unhandled command: {command}')
                    camd_pipe.send(CommandStatus.Failed)

                if cam.driver_lost_camera:
                    log.error(config.log_name, 'camera has disappeared')
                    break

    except Exception:
        traceback.print_exc(file=sys.stdout)
        camd_pipe.send(CommandStatus.Failed)

    camd_pipe.close()
    cam.shutdown()
