"""WASAPI capture through ctypes: microphones and system (loopback) sound.

Qt can record a microphone but it cannot record what the speakers are playing, so this
module talks to WASAPI directly. Only shared-mode capture is used, which is the mode every
Windows program can rely on: the endpoint's mix format is what the audio engine delivers,
so no exclusive-mode negotiation or device-side conversion is needed.

Threading rules: a capture thread initialises COM itself and creates, uses and releases
every interface on that thread. The GUI thread only ever calls module helpers that open
their own short-lived COM session and return plain strings, so no apartment is shared.
"""

import ctypes

_ole32 = ctypes.windll.ole32

CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

DEVICE_STATE_ACTIVE = 0x1
DEVICE_STATE_DISABLED = 0x2
DEVICE_STATE_NOTPRESENT = 0x4

E_RENDER = 0
E_CAPTURE = 1

AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1

AUDCLNT_E_DEVICE_INVALIDATED = -2004287480   # 0x88890004
AUDCLNT_S_BUFFER_EMPTY = 0x08890001

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IMMDevice = "{D666063F-1587-4E43-81F1-B948E807363F}"
IID_IAudioClient = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"
IID_IPropertyStore = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
PKEY_Device_FriendlyName = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 14)

SUBTYPE_PCM = "{00000001-0000-0010-8000-00AA00389B71}"
SUBTYPE_FLOAT = "{00000003-0000-0010-8000-00AA00389B71}"

STG_E_ACCESSDENIED = -2147287035


class WasapiError(RuntimeError):
    """A WASAPI call failed; `hr` keeps the HRESULT for diagnosis."""

    def __init__(self, message, hr=None):
        super().__init__(message)
        self.hr = hr


def _hresult_text(hr):
    return f"0x{hr & 0xFFFFFFFF:08X}"


class GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_ulong), ("data2", ctypes.c_ushort),
                ("data3", ctypes.c_ushort), ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        text = text.strip("{}")
        a, b, c, d, e = text.split("-")
        return cls(int(a, 16), int(b, 16), int(c, 16),
                   (ctypes.c_ubyte * 8)(int(d[0:2], 16), int(d[2:4], 16),
                                        *[int(e[i:i + 2], 16) for i in range(0, 12, 2)]))

    def text(self):
        return (f"{{{self.data1:08X}-{self.data2:04X}-{self.data3:04X}-"
                f"{self.data4[0]:02X}{self.data4[1]:02X}-"
                + "".join(f"{self.data4[i]:02X}" for i in range(2, 8)) + "}")


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_ulong), ("nAvgBytesPerSec", ctypes.c_ulong),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


def _fn(vtable, index, restype, *argtypes):
    """One vtable slot as a callable; `self` is passed as the first argument."""
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])


def _check(hr, what):
    if hr < 0:
        raise WasapiError(f"{what} failed ({_hresult_text(hr)})", hr)
    return hr


def com_initialize():
    """Enter a COM apartment for this thread; safe to call when one already exists."""
    hr = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if hr < 0 and hr != RPC_E_CHANGED_MODE:
        raise WasapiError(f"COM could not start ({_hresult_text(hr)})", hr)


class _Interface:
    """Shared COM plumbing: keep the pointer, expose vtable calls, release on close."""

    def __init__(self, ptr):
        self.ptr = ctypes.c_void_p(ptr)
        self._vtable = ctypes.cast(self.ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]

    def call(self, index, restype, *argtypes):
        return _fn(self._vtable, index, restype, *argtypes)

    def release(self):
        if self.ptr:
            self.call(2, ctypes.c_ulong)(self.ptr)
            self.ptr = ctypes.c_void_p()

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


