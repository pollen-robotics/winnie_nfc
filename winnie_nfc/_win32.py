"""USB product strings of the serial ports, read from Windows.

On Windows, pyserial leaves ListPortInfo.product to None: it only reports the
driver's name, "USB-Enhanced-SERIAL CH343 (COM3)". Windows does keep the
product string the device sent when it was plugged in, as the device property
DEVPKEY_Device_BusReportedDeviceDesc. This module reads it, through the same
SetupAPI and Configuration Manager calls pyserial uses to list the ports.

Imported only on Windows (see usb.py).
"""

import ctypes
import sys
from ctypes import wintypes

# Each device is a node in the PnP tree. The COM port can be the USB device
# itself (the WCH driver), an interface of a composite device (usbser.sys on a
# CDC ACM device, "USB\VID_...&MI_00\..."), or a child of a vendor bus (FTDI):
# its USB device is then up to a couple of levels above.
_MAX_DEPTH = 4

_DIGCF_PRESENT = 0x2
_DICS_FLAG_GLOBAL = 0x1
_DIREG_DEV = 0x1
_KEY_READ = 0x20019
_CR_SUCCESS = 0
_MAX_DEVICE_ID_LEN = 200
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(data1, data2, data3, data4):
    return _GUID(data1, data2, data3, (ctypes.c_ubyte * 8)(*data4))


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.ULONG)]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("ClassGuid", _GUID),
        ("DevInst", wintypes.DWORD),
        ("Reserved", ctypes.c_size_t),
    ]


# The "Ports (COM & LPT)" device class.
_GUID_DEVCLASS_PORTS = _guid(
    0x4D36E978, 0xE325, 0x11CE, (0xBF, 0xC1, 0x08, 0x00, 0x2B, 0xE1, 0x03, 0x18)
)
_DEVPKEY_Device_BusReportedDeviceDesc = _DEVPROPKEY(
    _guid(0x540B947E, 0x8B40, 0x45BC, (0xA8, 0xA2, 0x6A, 0x0B, 0x89, 0x4C, 0xBD, 0xA2)),
    4,
)


def _api():
    # Also tells type checkers that the ctypes calls below are Windows ones.
    assert sys.platform == "win32"

    setupapi = ctypes.WinDLL("setupapi")
    cfgmgr32 = ctypes.WinDLL("cfgmgr32")
    advapi32 = ctypes.WinDLL("advapi32")

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_GUID),
        wintypes.LPCWSTR,
        wintypes.HWND,
        wintypes.DWORD,
    ]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_SP_DEVINFO_DATA),
    ]
    setupapi.SetupDiEnumDeviceInfo.restype = wintypes.BOOL
    setupapi.SetupDiOpenDevRegKey.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_SP_DEVINFO_DATA),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    setupapi.SetupDiOpenDevRegKey.restype = ctypes.c_void_p
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    cfgmgr32.CM_Get_Parent.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        wintypes.ULONG,
    ]
    cfgmgr32.CM_Get_Parent.restype = wintypes.DWORD
    cfgmgr32.CM_Get_Device_IDW.argtypes = [
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    cfgmgr32.CM_Get_Device_IDW.restype = wintypes.DWORD
    cfgmgr32.CM_Get_DevNode_PropertyW.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(_DEVPROPKEY),
        ctypes.POINTER(wintypes.ULONG),
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.ULONG,
    ]
    cfgmgr32.CM_Get_DevNode_PropertyW.restype = wintypes.DWORD

    advapi32.RegQueryValueExW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.RegQueryValueExW.restype = wintypes.LONG
    advapi32.RegCloseKey.argtypes = [ctypes.c_void_p]
    advapi32.RegCloseKey.restype = wintypes.LONG
    return setupapi, cfgmgr32, advapi32


def _port_name(setupapi, advapi32, devices, info):
    """The PortName ("COM3") in the device's registry key, or None."""
    key = setupapi.SetupDiOpenDevRegKey(
        devices, ctypes.byref(info), _DICS_FLAG_GLOBAL, 0, _DIREG_DEV, _KEY_READ
    )
    if not key or key == _INVALID_HANDLE:
        return None
    try:
        buf = ctypes.create_unicode_buffer(256)
        size = wintypes.DWORD(ctypes.sizeof(buf))
        if advapi32.RegQueryValueExW(
            key, "PortName", None, None, buf, ctypes.byref(size)
        ):
            return None
        return buf.value or None
    finally:
        advapi32.RegCloseKey(key)


def _device_id(cfgmgr32, devinst):
    buf = ctypes.create_unicode_buffer(_MAX_DEVICE_ID_LEN)
    if cfgmgr32.CM_Get_Device_IDW(devinst, buf, len(buf), 0) != _CR_SUCCESS:
        return ""
    return buf.value


def _usb_device(cfgmgr32, devinst):
    """The USB device node at or above `devinst`, or None (Bluetooth, PCI...)."""
    for _ in range(_MAX_DEPTH):
        device_id = _device_id(cfgmgr32, devinst).upper()
        if device_id.startswith("USB\\") and "&MI_" not in device_id:
            return devinst
        parent = wintypes.DWORD()
        if cfgmgr32.CM_Get_Parent(ctypes.byref(parent), devinst, 0) != _CR_SUCCESS:
            return None
        devinst = parent.value
    return None


def _bus_reported_desc(cfgmgr32, devinst):
    prop_type = wintypes.ULONG()
    buf = ctypes.create_unicode_buffer(256)
    size = wintypes.ULONG(ctypes.sizeof(buf))
    status = cfgmgr32.CM_Get_DevNode_PropertyW(
        devinst,
        ctypes.byref(_DEVPKEY_Device_BusReportedDeviceDesc),
        ctypes.byref(prop_type),
        buf,
        ctypes.byref(size),
        0,
    )
    if status != _CR_SUCCESS:
        return None
    return buf.value or None


def usb_products():
    """{port name: USB product string} of the USB serial ports present."""
    setupapi, cfgmgr32, advapi32 = _api()
    devices = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(_GUID_DEVCLASS_PORTS), None, None, _DIGCF_PRESENT
    )
    if not devices or devices == _INVALID_HANDLE:
        return {}
    products = {}
    try:
        index = 0
        while True:
            info = _SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not setupapi.SetupDiEnumDeviceInfo(devices, index, ctypes.byref(info)):
                break
            index += 1
            # A Bluetooth port climbs to its USB radio, and can share the
            # board's COM name.
            if _device_id(cfgmgr32, info.DevInst).upper().startswith("BTHENUM\\"):
                continue
            port = _port_name(setupapi, advapi32, devices, info)
            usb_device = _usb_device(cfgmgr32, info.DevInst)
            if port is None or usb_device is None:
                continue
            product = _bus_reported_desc(cfgmgr32, usb_device)
            if product is not None:
                products[port] = product
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devices)
    return products
