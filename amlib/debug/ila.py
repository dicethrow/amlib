#
# This file was adapted from the LUNA project.
#
# Copyright (c) 2020 Great Scott Gadgets <info@greatscottgadgets.com>
# Copyright (c) 2021 Hans Baier <hansfbaier@gmail.com>
#
# SPDX-License-Identifier: BSD-3-Clause

""" Integrated logic analysis helpers. """

import os
import sys
import math
import tempfile
import pickle
import subprocess

from abc             import ABCMeta, abstractmethod

from amaranth          import Signal, Module, Cat, Elaboratable, Memory, DomainRenamer, Mux
from amaranth.hdl.ast  import Rose
from amaranth.lib.cdc  import FFSynchronizer
from amaranth.lib.fifo import SyncFIFOBuffered, AsyncFIFOBuffered
from vcd             import VCDWriter
from vcd.gtkw        import GTKWSave

from ..stream         import StreamInterface
from ..stream.uart    import UARTMultibyteTransmitter
from ..io.spi         import SPIDeviceInterface, SPIDeviceBus, SPIGatewareTestCase
from ..test.utils     import GatewareTestCase, sync_test_case


class IntegratedLogicAnalyzer(Elaboratable):
    """ Super-simple integrated-logic-analyzer generator class for LUNA.

    Attributes
    ----------
    enable: Signal(), input
        This input is only available if `with_enable` is True.
        Only samples with enable high will be captured.
    trigger: Signal(), input
        A strobe that determines when we should start sampling.
        Note that the sample at the same cycle as the trigger will
        be the first sample to be captured.
    capturing: Signal(), output
        Indicates that the trigger has occurred and sample memory
        is not yet full
    sampling: Signal(), output
        Indicates when data is being written into ILA memory

    complete: Signal(), output
        Indicates when sampling is complete and ready to be read.

    captured_sample_number: Signal(), input
        Selects which sample the ILA will output. Effectively the address for the ILA's
        sample buffer.
    captured_sample: Signal(), output
        The sample corresponding to the relevant sample number.
        Can be broken apart by using Cat(*signals).

    Parameters
    ----------
    signals: iterable of Signals
        An iterable of signals that should be captured by the ILA.
    sample_depth: int
        The depth of the desired buffer, in samples.

    domain: string
        The clock domain in which the ILA should operate.
    sample_rate: float
        Cosmetic indication of the sample rate. Used to format output.
    samples_pretrigger: int
        The number of our samples which should be captured _before_ the trigger.
        This also can act like an implicit synchronizer; so asynchronous inputs
        are allowed if this number is >= 1. Note that the trigger strobe is read
        on the rising edge of the clock.
    with_enable: bool
        This provides an 'enable' signal.
        Only samples with enable high will be captured.
    """

    def __init__(self, *, signals, sample_depth, domain="sync", sample_rate=60e6, samples_pretrigger=1, with_enable=False):
        self.domain             = domain
        self.signals            = signals
        self.inputs             = Cat(*signals)
        self.sample_width       = len(self.inputs)
        self.sample_depth       = sample_depth
        self.samples_pretrigger = samples_pretrigger
        self.sample_rate        = sample_rate
        self.sample_period      = 1 / sample_rate

        #
        # Create a backing store for our samples.
        #
        self.mem = Memory(width=self.sample_width, depth=sample_depth, name="ila_buffer")


        #
        # I/O port
        #
        self.with_enable = with_enable
        if with_enable:
            self.enable = Signal()

        self.trigger   = Signal()
        self.capturing = Signal()
        self.sampling  = Signal()
        self.complete  = Signal()

        self.captured_sample_number = Signal(range(0, self.sample_depth))
        self.captured_sample        = Signal(self.sample_width)


    def elaborate(self, platform):
        m  = Module()
        with_enable = self.with_enable

        # Memory ports.
        write_port = self.mem.write_port()
        read_port  = self.mem.read_port(domain='comb')
        m.submodules += [write_port, read_port]

        # If necessary, create synchronized versions of the relevant signals.
        if self.samples_pretrigger >= 1:
            synced_inputs  = Signal.like(self.inputs)
            delayed_inputs = Signal.like(self.inputs)

            # the first stage captures the trigger
            # the second stage the first pretrigger sample
            m.submodules.pretrigger_samples = \
                FFSynchronizer(self.inputs,  synced_inputs)
            if with_enable:
                synced_enable  = Signal()
                m.submodules.pretrigger_enable = \
                    FFSynchronizer(self.enable, synced_enable)

            if self.samples_pretrigger == 1:
                m.d.comb += delayed_inputs.eq(synced_inputs)
                if with_enable:
                    delayed_enable = Signal()
                    m.d.comb += delayed_enable.eq(synced_enable)
            else: # samples_pretrigger >= 2
                capture_fifo_width = self.sample_width
                if with_enable:
                    capture_fifo_width += 1

                pretrigger_fill_counter = Signal(range(self.samples_pretrigger * 2))
                pretrigger_filled       = Signal()
                m.d.comb += pretrigger_filled.eq(pretrigger_fill_counter >= (self.samples_pretrigger - 1))

                # fill up pretrigger FIFO with the number of pretrigger samples
                if (not with_enable):
                    synced_enable = 1
                with m.If(synced_enable & ~pretrigger_filled):
                    m.d.sync += pretrigger_fill_counter.eq(pretrigger_fill_counter + 1)

                m.submodules.pretrigger_fifo = pretrigger_fifo =  \
                    DomainRenamer(self.domain)(SyncFIFOBuffered(width=capture_fifo_width, depth=self.samples_pretrigger + 1))

                m.d.comb += [
                    pretrigger_fifo.w_data.eq(synced_inputs),
                    # We only want to capture enabled samples
                    # in the pretrigger period.
                    # Since we also capture the enable signal,
                    # we capture unconditionally after the pretrigger FIFO
                    # has been filled
                    pretrigger_fifo.w_en.eq(Mux(pretrigger_filled, 1, synced_enable)),
                    # buffer the specified number of pretrigger samples
                    pretrigger_fifo.r_en.eq(pretrigger_filled),

                    delayed_inputs.eq(pretrigger_fifo.r_data),
                ]

                if with_enable:
                    delayed_enable = Signal()
                    m.d.comb += [
                        pretrigger_fifo.w_data[-1].eq(synced_enable),
                        delayed_enable.eq(pretrigger_fifo.r_data[-1]),
                    ]

        else:
            delayed_inputs = Signal.like(self.inputs)
            m.d.sync += delayed_inputs.eq(self.inputs)
            if with_enable:
                delayed_enable = Signal()
                m.d.sync += delayed_enable.eq(self.enable)

        # Counter that keeps track of our write position.
        write_position = Signal(range(0, self.sample_depth))

        # Set up our write port to capture the input signals,
        # and our read port to provide the output.
        m.d.comb += [
            write_port.data        .eq(delayed_inputs),
            write_port.addr        .eq(write_position),

            self.captured_sample   .eq(read_port.data),
            read_port.addr         .eq(self.captured_sample_number)
        ]

        # Don't sample unless our FSM asserts our sample signal explicitly.
        sampling = Signal()
        m.d.comb += [
            write_port.en.eq(sampling),
            self.sampling.eq(sampling),
        ]

        with m.FSM(name="ila_fsm") as fsm:
            m.d.comb += self.capturing.eq(fsm.ongoing("CAPTURE"))

            # IDLE: wait for the trigger strobe
            with m.State('IDLE'):
                m.d.comb += sampling.eq(0)

                with m.If(self.trigger):
                    m.next = 'CAPTURE'

                    # Prepare to capture the first sample
                    m.d.sync += [
                        write_position .eq(0),
                        self.complete  .eq(0),
                    ]

            with m.State('CAPTURE'):
                enabled = delayed_enable if with_enable else 1
                m.d.comb += sampling.eq(enabled)

                with m.If(sampling):
                    m.d.sync += write_position .eq(write_position + 1)

                    # If this is the last sample, we're done. Finish up.
                    with m.If(write_position == (self.sample_depth - 1)):
                        m.d.sync += self.complete.eq(1)
                        m.next = "IDLE"

        # Convert our sync domain to the domain requested by the user, if necessary.
        if self.domain != "sync":
            m = DomainRenamer({"sync": self.domain})(m)

        return m