class IMMDevice(_Interface):
    def get_id(self):
        out = ctypes.c_void_p()
        _check(self.call(5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(self.ptr, ctypes.byref(out)),
               "GetId")
        try:
            return ctypes.wstring_at(out.value)
        finally:
            _ole32.CoTaskMemFree(out)

    def get_state(self):
        out = ctypes.c_ulong()
        _check(self.call(6, ctypes.c_long, ctypes.POINTER(ctypes.c_ulong))(self.ptr, ctypes.byref(out)),
               "GetState")
        return int(out.value)

    def activate(self, iid):
        out = ctypes.c_void_p()
        iid_value = GUID.parse(iid)
        _check(self.call(3, ctypes.c_long, ctypes.POINTER(GUID), ctypes.c_ulong, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p))(
            self.ptr, ctypes.byref(iid_value), CLSCTX_ALL, None, ctypes.byref(out)),
            "Activate")
        return out.value

    def friendly_name(self):
        store = ctypes.c_void_p()
        hr = self.call(4, ctypes.c_long, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))(
            self.ptr, 0, ctypes.byref(store))
        if hr < 0:
            return ""
        prop = _Interface(store.value)
        try:
            key = PROPERTYKEY(GUID.parse(PKEY_Device_FriendlyName[0]), PKEY_Device_FriendlyName[1])
            value = (ctypes.c_byte * 32)()      # PROPVARIANT is 24 bytes; keep room
            hr = prop.call(5, ctypes.c_long, ctypes.POINTER(PROPERTYKEY), ctypes.c_void_p)(
                prop.ptr, ctypes.byref(key), ctypes.byref(value))
            if hr < 0:
                return ""
            vartype = ctypes.c_ushort.from_buffer(value, 0).value
            if vartype != 31:                    # VT_LPWSTR
                return ""
            text = ctypes.c_void_p.from_buffer(value, 8).value
            if not text:
                return ""
            try:
                return ctypes.wstring_at(text)
            finally:
                _ole32.PropVariantClear(ctypes.byref(value))
        finally:
            prop.release()


class IMMDeviceEnumerator(_Interface):
    def endpoints(self, flow, state_mask=DEVICE_STATE_ACTIVE):
        out = ctypes.c_void_p()
        _check(self.call(3, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                         ctypes.POINTER(ctypes.c_void_p))(
            self.ptr, flow, state_mask, ctypes.byref(out)), "EnumAudioEndpoints")
        collection = _Interface(out.value)
        try:
            count = ctypes.c_ulong()
            _check(collection.call(3, ctypes.c_long, ctypes.POINTER(ctypes.c_ulong))(
                collection.ptr, ctypes.byref(count)), "GetCount")
            devices = []
            for index in range(int(count.value)):
                dev = ctypes.c_void_p()
                hr = collection.call(4, ctypes.c_long, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))(
                    collection.ptr, index, ctypes.byref(dev))
                if hr >= 0 and dev.value:
                    devices.append(IMMDevice(dev.value))
            return devices
        finally:
            collection.release()

    def get_device(self, device_id):
        out = ctypes.c_void_p()
        _check(self.call(5, ctypes.c_long, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p))(
            self.ptr, device_id, ctypes.byref(out)), "GetDevice")
        return IMMDevice(out.value)

    def default_endpoint(self, flow, role=0):
        out = ctypes.c_void_p()
        _check(self.call(4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                         ctypes.POINTER(ctypes.c_void_p))(
            self.ptr, flow, role, ctypes.byref(out)), "GetDefaultAudioEndpoint")
        return IMMDevice(out.value)


def _enumerator():
    out = ctypes.c_void_p()
    clsid, iid = GUID.parse(CLSID_MMDeviceEnumerator), GUID.parse(IID_IMMDeviceEnumerator)
    _check(_ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL, ctypes.byref(iid),
                                   ctypes.byref(out)), "CoCreateInstance(MMDeviceEnumerator)")
    return IMMDeviceEnumerator(out.value)


class EndpointInfo:
    """One audio endpoint as the UI needs it: a stable id, a label and its direction."""

    __slots__ = ("id", "name", "loopback")

    def __init__(self, device_id, name, loopback):
        self.id, self.name, self.loopback = str(device_id), str(name or "Audio device"), bool(loopback)

    @property
    def label(self):
        return self.name

    def __repr__(self):
        return f"EndpointInfo({self.name!r}, loopback={self.loopback})"

    def __eq__(self, other):
        return isinstance(other, EndpointInfo) and (self.id, self.loopback) == (other.id, other.loopback)

    def __hash__(self):
        return hash((self.id, self.loopback))


