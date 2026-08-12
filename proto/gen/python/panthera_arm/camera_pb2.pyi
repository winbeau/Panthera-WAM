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

class CameraTimestampUnit(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAMERA_TIMESTAMP_UNIT_UNSPECIFIED: _ClassVar[CameraTimestampUnit]
    CAMERA_TIMESTAMP_UNIT_MILLISECONDS: _ClassVar[CameraTimestampUnit]
    CAMERA_TIMESTAMP_UNIT_NANOSECONDS: _ClassVar[CameraTimestampUnit]

class CameraClockDomain(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAMERA_CLOCK_DOMAIN_UNSPECIFIED: _ClassVar[CameraClockDomain]
    CAMERA_CLOCK_DOMAIN_REALSENSE_HARDWARE: _ClassVar[CameraClockDomain]
    CAMERA_CLOCK_DOMAIN_REALSENSE_SYSTEM_TIME: _ClassVar[CameraClockDomain]
    CAMERA_CLOCK_DOMAIN_HOST_MONOTONIC: _ClassVar[CameraClockDomain]
    CAMERA_CLOCK_DOMAIN_SIMULATED: _ClassVar[CameraClockDomain]

class CameraTimestampSource(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAMERA_TIMESTAMP_SOURCE_UNSPECIFIED: _ClassVar[CameraTimestampSource]
    CAMERA_TIMESTAMP_SOURCE_DEVICE: _ClassVar[CameraTimestampSource]
    CAMERA_TIMESTAMP_SOURCE_V4L2_BUFFER: _ClassVar[CameraTimestampSource]
    CAMERA_TIMESTAMP_SOURCE_HOST_RECEIVE: _ClassVar[CameraTimestampSource]
    CAMERA_TIMESTAMP_SOURCE_DEVICE_TO_HOST_ESTIMATE: _ClassVar[CameraTimestampSource]

class CameraTimestampQuality(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    CAMERA_TIMESTAMP_QUALITY_UNSPECIFIED: _ClassVar[CameraTimestampQuality]
    CAMERA_TIMESTAMP_QUALITY_DEVICE_NATIVE: _ClassVar[CameraTimestampQuality]
    CAMERA_TIMESTAMP_QUALITY_DRIVER_REPORTED: _ClassVar[CameraTimestampQuality]
    CAMERA_TIMESTAMP_QUALITY_ESTIMATED: _ClassVar[CameraTimestampQuality]
    CAMERA_TIMESTAMP_QUALITY_HOST_OBSERVED: _ClassVar[CameraTimestampQuality]
    CAMERA_TIMESTAMP_QUALITY_SIMULATED: _ClassVar[CameraTimestampQuality]
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
CAMERA_TIMESTAMP_UNIT_UNSPECIFIED: CameraTimestampUnit
CAMERA_TIMESTAMP_UNIT_MILLISECONDS: CameraTimestampUnit
CAMERA_TIMESTAMP_UNIT_NANOSECONDS: CameraTimestampUnit
CAMERA_CLOCK_DOMAIN_UNSPECIFIED: CameraClockDomain
CAMERA_CLOCK_DOMAIN_REALSENSE_HARDWARE: CameraClockDomain
CAMERA_CLOCK_DOMAIN_REALSENSE_SYSTEM_TIME: CameraClockDomain
CAMERA_CLOCK_DOMAIN_HOST_MONOTONIC: CameraClockDomain
CAMERA_CLOCK_DOMAIN_SIMULATED: CameraClockDomain
CAMERA_TIMESTAMP_SOURCE_UNSPECIFIED: CameraTimestampSource
CAMERA_TIMESTAMP_SOURCE_DEVICE: CameraTimestampSource
CAMERA_TIMESTAMP_SOURCE_V4L2_BUFFER: CameraTimestampSource
CAMERA_TIMESTAMP_SOURCE_HOST_RECEIVE: CameraTimestampSource
CAMERA_TIMESTAMP_SOURCE_DEVICE_TO_HOST_ESTIMATE: CameraTimestampSource
CAMERA_TIMESTAMP_QUALITY_UNSPECIFIED: CameraTimestampQuality
CAMERA_TIMESTAMP_QUALITY_DEVICE_NATIVE: CameraTimestampQuality
CAMERA_TIMESTAMP_QUALITY_DRIVER_REPORTED: CameraTimestampQuality
CAMERA_TIMESTAMP_QUALITY_ESTIMATED: CameraTimestampQuality
CAMERA_TIMESTAMP_QUALITY_HOST_OBSERVED: CameraTimestampQuality
CAMERA_TIMESTAMP_QUALITY_SIMULATED: CameraTimestampQuality

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

class StreamCollectedFramesRequest(_message.Message):
    __slots__ = ("stream", "after_sequence", "start_at_latest", "max_frames")
    STREAM_FIELD_NUMBER: _ClassVar[int]
    AFTER_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    START_AT_LATEST_FIELD_NUMBER: _ClassVar[int]
    MAX_FRAMES_FIELD_NUMBER: _ClassVar[int]
    stream: CameraStreamType
    after_sequence: int
    start_at_latest: bool
    max_frames: int
    def __init__(self, stream: _Optional[_Union[CameraStreamType, str]] = ..., after_sequence: _Optional[int] = ..., start_at_latest: _Optional[bool] = ..., max_frames: _Optional[int] = ...) -> None: ...

class CameraFrame(_message.Message):
    __slots__ = ("stream", "pixel_format", "sequence", "captured_at_ns", "device_timestamp_ms", "width", "height", "stride", "depth_scale", "data", "role", "captured_monotonic_ns", "device_timestamp_raw", "device_timestamp_unit", "device_clock_domain", "host_receive_monotonic_ns", "host_publish_monotonic_ns", "estimated_capture_monotonic_ns", "timestamp_source", "timestamp_quality", "device_frame_number", "frameset_sequence", "stream_instance_id")
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
    DEVICE_TIMESTAMP_RAW_FIELD_NUMBER: _ClassVar[int]
    DEVICE_TIMESTAMP_UNIT_FIELD_NUMBER: _ClassVar[int]
    DEVICE_CLOCK_DOMAIN_FIELD_NUMBER: _ClassVar[int]
    HOST_RECEIVE_MONOTONIC_NS_FIELD_NUMBER: _ClassVar[int]
    HOST_PUBLISH_MONOTONIC_NS_FIELD_NUMBER: _ClassVar[int]
    ESTIMATED_CAPTURE_MONOTONIC_NS_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_SOURCE_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_QUALITY_FIELD_NUMBER: _ClassVar[int]
    DEVICE_FRAME_NUMBER_FIELD_NUMBER: _ClassVar[int]
    FRAMESET_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    STREAM_INSTANCE_ID_FIELD_NUMBER: _ClassVar[int]
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
    device_timestamp_raw: float
    device_timestamp_unit: CameraTimestampUnit
    device_clock_domain: CameraClockDomain
    host_receive_monotonic_ns: int
    host_publish_monotonic_ns: int
    estimated_capture_monotonic_ns: int
    timestamp_source: CameraTimestampSource
    timestamp_quality: CameraTimestampQuality
    device_frame_number: int
    frameset_sequence: int
    stream_instance_id: str
    def __init__(self, stream: _Optional[_Union[CameraStreamType, str]] = ..., pixel_format: _Optional[_Union[CameraPixelFormat, str]] = ..., sequence: _Optional[int] = ..., captured_at_ns: _Optional[int] = ..., device_timestamp_ms: _Optional[float] = ..., width: _Optional[int] = ..., height: _Optional[int] = ..., stride: _Optional[int] = ..., depth_scale: _Optional[float] = ..., data: _Optional[bytes] = ..., role: _Optional[_Union[CameraDeviceRole, str]] = ..., captured_monotonic_ns: _Optional[int] = ..., device_timestamp_raw: _Optional[float] = ..., device_timestamp_unit: _Optional[_Union[CameraTimestampUnit, str]] = ..., device_clock_domain: _Optional[_Union[CameraClockDomain, str]] = ..., host_receive_monotonic_ns: _Optional[int] = ..., host_publish_monotonic_ns: _Optional[int] = ..., estimated_capture_monotonic_ns: _Optional[int] = ..., timestamp_source: _Optional[_Union[CameraTimestampSource, str]] = ..., timestamp_quality: _Optional[_Union[CameraTimestampQuality, str]] = ..., device_frame_number: _Optional[int] = ..., frameset_sequence: _Optional[int] = ..., stream_instance_id: _Optional[str] = ...) -> None: ...

class CollectedCameraFrame(_message.Message):
    __slots__ = ("frame", "oldest_available_sequence", "overwritten_samples_total")
    FRAME_FIELD_NUMBER: _ClassVar[int]
    OLDEST_AVAILABLE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    OVERWRITTEN_SAMPLES_TOTAL_FIELD_NUMBER: _ClassVar[int]
    frame: CameraFrame
    oldest_available_sequence: int
    overwritten_samples_total: int
    def __init__(self, frame: _Optional[_Union[CameraFrame, _Mapping]] = ..., oldest_available_sequence: _Optional[int] = ..., overwritten_samples_total: _Optional[int] = ...) -> None: ...