class IntegratedLogicAnalyzerTest(GatewareTestCase):
    def initialize_signals(self):
        yield self.input_a .eq(0)
        yield self.input_b .eq(0)
        yield self.input_c .eq(0)

    def provide_all_signals(self, value):
        all_signals = Cat(self.input_a, self.input_b, self.input_c)
        yield all_signals.eq(value)

    def assert_sample_value(self, address, value):
        """ Helper that asserts a ILA sample has a given value. """

        yield self.dut.captured_sample_number.eq(address)
        yield

        try:
            self.assertEqual((yield self.dut.captured_sample), value)
            return
        except AssertionError:
            pass

        # Generate an appropriate exception.
        actual_value = (yield self.dut.captured_sample)
        message = "assertion failed: at address 0x{:08x}: {:08x} != {:08x} (expected)".format(address, actual_value, value)
        raise AssertionError(message)

class IntegratedLogicAnalyzerBasicTest(IntegratedLogicAnalyzerTest):
    def instantiate_dut(self):
        self.input_a = Signal()
        self.input_b = Signal(30)
        self.input_c = Signal()

        return IntegratedLogicAnalyzer(
            signals=[self.input_a, self.input_b, self.input_c],
            sample_depth = 32,
            samples_pretrigger=0,
            with_enable=True
        )

    @sync_test_case
    def test_sampling(self):

        # Quick helper that generates simple, repetitive samples.
        def sample_value(i):
            return i | (i << 8) | (i << 16) | (0xFF << 24)

        yield self.dut.enable.eq(1)
        yield from self.provide_all_signals(0xDEADBEEF)
        yield

        # Before we trigger, we shouldn't be capturing any samples,
        # and we shouldn't be complete.
        self.assertEqual((yield self.dut.sampling), 0)
        self.assertEqual((yield self.dut.complete), 0)

        # Advance a bunch of cycles, and ensure we don't start sampling.
        yield from self.advance_cycles(10)
        self.assertEqual((yield self.dut.sampling), 0)

        # Set a new piece of data for a couple of cycles.
        yield from self.provide_all_signals(0x01234567)
        yield
        yield from self.provide_all_signals(0x89ABCDEF)
        yield

        # Finally, trigger the capture.
        yield from self.provide_all_signals(sample_value(0))
        yield from self.pulse(self.dut.trigger, step_after=False)

        yield from self.provide_all_signals(sample_value(1))
        yield

        # After we pulse our trigger strobe, we should be sampling.
        self.assertEqual((yield self.dut.sampling), 1)

        # Populate the memory with a variety of interesting signals;
        # and continue afterwards for a couple of cycles to make sure
        # these don't make it into our sample buffer.
        for i in range(2, 32 + 32):
            # after the first two samples above, we only sample every
            # other sample
            yield self.dut.enable.eq(i % 2 == 0)
            yield from self.provide_all_signals(sample_value(i))
            yield

        # We now should be done with our sampling.
        self.assertEqual((yield self.dut.sampling), 0)
        self.assertEqual((yield self.dut.complete), 1)

        yield from self.assert_sample_value(0, sample_value(0))
        yield from self.assert_sample_value(1, sample_value(1))

        # Validate the memory values after the first two samples
        # were captured are the even samples
        for i in range(2, 32):
            yield from self.assert_sample_value(i, sample_value((i - 1) * 2))

        # All of those reads shouldn't change our completeness.
        self.assertEqual((yield self.dut.sampling), 0)
        self.assertEqual((yield self.dut.complete), 1)