def list_endpoints(loopback=False, refresh=False):
    """Capture endpoints (microphones) or render endpoints (system sound, via loopback)."""
    global _CACHE
    if refresh:
        _CACHE = {}
    key = bool(loopback)
    if key not in _CACHE:
        com_initialize()
        found = []
        enumerator = _enumerator()
        try:
            for device in enumerator.endpoints(E_CAPTURE if not loopback else E_RENDER,
                                              DEVICE_STATE_ACTIVE):
                try:
                    found.append(EndpointInfo(device.get_id(), device.friendly_name(), loopback))
                finally:
                    device.release()
        finally:
            enumerator.release()
        _CACHE[key] = found
    return list(_CACHE[key])


_CACHE = {}


def default_endpoint_id(loopback=False):
    """The Windows default device for that direction, or "" when there is none."""
    try:
        com_initialize()
        enumerator = _enumerator()
        try:
            device = enumerator.default_endpoint(E_RENDER if loopback else E_CAPTURE)
            try:
                return device.get_id()
            finally:
                device.release()
        finally:
            enumerator.release()
    except Exception:
        return ""


class CaptureFormat:
    """What the endpoint actually delivers in shared mode."""

    __slots__ = ("rate", "channels", "kind", "bits", "block_align")

    def __init__(self, rate, channels, kind, bits, block_align):
        self.rate, self.channels, self.kind = int(rate), int(channels), kind
        self.bits, self.block_align = int(bits), int(block_align)

    def __repr__(self):
        return f"CaptureFormat({self.rate} Hz, {self.channels} ch, {self.kind})"


def _parse_format(pointer):
    base = ctypes.cast(pointer, ctypes.POINTER(WAVEFORMATEX)).contents
    tag, bits = int(base.wFormatTag), int(base.wBitsPerSample)
    subtype = ""
    if tag == WAVE_FORMAT_EXTENSIBLE:
        raw = ctypes.string_at(pointer, 40)
        subtype = GUID.from_buffer_copy(raw, 24).text()
    if tag == WAVE_FORMAT_IEEE_FLOAT or (tag == WAVE_FORMAT_EXTENSIBLE and subtype == SUBTYPE_FLOAT):
        kind = "float32"
        bits = 32
    elif bits == 16:
        kind, bits = "int16", 16
    elif bits == 24:
        kind = "int24"
    elif bits == 32:
        kind = "int32"
    else:
        raise WasapiError(f"Unsupported sample format ({bits} bit)")
    return CaptureFormat(base.nSamplesPerSec, base.nChannels, kind, bits, base.nBlockAlign)


