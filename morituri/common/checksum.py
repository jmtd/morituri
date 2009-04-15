# -*- Mode: Python; test-case-name: morituri.test.test_common_checksum -*-
# vi:si:et:sw=4:sts=4:ts=4

# Morituri - for those about to RIP

# Copyright (C) 2009 Thomas Vander Stichele

# This file is part of morituri.
# 
# morituri is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# morituri is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with morituri.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import struct
import zlib

import gobject
import gst

from morituri.common import task

# checksums are not CRC's. a CRC is a specific type of checksum.

SAMPLES_PER_FRAME = 588
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 4
FRAMES_PER_SECOND = 75

class ChecksumTask(task.Task):
    """
    I am a task that calculates a checksum.

    @ivar checksum: the resulting checksum
    """

    # this object needs a main loop to stop
    description = 'Calculating checksum...'

    def __init__(self, path, frameStart=0, frameLength=-1):
        """
        A frame is considered a set of samples for each channel;
        ie 16 bit stereo is 4 bytes per frame.
        If frameLength < 0 it is treated as 'unknown' and calculated.

        @type  frameStart: int
        @param frameStart: the frame to start at
        """
        if not os.path.exists(path):
            raise IndexError, '%s does not exist' % path

        self._path = path
        self._frameStart = frameStart
        self._frameLength = frameLength
        self._frameEnd = None
        self._checksum = 0
        self._bytes = 0 # number of bytes received
        self._first = None
        self._last = None
        self._adapter = gst.Adapter()

        self.checksum = None # result

    def start(self, runner):
        task.Task.start(self, runner)
        self._pipeline = gst.parse_launch('''
            filesrc location="%s" !
            decodebin ! audio/x-raw-int !
            appsink name=sink sync=False emit-signals=True''' % self._path)
        self.debug('pausing')
        self._pipeline.set_state(gst.STATE_PAUSED)
        self._pipeline.get_state()
        self.debug('paused')

        self.debug('query duration')
        sink = self._pipeline.get_by_name('sink')

        if self._frameLength < 0:
            length, format = sink.query_duration(gst.FORMAT_DEFAULT)
            # wavparse 0.10.14 returns in bytes
            if format == gst.FORMAT_BYTES:
                self.debug('query returned in BYTES format')
                length /= 4
            self.debug('total length', length)
            self._frameLength = length - self._frameStart
            self.debug('audio frame length is', self._frameLength)
        self._frameEnd = self._frameStart + self._frameLength - 1

        self.debug('event')


        # the segment end only is respected since -good 0.10.14.1
        event = gst.event_new_seek(1.0, gst.FORMAT_DEFAULT,
            gst.SEEK_FLAG_FLUSH,
            gst.SEEK_TYPE_SET, self._frameStart,
            gst.SEEK_TYPE_SET, self._frameEnd + 1) # half-inclusive interval
        gst.debug('CRCing %s from sector %d to sector %d' % (
            self._path, self._frameStart / SAMPLES_PER_FRAME,
            (self._frameEnd + 1) / SAMPLES_PER_FRAME))
        # FIXME: sending it with frameEnd set screws up the seek, we don't get
        # everything for flac; fixed in recent -good
        result = sink.send_event(event)
        #self.debug('event sent')
        #self.debug(result)
        sink.connect('new-buffer', self._new_buffer_cb)
        sink.connect('eos', self._eos_cb)

        self.debug('scheduling setting to play')
        # since set_state returns non-False, adding it as timeout_add
        # will repeatedly call it, and block the main loop; so
        #   gobject.timeout_add(0L, self._pipeline.set_state, gst.STATE_PLAYING)
        # would not work.

        def play():
            self._pipeline.set_state(gst.STATE_PLAYING)
            return False
        self.runner.schedule(0, play)

        #self._pipeline.set_state(gst.STATE_PLAYING)
        self.debug('scheduled setting to play')

    def _new_buffer_cb(self, sink):
        buffer = sink.emit('pull-buffer')
        gst.log('received new buffer at offset %r with length %r' % (
            buffer.offset, buffer.size))
        if self._first is None:
            self._first = buffer.offset
            self.debug('first sample is', self._first)
        self._last = buffer

        assert len(buffer) % 4 == 0, "buffer is not a multiple of 4 bytes"
        
        # FIXME: gst-python 0.10.14.1 doesn't have adapter_peek/_take wrapped
        # see http://bugzilla.gnome.org/show_bug.cgi?id=576505
        self._adapter.push(buffer)

        while self._adapter.available() >= BYTES_PER_FRAME:
            # FIXME: in 0.10.14.1, take_buffer leaks a ref
            buffer = self._adapter.take_buffer(BYTES_PER_FRAME)

            self._checksum = self.do_checksum_buffer(buffer, self._checksum)
            self._bytes += len(buffer)

            # update progress
            frame = self._first + self._bytes / 4
            framesDone = frame - self._frameStart
            progress = float(framesDone) / float((self._frameLength))
            # marshall to the main thread
            self.runner.schedule(0, self.setProgress, progress)

    def do_checksum_buffer(self, buffer, checksum):
        """
        Subclasses should implement this.
        """
        raise NotImplementedError

    def _eos_cb(self, sink):
        # get the last one; FIXME: why does this not get to us before ?
        #self._new_buffer_cb(sink)
        self.debug('eos, scheduling stop')
        self.runner.schedule(0, self.stop)

    def stop(self):
        self.debug('stopping')
        self.debug('setting state to NULL')
        self._pipeline.set_state(gst.STATE_NULL)

        if not self._last:
            # see http://bugzilla.gnome.org/show_bug.cgi?id=578612
            print 'ERROR: not a single buffer gotten'
            raise
        else:
            self._checksum = self._checksum % 2 ** 32
            last = self._last.offset + len(self._last) / 4 - 1
            self.debug("last sample:", last)
            self.debug("frame length:", self._frameLength)
            self.debug("checksum: %08X" % self._checksum)
            self.debug("bytes: %d" % self._bytes)
            if self._frameEnd != last:
                print 'ERROR: did not get all frames, %d missing' % (
                    self._frameEnd - last)

        # publicize and stop
        self.checksum = self._checksum
        task.Task.stop(self)