class IntegratedLogicAnalyzerPretriggerTest(IntegratedLogicAnalyzerTest):
    PRETRIGGER_SAMPLES = 8

    def instantiate_dut(self):
        self.input_a = Signal()
        self.input_b = Signal(30)
        self.input_c = Signal()

        return IntegratedLogicAnalyzer(
            signals=[self.input_a, self.input_b, self.input_c],
            sample_depth = 32,
            samples_pretrigger=self.PRETRIGGER_SAMPLES,
            with_enable=True
        )

    @sync_test_case
    def test_sampling(self):

        # Quick helper that generates simple, repetitive samples.
        def sample_value(i):
            return i | (i << 8) | (i << 16) | (0xFF << 24)

        yield self.dut.enable.eq(1)
        yield from self.provide_all_signals(0xDEADBEEF)
        yield

        # Before we trigger, we shouldn't be capturing any samples,
        # and we shouldn't be complete.
        self.assertEqual((yield self.dut.sampling), 0)
        self.assertEqual((yield self.dut.complete), 0)

        # Advance a bunch of cycles, and ensure we don't start sampling.
        yield from self.advance_cycles(2 + self.PRETRIGGER_SAMPLES)
        self.assertEqual((yield self.dut.sampling), 0)

        # Set a new piece of data for a couple of cycles.
        yield from self.provide_all_signals(0x01234567)
        yield
        yield from self.provide_all_signals(0x89ABCDEF)
        yield

        # Finally, trigger the capture.
        yield from self.provide_all_signals(sample_value(0))
        yield from self.pulse(self.dut.trigger, step_after=False)

        yield from self.provide_all_signals(sample_value(1))
        yield

        # After we pulse our trigger strobe, we should be sampling.
        self.assertEqual((yield self.dut.sampling), 1)

        # Populate the memory with a variety of interesting signals;
        # and continue afterwards for a couple of cycles to make sure
        # these don't make it into our sample buffer.
        for i in range(2, 32 + 32):
            # after the first two samples above, we only sample every
            # other sample
            yield self.dut.enable.eq(i % 2 == 0)
            yield from self.provide_all_signals(sample_value(i))
            yield

        # We now should be done with our sampling.
        self.assertEqual((yield self.dut.sampling), 0)
        self.assertEqual((yield self.dut.complete), 1)

        for n in range(self.PRETRIGGER_SAMPLES - 2):
            yield from self.assert_sample_value(n, 0xDEADBEEF)

        yield from self.assert_sample_value(self.PRETRIGGER_SAMPLES - 2, 0x01234567)
        yield from self.assert_sample_value(self.PRETRIGGER_SAMPLES - 1, 0x89ABCDEF)

        yield from self.assert_sample_value(self.PRETRIGGER_SAMPLES,     sample_value(0))
        yield from self.assert_sample_value(self.PRETRIGGER_SAMPLES + 1, sample_value(1))

        # Validate the memory values after the first two samples
        # were captured are the even samples
        for i in range(1, 23):
            yield from self.assert_sample_value(self.PRETRIGGER_SAMPLES + 1 + i, sample_value(i * 2))

        # All of those reads shouldn't change our completeness.
        self.assertEqual((yield self.dut.sampling), 0)
        self.assertEqual((yield self.dut.complete), 1)