class WasapiCapture:
    """A shared-mode capture stream: one microphone, or one output device in loopback."""

    def __init__(self, device_id, loopback=False):
        self.device_id = device_id
        self.loopback = bool(loopback)
        self.format = None
        self._device = self._client = self._capture = None
        self._format_ptr = None
        self._started = False
        self._buffer_frames = 0

    # ------------------------------------------------------------------ lifecycle
    def open(self):
        com_initialize()
        enumerator = _enumerator()
        try:
            self._device = enumerator.get_device(self.device_id)
        finally:
            enumerator.release()
        self._client = _Interface(self._device.activate(IID_IAudioClient))
        mix_ptr = ctypes.c_void_p()
        _check(self._client.call(8, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
            self._client.ptr, ctypes.byref(mix_ptr)), "GetMixFormat")
        try:
            self.format = _parse_format(mix_ptr.value)
        except WasapiError:
            _ole32.CoTaskMemFree(mix_ptr)
            raise
        self._format_ptr = mix_ptr
        flags = AUDCLNT_STREAMFLAGS_LOOPBACK if self.loopback else 0
        # 500 ms of slack in the endpoint buffer: polling cannot overflow it, and a slow
        # GUI thread never drops audio because of it.
        hr = self._client.call(3, ctypes.c_long, ctypes.c_int, ctypes.c_ulong, ctypes.c_longlong,
                               ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p)(
            self._client.ptr, AUDCLNT_SHAREMODE_SHARED, flags, 5_000_000, 0, mix_ptr.value, None)
        _ole32.CoTaskMemFree(mix_ptr)
        self._format_ptr = None
        _check(hr, "IAudioClient::Initialize")
        size = ctypes.c_ulong()
        self._client.call(4, ctypes.c_long, ctypes.POINTER(ctypes.c_ulong))(self._client.ptr,
                                                                            ctypes.byref(size))
        self._buffer_frames = int(size.value)
        # The capture client is handed out by the audio client (GetService): activating it on
        # the device fails, because it only exists once a stream has been created.
        out = ctypes.c_void_p()
        iid = GUID.parse(IID_IAudioCaptureClient)
        _check(self._client.call(14, ctypes.c_long, ctypes.POINTER(GUID),
                                 ctypes.POINTER(ctypes.c_void_p))(
            self._client.ptr, ctypes.byref(iid), ctypes.byref(out)),
            "IAudioClient::GetService(IAudioCaptureClient)")
        self._capture = _Interface(out.value)
        return self.format

    def start(self):
        if self._client is None:
            self.open()
        _check(self._client.call(10, ctypes.c_long)(self._client.ptr), "IAudioClient::Start")
        self._started = True

    def stop(self):
        if self._client is not None and self._started:
            self._client.call(11, ctypes.c_long)(self._client.ptr)
            self._started = False

    def close(self):
        try:
            self.stop()
        finally:
            for item in (self._capture, self._client, self._device):
                if item is not None:
                    item.release()
            self._capture = self._client = self._device = None

    # ------------------------------------------------------------------ reading
    def next_packet_frames(self):
        size = ctypes.c_ulong()
        hr = self._capture.call(5, ctypes.c_long, ctypes.POINTER(ctypes.c_ulong))(
            self._capture.ptr, ctypes.byref(size))
        if hr < 0:
            raise WasapiError("The audio device stopped responding", hr)
        return int(size.value)

    def read_packet(self):
        """(samples, flags, device_position) for one packet; samples may be all zeros."""
        import numpy as np

        data = ctypes.c_void_p()
        frames = ctypes.c_ulong()
        flags = ctypes.c_ulong()
        position = ctypes.c_ulonglong()
        hr = self._capture.call(3, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p),
                                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong))(
            self._capture.ptr, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags),
            ctypes.byref(position), None)
        _check(hr, "IAudioCaptureClient::GetBuffer")
        count = int(frames.value)
        if count <= 0:
            return np.zeros((0, self.format.channels), dtype=np.float32), 0, int(position.value)
        try:
            if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                block = np.zeros((count, self.format.channels), dtype=np.float32)
            else:
                size = count * self.format.block_align
                raw = ctypes.string_at(data.value, size)
                block = _to_float(raw, self.format, count)
        finally:
            self._capture.call(4, ctypes.c_long, ctypes.c_ulong)(self._capture.ptr, count)
        return block, int(flags.value), int(position.value)


def _to_float(raw, fmt, frames):
    """Decode a packet to float32 in -1..1 without an extra copy where possible."""
    import numpy as np

    if fmt.kind == "float32":
        block = np.frombuffer(raw, dtype=np.float32).reshape(frames, fmt.channels)
        return np.array(block, dtype=np.float32, copy=True)
    if fmt.kind == "int16":
        block = np.frombuffer(raw, dtype=np.int16).reshape(frames, fmt.channels)
        return block.astype(np.float32) / 32768.0
    if fmt.kind == "int24":
        raw = np.frombuffer(raw, dtype=np.uint8).reshape(frames * fmt.channels, 3)
        wide = np.zeros((raw.shape[0], 4), dtype=np.uint8)
        wide[:, 1:] = raw                            # little-endian 24-bit into the high bytes
        return wide.view(np.int32).reshape(frames, fmt.channels).astype(np.float32) / 2147483648.0
    block = np.frombuffer(raw, dtype=np.int32).reshape(frames, fmt.channels)
    return block.astype(np.float32) / 2147483648.0


def device_is_present(device_id, loopback=False):
    for endpoint in list_endpoints(loopback, refresh=True):
        if endpoint.id == device_id:
            return True
    return False
