from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CameraStreamType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAMERA_STREAM_TYPE_UNSPECIFIED: _ClassVar[CameraStreamType]
    CAMERA_STREAM_TYPE_DEPTH: _ClassVar[CameraStreamType]
    CAMERA_STREAM_TYPE_COLOR: _ClassVar[CameraStreamType]

class CameraDeviceRole(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAMERA_DEVICE_ROLE_UNSPECIFIED: _ClassVar[CameraDeviceRole]
    CAMERA_DEVICE_ROLE_WRIST: _ClassVar[CameraDeviceRole]
    CAMERA_DEVICE_ROLE_OVERHEAD: _ClassVar[CameraDeviceRole]

class CameraPixelFormat(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAMERA_PIXEL_FORMAT_UNSPECIFIED: _ClassVar[CameraPixelFormat]
    CAMERA_PIXEL_FORMAT_Z16: _ClassVar[CameraPixelFormat]
    CAMERA_PIXEL_FORMAT_RGB8: _ClassVar[CameraPixelFormat]
    CAMERA_PIXEL_FORMAT_JPEG: _ClassVar[CameraPixelFormat]
CAMERA_STREAM_TYPE_UNSPECIFIED: CameraStreamType
CAMERA_STREAM_TYPE_DEPTH: CameraStreamType
CAMERA_STREAM_TYPE_COLOR: CameraStreamType
CAMERA_DEVICE_ROLE_UNSPECIFIED: CameraDeviceRole
CAMERA_DEVICE_ROLE_WRIST: CameraDeviceRole
CAMERA_DEVICE_ROLE_OVERHEAD: CameraDeviceRole
CAMERA_PIXEL_FORMAT_UNSPECIFIED: CameraPixelFormat
CAMERA_PIXEL_FORMAT_Z16: CameraPixelFormat
CAMERA_PIXEL_FORMAT_RGB8: CameraPixelFormat
CAMERA_PIXEL_FORMAT_JPEG: CameraPixelFormat

class CameraStatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class CameraProfile(_message.Message):
    __slots__ = ("stream", "pixel_format", "width", "height", "fps")
    STREAM_FIELD_NUMBER: _ClassVar[int]
    PIXEL_FORMAT_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    FPS_FIELD_NUMBER: _ClassVar[int]
    stream: CameraStreamType
    pixel_format: CameraPixelFormat
    width: int
    height: int
    fps: int
    def __init__(self, stream: _Optional[_Union[CameraStreamType, str]] = ..., pixel_format: _Optional[_Union[CameraPixelFormat, str]] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., fps: _Optional[int] = ...) -> None: ...

class CameraStatus(_message.Message):
    __slots__ = ("enabled", "available", "streaming", "model", "serial", "firmware", "usb_type", "sdk_version", "error", "last_frame_age_ms", "actual_fps", "profiles", "role")
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    STREAMING_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    SERIAL_FIELD_NUMBER: _ClassVar[int]
    FIRMWARE_FIELD_NUMBER: _ClassVar[int]
    USB_TYPE_FIELD_NUMBER: _ClassVar[int]
    SDK_VERSION_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    LAST_FRAME_AGE_MS_FIELD_NUMBER: _ClassVar[int]
    ACTUAL_FPS_FIELD_NUMBER: _ClassVar[int]
    PROFILES_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    enabled: bool
    available: bool
    streaming: bool
    model: str
    serial: str
    firmware: str
    usb_type: str
    sdk_version: str
    error: str
    last_frame_age_ms: int
    actual_fps: float
    profiles: _containers.RepeatedCompositeFieldContainer[CameraProfile]
    role: CameraDeviceRole
    def __init__(self, enabled: _Optional[bool] = ..., available: _Optional[bool] = ..., streaming: _Optional[bool] = ..., model: _Optional[str] = ..., serial: _Optional[str] = ..., firmware: _Optional[str] = ..., usb_type: _Optional[str] = ..., sdk_version: _Optional[str] = ..., error: _Optional[str] = ..., last_frame_age_ms: _Optional[int] = ..., actual_fps: _Optional[float] = ..., profiles: _Optional[_Iterable[_Union[CameraProfile, _Mapping]]] = ..., role: _Optional[_Union[CameraDeviceRole, str]] = ...) -> None: ...

class CaptureFrameRequest(_message.Message):
    __slots__ = ("stream", "timeout_ms")
    STREAM_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_MS_FIELD_NUMBER: _ClassVar[int]
    stream: CameraStreamType
    timeout_ms: int
    def __init__(self, stream: _Optional[_Union[CameraStreamType, str]] = ..., timeout_ms: _Optional[int] = ...) -> None: ...

class StreamFramesRequest(_message.Message):
    __slots__ = ("stream", "max_rate_hz", "max_frames")
    STREAM_FIELD_NUMBER: _ClassVar[int]
    MAX_RATE_HZ_FIELD_NUMBER: _ClassVar[int]
    MAX_FRAMES_FIELD_NUMBER: _ClassVar[int]
    stream: CameraStreamType
    max_rate_hz: float
    max_frames: int
    def __init__(self, stream: _Optional[_Union[CameraStreamType, str]] = ..., max_rate_hz: _Optional[float] = ..., max_frames: _Optional[int] = ...) -> None: ...

class CameraFrame(_message.Message):
    __slots__ = ("stream", "pixel_format", "sequence", "captured_at_ns", "device_timestamp_ms", "width", "height", "stride", "depth_scale", "data", "role", "captured_monotonic_ns")
    STREAM_FIELD_NUMBER: _ClassVar[int]
    PIXEL_FORMAT_FIELD_NUMBER: _ClassVar[int]
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    CAPTURED_AT_NS_FIELD_NUMBER: _ClassVar[int]
    DEVICE_TIMESTAMP_MS_FIELD_NUMBER: _ClassVar[int]
    WIDTH_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    STRIDE_FIELD_NUMBER: _ClassVar[int]
    DEPTH_SCALE_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CAPTURED_MONOTONIC_NS_FIELD_NUMBER: _ClassVar[int]
    stream: CameraStreamType
    pixel_format: CameraPixelFormat
    sequence: int
    captured_at_ns: int
    device_timestamp_ms: float
    width: int
    height: int
    stride: int
    depth_scale: float
    data: bytes
    role: CameraDeviceRole
    captured_monotonic_ns: int
    def __init__(self, stream: _Optional[_Union[CameraStreamType, str]] = ..., pixel_format: _Optional[_Union[CameraPixelFormat, str]] = ..., sequence: _Optional[int] = ..., captured_at_ns: _Optional[int] = ..., device_timestamp_ms: _Optional[float] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., stride: _Optional[int] = ..., depth_scale: _Optional[float] = ..., data: _Optional[bytes] = ..., role: _Optional[_Union[CameraDeviceRole, str]] = ..., captured_monotonic_ns: _Optional[int] = ...) -> None: ...