class SyncSerialILA(Elaboratable):
    """ Super-simple ILA that reads samples out over a simple unidirectional SPI.
    Create a receiver for this object by calling apollo_fpga.ila_receiver_for(<this>).

    This protocol is simple: every time CS goes low, we begin sending out a bit of
    sample on each rising edge. Once a new sample is complete, the next sample begins
    on the next 32-bit boundary.

    Attributes
    ----------
    enable: Signal(), input
        This input is only available if `with_enable` is True.
        Only samples with enable high will be captured.
    trigger: Signal(), input
        A strobe that determines when we should start sampling.
    capturing: Signal(), output
        Indicates that the trigger has occurred and sample memory
        is not yet full
    sampling: Signal(), output
        Indicates when data is being written into ILA memory
    complete: Signal(), output
        Indicates when sampling is complete and ready to be read.

    sck: Signal(), input
        Serial clock for the SPI lines.
    sdo: Signal(), output
        Serial data out for the SPI lines.
    cs: Signal(), input
        Chip select for the SPI lines.

    Parameters
    ----------
    signals: iterable of Signals
        An iterable of signals that should be captured by the ILA.
    sample_depth: int
        The depth of the desired buffer, in samples.

    domain: string
        The clock domain in which the ILA should operate.
    samples_pretrigger: int
        The number of our samples which should be captured _before_ the trigger.
                                  This also can act like an implicit synchronizer; so asynchronous inputs
                                  are allowed if this number is >= 2.

    clock_polarity: int, 0 or 1
        Clock polarity for the output SPI transciever. Optional.
    clock_phase: int, 0 or 1
        Clock phase for the output SPI transciever. Optional.
    cs_idles_high: bool, optional
        If True, the CS line will be assumed to be asserted when cs=0.
        If False or not provided, the CS line will be assumed to be asserted when cs=1.
        This can be used to share a simple two-device SPI bus, so two internal endpoints
        can use the same CS line, with two opposite polarities.

    with_enable: bool
        This provides an 'enable' signal.
        Only samples with enable high will be captured.
    """

    def __init__(self, *, signals, sample_depth, clock_polarity=0, clock_phase=1, cs_idles_high=False, **kwargs):

        #
        # I/O port
        #
        self.spi = SPIDeviceBus()

        #
        # Init
        #

        self.clock_phase = clock_phase
        self.clock_polarity = clock_polarity

        # Extract the domain from our keyword arguments, and then translate it to sync
        # before we pass it back below. We'll use a DomainRenamer at the boundary to
        # handle non-sync domains.
        self.domain = kwargs.get('domain', 'sync')
        kwargs['domain'] = 'sync'

        # Create our core integrated logic analyzer.
        self.ila = IntegratedLogicAnalyzer(
            signals=signals,
            sample_depth=sample_depth,
            **kwargs)

        # Copy some core parameters from our inner ILA.
        self.signals       = signals
        self.sample_width  = self.ila.sample_width
        self.sample_depth  = self.ila.sample_depth
        self.sample_rate   = self.ila.sample_rate
        self.sample_period = self.ila.sample_period

        if kwargs.get('with_enable'):
            self.enable = self.ila.enable

        # Figure out how many bytes we'll send per sample.
        # We'll always send things squished into 32-bit chunks, as this is what the SPI engine
        # on our Debug Controller likes most.
        words_per_sample = (self.ila.sample_width + 31) // 32

        # Bolster our bits_per_word up to a power of two...
        self.bits_per_sample = words_per_sample * 4 * 8
        self.bits_per_sample = 2 ** ((self.bits_per_sample - 1).bit_length())

        # ... and compute how many bits should be used.
        self.bytes_per_sample = self.bits_per_sample // 8

        # Expose our ILA's trigger and status ports directly.
        self.trigger   = self.ila.trigger
        self.capturing = self.ila.capturing
        self.sampling  = self.ila.sampling
        self.complete  = self.ila.complete


    def elaborate(self, platform):
        m  = Module()
        m.submodules.ila = self.ila

        transaction_start = Rose(self.spi.cs)

        # Connect up our SPI transciever to our public interface.
        interface = SPIDeviceInterface(
            word_size=self.bits_per_sample,
            clock_polarity=self.clock_polarity,
            clock_phase=self.clock_phase
        )
        m.submodules.spi = interface
        m.d.comb += [
            interface.spi      .connect(self.spi),

            # Always output the captured sample.
            interface.word_out .eq(self.ila.captured_sample)
        ]

        # Count where we are in the current transmission.
        current_sample_number = Signal(range(0, self.ila.sample_depth))

        # Our first piece of data is latched in when the transaction
        # starts, so we'll move on to sample #1.
        with m.If(self.spi.cs):
            with m.If(transaction_start):
                m.d.sync += current_sample_number.eq(1)

            # From then on, we'll move to the next sample whenever we're finished
            # scanning out a word (and thus our current samples are latched in).
            with m.Elif(interface.word_accepted):
                m.d.sync += current_sample_number.eq(current_sample_number + 1)

        # Whenever CS is low, we should be providing the very first sample,
        # so reset our sample counter to 0.
        with m.Else():
            m.d.sync += current_sample_number.eq(0)


        # Ensure our ILA module outputs the right sample.
        m.d.sync += [
            self.ila.captured_sample_number .eq(current_sample_number)
        ]

        # Convert our sync domain to the domain requested by the user, if necessary.
        if self.domain != "sync":
            m = DomainRenamer({"sync": self.domain})(m)

        return m