class CRC32Task(ChecksumTask):
    """
    I do a simple CRC32 check.
    """
    def do_checksum_buffer(self, buffer, checksum):
        return zlib.crc32(buffer, checksum)

class AccurateRipChecksumTask(ChecksumTask):
    """
    I implement the AccurateRip checksum.

    See http://www.accuraterip.com/
    """
    def __init__(self, path, trackNumber, trackCount, frameStart=0, frameLength=-1):
        ChecksumTask.__init__(self, path, frameStart, frameLength)
        self._trackNumber = trackNumber
        self._trackCount = trackCount
        self._discFrameCounter = 0 # 1-based

    def do_checksum_buffer(self, buffer, checksum):
        self._discFrameCounter += 1

        # on first track ...
        if self._trackNumber == 1:
            # ... skip first 4 CD frames
            if self._discFrameCounter <= 4:
                gst.debug('skipping frame %d' % self._discFrameCounter)
                return checksum
            # ... on 5th frame, only use last value
            elif self._discFrameCounter == 5:
                values = struct.unpack("<I", buffer[-4:])
                checksum += SAMPLES_PER_FRAME * 5 * values[0]
                checksum &= 0xFFFFFFFF
                return checksum
 
        # on last track, skip last 5 CD frames
        if self._trackNumber == self._trackCount:
            discFrameLength = self._frameLength / SAMPLES_PER_FRAME
            if self._discFrameCounter > discFrameLength - 5:
                self.debug('skipping frame %d' % self._discFrameCounter)
                return checksum

        values = struct.unpack("<%dI" % (len(buffer) / 4), buffer)
        sum = 0
        for i, value in enumerate(values):
            # self._bytes is updated after do_checksum_buffer
            sum += (self._bytes / 4 + i + 1) * value
            sum &= 0xFFFFFFFF
            # offset = self._bytes / 4 + i + 1
            # if offset % SAMPLES_PER_FRAME == 0:
            #    print 'THOMAS: frame %d, ends before %d, last value %08x, CRC %08x' % (
            #        offset / SAMPLES_PER_FRAME, offset, value, sum)

        checksum += sum
        checksum &= 0xFFFFFFFF
        return checksum