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

"""Helper process for preparing and saving fits images"""

# pylint: disable=too-many-arguments
# pylint: disable=too-many-branches

import os.path
import shutil
from astropy.io import fits
import astropy.units as u
import numpy as np
from rockit.common import daemons, log


def window_sensor_region(region, window):
    """Calculate new region coordinates when cropped to a given window"""
    x1 = max(0, region[0] - window[0])
    x2 = min(region[1] - window[0], window[1] - window[0])
    y1 = max(0, region[2] - window[2])
    y2 = min(region[3] - window[2], window[3] - window[2])
    if x1 > x2 or y1 > y2:
        return None

    return [x1, x2, y1, y2]


def bin_sensor_region(region, binning):
    """Calculate new region coordinates when binned by a given value"""
    return [
        (region[0] + binning - 1) // binning,
        region[1] // binning,
        (region[2] + binning - 1) // binning,
        region[3] // binning
    ]


def format_sensor_region(region):
    """Format a 0-indexed region as a 1-indexed fits region"""
    return f'[{region[0] + 1}:{region[1] + 1},{region[2] + 1}:{region[3] + 1}]'


def output_process(process_queue, processing_framebuffer, processing_framebuffer_offsets, stop_signal,
                   camera_id, camera_serial, header_card_capacity, output_path, log_name,
                   pipeline_daemon_name, pipeline_handover_timeout):
    """
    Helper process to save frames to disk.
    This uses a process (rather than a thread) to avoid the GIL bottlenecking throughput,
    and multiple worker processes allow frames to be handled in parallel.
    """
    pipeline_daemon = getattr(daemons, pipeline_daemon_name)
    while True:
        frame = process_queue.get()

        data = np.frombuffer(processing_framebuffer, dtype=np.uint16,
                             offset=frame['data_offset'], count=frame['data_height'] * frame['data_width']) \
            .reshape((frame['data_height'], frame['data_width'])).copy()
        processing_framebuffer_offsets.put(frame['data_offset'])

        # Estimate frame end time based on when we finished reading out
        end_offset = -frame['lineperiod'] * (frame['data_height'] - 2 * (frame['window_region'][2] // 2))

        start_offset = end_offset - frame['exposure']
        end_time = (frame['read_end_time'] + end_offset * u.s).strftime('%Y-%m-%dT%H:%M:%S.%f')
        start_time = (frame['read_end_time'] + start_offset * u.s).strftime('%Y-%m-%dT%H:%M:%S.%f')
        date_headers = [
            ('DATE-OBS', start_time, '[utc] estimated row 0 exposure start time'),
            ('DATE-END', end_time, '[utc] estimated row 0 exposure end time'),
            ('TIME-SRC', 'NTP', 'DATE-OBS is estimated from NTP-synced PC clock'),
        ]

        # TODO
        if frame['gps_start_time'] and False:
            date_headers = [
                ('DATE-OBS', start_time, '[utc] row 0 exposure start time'),
                ('DATE-END', end_time, '[utc] row 0 exposure end time'),
                ('TIME-SRC', 'GPS', 'DATE-OBS is from a GPS measured HSYNC signal'),
            ]

        # Crop data to window
        image_region = window_sensor_region(frame['image_region'], frame['window_region'])
        window_region = frame['window_region']
        if image_region != frame['image_region']:
            # Crop output data
            # NOTE: Rolling shutter correction is handled in the branches above
            data = data[window_region[2]:window_region[3] + 1, window_region[0]:window_region[1] + 1]

        if frame['binning'] > 1:
            nrows, ncols = data.shape
            n_binned_cols = ncols//frame['binning']
            n_binned_rows = nrows//frame['binning']
            binned_cols = np.zeros((nrows, n_binned_cols), dtype=np.uint32)

            for i in range(nrows):
                binned_cols[i] = np.sum(data[i][:n_binned_cols*frame['binning']]
                                        .reshape(n_binned_cols, frame['binning']), axis=1)

            data = np.zeros((n_binned_rows, n_binned_cols), dtype=np.uint32)
            for i in range(n_binned_cols):
                data[:, i] = np.sum(binned_cols[:, i][:n_binned_rows*frame['binning']]
                                    .reshape(n_binned_rows, frame['binning']), axis=1)

            image_region = bin_sensor_region(image_region, frame['binning'])
            window_region[1] = frame['window_region'][0] + n_binned_cols * frame['binning'] - 1
            window_region[3] = frame['window_region'][2] + n_binned_rows * frame['binning'] - 1

        if image_region is not None:
            image_region_header = ('IMAG-RGN', format_sensor_region(image_region),
                                   '[x1:x2,y1:y2] image region (image coords)')
        else:
            image_region_header = ('COMMENT', ' IMAG-RGN not available', '')

        if frame['cooler_setpoint'] is not None:
            setpoint_header = ('TEMP-SET', frame['cooler_setpoint'], '[deg c] cmos temperature set point')
        else:
            setpoint_header = ('COMMENT', ' TEMP-SET not available', '')

        header = [
            (None, None, None),
            ('COMMENT', ' ---                DATE/TIME                --- ', ''),
        ] + date_headers + [
            ('GPS-SATS', frame['gps_satellites'], 'number of GPS satellites visible'),
            ('EXPTIME', round(frame['exposure'], 3), '[s] actual exposure length'),
            #('EXPRQSTD', round(frame['requested_exposure'], 3), '[s] requested exposure length'),
            #('EXPCADNC', round(frame['frameperiod'], 3), '[s] exposure cadence'),
            ('ROWDELTA', round(frame['lineperiod'] * 1e6, 3), '[us] rolling shutter unbinned row period'),
            ('PC-RDEND', frame['read_end_time'].strftime('%Y-%m-%dT%H:%M:%S.%f'),
             '[utc] local PC time when readout completed'),
            (None, None, None),
            ('COMMENT', ' ---           CAMERA INFORMATION            --- ', ''),
            ('SDKVER', frame['sdk_version'], 'Moravian SDK version'),
            ('FWVER', frame['firmware_version'], 'camera firmware version'),
            ('CAMID', camera_id, 'camera identifier'),
            ('CAMERA', frame['camera_description'], 'camera model and serial number'),
            ('CAM-GAIN', frame['gain'], 'cmos gain setting'),
            ('CAM-TEMP', round(frame['chip_temp'], 2),
             '[deg c] sensor temperature at end of exposure'),
            #('TEMP-MOD', CoolerMode.label(frame['cooler_mode']), 'temperature control mode'),
            ('TEMP-PWR', round(frame['cooler_power'] * 100), '[%] cooler power'),
            setpoint_header,
            #('TEMP-LCK', frame['cooler_mode'] == CoolerMode.Locked, 'cmos temperature is locked to set point'),
            ('CAM-BIN', frame['binning'], '[px] binning factor'),
            ('CAM-WIND', format_sensor_region(window_region), '[x1:x2,y1:y2] readout region (detector coords)'),
            image_region_header,
            ('EXPCNT', frame['exposure_count'], 'running exposure count since EXPCREF'),
            ('EXPCREF', frame['exposure_count_reference'], 'date the exposure counter was reset'),
        ]

        hdu = fits.PrimaryHDU(data)

        # Using Card and append() to force comment cards to be placed inline
        for h in header:
            hdu.header.append(fits.Card(h[0], h[1], h[2]), end=True)

        # Pad with sufficient blank cards that pipelined won't need to allocate extra header blocks
        padding = max(0, header_card_capacity - len(hdu.header) - 1)
        for _ in range(padding):
            hdu.header.append(fits.Card(None, None, None), end=True)

        # Save errors shouldn't interfere with preview updates, so we use a separate try/catch
        try:
            filename = f'{camera_id}-{frame["exposure_count"]:08d}.fits'
            path = os.path.join(output_path, filename)

            # Simulate an atomic write by writing to a temporary file then renaming
            hdu.writeto(path + '.tmp', overwrite=True)
            shutil.move(path + '.tmp', path)
            print('Saving temporary frame: ' + filename)

        except Exception as e:
            stop_signal.value = True
            log.error(log_name, 'Failed to save temporary frame (' + str(e) + ')')
            continue

        # Hand frame over to the pipeline
        # This may block if the pipeline is busy
        try:
            with pipeline_daemon.connect(pipeline_handover_timeout) as pipeline:
                pipeline.notify_frame(camera_id, filename)
        except Exception as e:
            stop_signal.value = True
            log.error(log_name, 'Failed to hand frame to pipeline (' + str(e) + ')')