class SyncSerialReadoutILATest(SPIGatewareTestCase):

    def instantiate_dut(self):
        self.input_signal = Signal(12)
        return SyncSerialILA(
            signals=[self.input_signal],
            sample_depth=16,
            clock_polarity=1,
            clock_phase=0
        )

    def initialize_signals(self):
        yield self.input_signal.eq(0xF00)

    @sync_test_case
    def test_spi_readout(self):
        input_signal = self.input_signal

        # Trigger the test while offering our first sample.
        yield
        yield from self.pulse(self.dut.trigger, step_after=False)

        # Provide the remainder of our samples.
        for i in range(1, 16):
            yield input_signal.eq(0xF00 | i)
            yield

        # Wait a few cycles to account for delays in
        # the sampling pipeline.
        yield from self.advance_cycles(5)

        # We've now captured a full set of samples.
        # We'll test reading them out.
        self.assertEqual((yield self.dut.complete), 1)

        # Start the transaction, and exchange 16 bytes of data.
        yield self.dut.spi.cs.eq(1)
        yield

        # Read our our result over SPI...
        data = yield from self.spi_exchange_data(b"\0" * 32)

        # ... and ensure it matches what was sampled.
        i = 0
        while data:
            datum = data[0:4]
            del data[0:4]

            expected = b"\x00\x00\x0f" + bytes([i])
            self.assertEqual(datum, expected)
            i += 1




class StreamILA(Elaboratable):
    """ Super-simple ILA that outputs its samples over a Stream.
    Create a receiver for this object by calling apollo.ila_receiver_for(<this>).

    This protocol is simple: we wait for a trigger; and then broadcast our samples.
    We broadcast one buffer of samples per each subsequent trigger.

    Attributes
    ----------
    enable: Signal(), input
        This input is only available if `with_enable` is True.
        Only samples with enable high will be captured.
    trigger: Signal(), input
        A strobe that determines when we should start sampling.
    capturing: Signal(), output
        Indicates that the trigger has occurred and sample memory
        is not yet full
    sampling: Signal(), output
        Indicates when data is being written into ILA memory
    complete: Signal(), output
        Indicates when sampling is complete and ready to be read.

    stream: output stream
        Stream output for the ILA.

    Parameters
    ----------
    signals: iterable of Signals
        An iterable of signals that should be captured by the ILA.
    sample_depth: int
        The depth of the desired buffer, in samples.

    domain: string
        The clock domain in which the ILA should operate.
    o_domain: string
        The clock domain in which the output stream will be generated.
        If omitted, defaults to the same domain as the core ILA.
    samples_pretrigger: int
        The number of our samples which should be captured _before_ the trigger.
        This also can act like an implicit synchronizer; so asynchronous inputs
        are allowed if this number is >= 2.

    with_enable: bool
        This provides an 'enable' signal.
        Only samples with enable high will be captured.
    """

    def __init__(self, *, signals, sample_depth, o_domain=None, **kwargs):
        # Extract the domain from our keyword arguments, and then translate it to sync
        # before we pass it back below. We'll use a DomainRenamer at the boundary to
        # handle non-sync domains.
        self.domain = kwargs.get('domain', 'sync')
        kwargs['domain'] = 'sync'

        self._o_domain = o_domain if o_domain else self.domain

        # Create our core integrated logic analyzer.
        self.ila = IntegratedLogicAnalyzer(
            signals=signals,
            sample_depth=sample_depth,
            **kwargs)

        # Copy some core parameters from our inner ILA.
        self.signals       = signals
        self.sample_width  = self.ila.sample_width
        self.sample_depth  = self.ila.sample_depth
        self.sample_rate   = self.ila.sample_rate
        self.sample_period = self.ila.sample_period

        if kwargs.get('with_enable'):
            self.enable = self.ila.enable

        # Bolster our bits per sample "word" up to a power of two.
        self.bits_per_sample = 2 ** ((self.ila.sample_width - 1).bit_length())
        self.bytes_per_sample = self.bits_per_sample // 8

        #
        # I/O port
        #
        self.stream  = StreamInterface(payload_width=self.bits_per_sample)
        self.trigger = Signal()

        # Expose our ILA's trigger and status ports directly.
        self.capturing = self.ila.capturing
        self.sampling  = self.ila.sampling
        self.complete  = self.ila.complete


    def elaborate(self, platform):
        m  = Module()
        m.submodules.ila = ila = self.ila

        if self._o_domain == self.domain:
            in_domain_stream = self.stream
        else:
            in_domain_stream = StreamInterface(payload_width=self.bits_per_sample)

        # Count where we are in the current transmission.
        current_sample_number = Signal(range(0, ila.sample_depth))

        # Always present the current sample number to our ILA, and the current
        # sample value to the UART.
        m.d.comb += [
            ila.captured_sample_number  .eq(current_sample_number),
            in_domain_stream.payload    .eq(ila.captured_sample)
        ]

        with m.FSM():

            # IDLE -- we're currently waiting for a trigger before capturing samples.
            with m.State("IDLE"):

                # Always allow triggering, as we're ready for the data.
                m.d.comb += self.ila.trigger.eq(self.trigger)

                # Once we're triggered, move onto the SAMPLING state.
                with m.If(self.trigger):
                    m.next = "SAMPLING"


            # SAMPLING -- the internal ILA is sampling; we're now waiting for it to
            # complete. This state is similar to IDLE; except we block triggers in order
            # to cleanly avoid a race condition.
            with m.State("SAMPLING"):

                # Once our ILA has finished sampling, prepare to read out our samples.
                with m.If(self.ila.complete):
                    m.d.sync += [
                        current_sample_number  .eq(0),
                        in_domain_stream.first      .eq(1)
                    ]
                    m.next = "SENDING"


            # SENDING -- we now have a valid buffer of samples to send up to the host;
            # we'll transmit them over our stream interface.
            with m.State("SENDING"):
                m.d.comb += [
                    # While we're sending, we're always providing valid data to the UART.
                    in_domain_stream.valid  .eq(1),

                    # Indicate when we're on the last sample.
                    in_domain_stream.last   .eq(current_sample_number == (self.sample_depth - 1))
                ]

                # Each time the UART accepts a valid word, move on to the next one.
                with m.If(in_domain_stream.ready):
                    m.d.sync += [
                        current_sample_number .eq(current_sample_number + 1),
                        in_domain_stream.first     .eq(0)
                    ]

                    # If this was the last sample, we're done! Move back to idle.
                    with m.If(self.stream.last):
                        m.next = "IDLE"


        # If we're not streaming out of the same domain we're capturing from,
        # we'll add some clock-domain crossing hardware.
        if self._o_domain != self.domain:
            in_domain_signals  = Cat(
                in_domain_stream.first,
                in_domain_stream.payload,
                in_domain_stream.last
            )
            out_domain_signals = Cat(
                self.stream.first,
                self.stream.payload,
                self.stream.last
            )

            # Create our async FIFO...
            m.submodules.cdc = fifo = AsyncFIFOBuffered(
                width=len(in_domain_signals),
                depth=16,
                w_domain="sync",
                r_domain=self._o_domain
            )

            m.d.comb += [
                # ... fill it from our in-domain stream...
                fifo.w_data             .eq(in_domain_signals),
                fifo.w_en               .eq(in_domain_stream.valid),
                in_domain_stream.ready  .eq(fifo.w_rdy),

                # ... and output it into our outupt stream.
                out_domain_signals      .eq(fifo.r_data),
                self.stream.valid       .eq(fifo.r_rdy),
                fifo.r_en               .eq(self.stream.ready)
            ]

        # Convert our sync domain to the domain requested by the user, if necessary.
        if self.domain != "sync":
            m = DomainRenamer({"sync": self.domain})(m)

        return m



class AsyncSerialILA(Elaboratable):
    """ Super-simple ILA that reads samples out over a UART connection.
    Create a receiver for this object by calling apollo_fpga.ila_receiver_for(<this>).

    This protocol is simple: we wait for a trigger; and then broadcast our samples.
    We broadcast one buffer of samples per each subsequent trigger.

    Attributes
    ----------
    enable: Signal(), input
        This input is only available if `with_enable` is True.
        Only samples with enable high will be captured.
    trigger: Signal(), input
        A strobe that determines when we should start sampling.
    capturing: Signal(), output
        Indicates that the trigger has occurred and sample memory
        is not yet full
    sampling: Signal(), output
        Indicates when data is being written into ILA memory
    complete: Signal(), output
        Indicates when sampling is complete and ready to be read.

    tx: Signal(), output
        Serial output for the ILA.

    Parameters
    ----------
    signals: iterable of Signals
        An iterable of signals that should be captured by the ILA.
    sample_depth: int
        The depth of the desired buffer, in samples.

    divisor: int
        The number of `sync` clock cycles per bit period.

    domain: string
        The clock domain in which the ILA should operate.
    samples_pretrigger: int
        The number of our samples which should be captured _before_ the trigger.
        This also can act like an implicit synchronizer; so asynchronous inputs
        are allowed if this number is >= 2.

    with_enable: bool
        This provides an 'enable' signal.
        Only samples with enable high will be captured.
    """

    def __init__(self, *, signals, sample_depth, divisor, **kwargs):
        self.divisor = divisor

        #
        # I/O port
        #
        self.tx      = Signal()

        # Extract the domain from our keyword arguments, and then translate it to sync
        # before we pass it back below. We'll use a DomainRenamer at the boundary to
        # handle non-sync domains.
        self.domain = kwargs.get('domain', 'sync')
        kwargs['domain'] = 'sync'

        # Create our core integrated logic analyzer.
        self.ila = StreamILA(
            signals=signals,
            sample_depth=sample_depth,
            **kwargs)

        # Copy some core parameters from our inner ILA.
        self.signals          = signals
        self.sample_width     = self.ila.sample_width
        self.sample_depth     = self.ila.sample_depth
        self.sample_rate      = self.ila.sample_rate
        self.sample_period    = self.ila.sample_period
        self.bits_per_sample  = self.ila.bits_per_sample
        self.bytes_per_sample = self.ila.bytes_per_sample

        if kwargs.get('with_enable'):
            self.enable = self.ila.enable

        # Expose our ILA's trigger and status ports directly.
        self.trigger   = self.ila.trigger
        self.capturing = self.ila.capturing
        self.sampling  = self.ila.sampling
        self.complete  = self.ila.complete


    def elaborate(self, platform):
        m  = Module()
        m.submodules.ila = ila = self.ila

        # Create our UART transmitter, and connect it to our stream interface.
        m.submodules.uart = uart = UARTMultibyteTransmitter(
            byte_width=self.bytes_per_sample,
            divisor=self.divisor
        )
        m.d.comb +=[
            uart.stream  .stream_eq(ila.stream),
            self.tx      .eq(uart.tx)
        ]


        # Convert our sync domain to the domain requested by the user, if necessary.
        if self.domain != "sync":
            m = DomainRenamer({"sync": self.domain})(m)

        return m

class ILACoreParameters:
    """ This Class is needed to pickle the core parameters of an ILA.
        This makes it possible to run the frontend in a different python script
    """
    def __init__(self, ila) -> None:
        self.signals          = ila.signals
        self.sample_width     = ila.sample_width
        self.sample_depth     = ila.sample_depth
        self.sample_rate      = ila.sample_rate
        self.sample_period    = ila.sample_period
        self.bits_per_sample  = ila.bits_per_sample
        self.bytes_per_sample = ila.bytes_per_sample

    def pickle(self, filename="ila.P"):
        pickle.dump(self, open(filename, "wb"))

    @staticmethod
    def unpickle(filename="ila.P"):
        ila_core_parameters = pickle.load(open(filename, "rb"))
        return ILACoreParameters(ila_core_parameters)

class ILAFrontend(metaclass=ABCMeta):
    """ Class that communicates with an ILA module and emits useful output. """

    def __init__(self, ila):
        """
        Parameters:
            ila -- The ILA object to work with.
        """
        self.ila = ila
        self.samples = None


    @abstractmethod
    def _read_samples(self):
        """ Read samples from the target ILA. Should return an iterable of samples. """


    def _parse_sample(self, raw_sample):
        """ Converts a single binary sample to a dictionary of names -> sample values. """

        position = 0
        sample   = {}

        # Split our raw, bits(0) signal into smaller slices, and associate them with their names.
        for signal in self.ila.signals:
            signal_width = len(signal)
            signal_bits  = raw_sample[position : position + signal_width]
            position += signal_width

            sample[signal.name] = signal_bits

        return sample


    def _parse_samples(self, raw_samples):
        """ Converts raw, binary samples to dictionaries of name -> sample. """
        return [self._parse_sample(sample) for sample in raw_samples]


    def refresh(self):
        """ Fetches the latest set of samples from the target ILA. """
        self.samples = self._parse_samples(self._read_samples())


    def enumerate_samples(self):
        """ Returns an iterator that returns pairs of (timestamp, sample). """

        # If we don't have any samples, fetch samples from the ILA.
        if self.samples is None:
            self.refresh()

        timestamp = 0

        # Iterate over each sample...
        for sample in self.samples:
            yield timestamp, sample

            # ... and advance the timestamp by the relevant interval.
            timestamp += self.ila.sample_period


    def print_samples(self):
        """ Simple method that prints each of our samples; for simple CLI debugging."""

        for timestamp, sample in self.enumerate_samples():
            timestamp_scaled = 1000000 * timestamp
            print(f"{timestamp_scaled:08f}us: {sample}")



    def emit_vcd(self, filename, *, gtkw_filename=None, add_clock=True):
        """ Emits a VCD file containing the ILA samples.

        Parameters:
            filename      -- The filename to write to, or '-' to write to stdout.
            gtkw_filename -- If provided, a gtkwave save file will be generated that
                             automatically displays all of the relevant signals in the
                             order provided to the ILA.
            add_clock     -- If true or not provided, adds a replica of the ILA's sample
                             clock to make change points easier to see.
        """

        # Select the file-like object we're working with.
        if filename == "-":
            stream = sys.stdout
            close_after = False
        else:
            stream = open(filename, 'w')
            close_after = True

        # Create our basic VCD.
        with VCDWriter(stream, timescale=f"1 ns", date='today') as writer:
            first_timestamp = math.inf
            last_timestamp  = 0

            signals = {}

            # If we're adding a clock...
            if add_clock:
                clock_value  = 1
                clock_signal = writer.register_var('ila', 'ila_clock', 'integer', size=1, init=clock_value ^ 1)

            # Create named values for each of our signals.
            for signal in self.ila.signals:
                signals[signal.name] = writer.register_var('ila', signal.name, 'integer', size=len(signal))

            # Dump the each of our samples into the VCD.
            clock_time = 0
            for timestamp, sample in self.enumerate_samples():
                for signal_name, signal_value in sample.items():

                    # If we're adding a clock signal, add any changes necessary since
                    # the last value-change.
                    if add_clock:
                        while clock_time < timestamp:
                            writer.change(clock_signal, clock_time / 1e-9, clock_value)

                            clock_value ^= 1
                            clock_time  += (self.ila.sample_period / 2)

                    # Register the signal change.
                    writer.change(signals[signal_name], timestamp / 1e-9, signal_value.to_int())


        # If we're generating a GTKW, delegate that to our helper function.
        if gtkw_filename:
            assert(filename != '-')
            self._emit_gtkw(gtkw_filename, filename, add_clock=add_clock)


    def _emit_gtkw(self, filename, dump_filename, *, add_clock=True):
        """ Emits a GTKWave save file to accompany a generated VCD.

        Parameters:
            filename      -- The filename to write the GTKW save to.
            dump_filename -- The filename of the VCD that should be opened with this save.
            add_clock     -- True iff a clock signal should be added to the GTKW save.
        """

        with open(filename, 'w') as f:
            gtkw = GTKWSave(f)

            # Comments / context.
            gtkw.comment("Generated by the amaranth-library internal logic analyzer.")

            # Add a reference to the dumpfile we're working with.
            gtkw.dumpfile(dump_filename)

            # If we're adding a clock, add it to the top of the view.
            gtkw.trace('ila.ila_clock')

            # Gain more screen space by collapsing the signal tree
            gtkw.sst_expanded(False)

            # Zoom out quite a bit. We want to start with the big picture
            gtkw.zoom_markers(zoom=-11.0)

            # create enough space in the signal names pane to have the signal
            # values visible
            gtkw.signals_width(500)

            # Add each of our signals to the file.
            for signal in self.ila.signals:
                gtkw.trace(f"ila.{signal.name}")


    def interactive_display(self, *, add_clock=True):
        """ Attempts to spawn a GTKWave instance to display the ILA results interactively. """

        # Hack: generate files in a way that doesn't trip macOS's fancy guards.
        try:
            vcd_filename = os.path.join(tempfile.gettempdir(), os.urandom(24).hex() + '.vcd')
            gtkw_filename = os.path.join(tempfile.gettempdir(), os.urandom(24).hex() + '.gtkw')

            self.emit_vcd(vcd_filename, gtkw_filename=gtkw_filename)
            subprocess.run(["gtkwave", "-f", vcd_filename, "-a", gtkw_filename])
        finally:
            os.remove(vcd_filename)
            os.remove(gtkw_filename)


class AsyncSerialILAFrontend(ILAFrontend):
    """ UART-based ILA transport.

    Parameters
    ------------
    port: string
        The serial port to use to connect. This is typically a path on *nix systems.
    ila: IntegratedLogicAnalyzer
        The ILA object to work with.
    """

    def __init__(self, *args, ila, **kwargs):
        import serial

        self._port = serial.Serial(*args, **kwargs)
        self._port.reset_input_buffer()

        super().__init__(ila)


    def _split_samples(self, all_samples):
        """ Returns an iterator that iterates over each sample in the raw binary of samples. """
        from ..utils.bits import bits

        sample_width_bytes = self.ila.bytes_per_sample

        # Iterate over each sample, and yield its value as a bits object.
        for i in range(0, len(all_samples), sample_width_bytes):
            raw_sample    = all_samples[i:i + sample_width_bytes]
            sample_length = len(Cat(self.ila.signals))

            yield bits.from_bytes(raw_sample, length=sample_length, byteorder='big')


    def _read_samples(self):
        """ Reads a set of ILA samples, and returns them. """

        sample_width_bytes = self.ila.bytes_per_sample
        total_to_read      = self.ila.sample_depth * sample_width_bytes

        # Fetch all of our samples from the given device.
        # TODO: figure out why the bytes to read sometimes
        # are greater than the total.
        # in that case we would get and Overflow error.
        # If we make instead the bytes to read larger than
        # the actual file the read will timeout but still
        # return a correct, full trace
        all_samples = self._port.read(2 * total_to_read)
        return list(self._split_samples(all_samples))