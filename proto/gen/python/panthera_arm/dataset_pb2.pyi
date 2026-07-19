from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DatasetJobState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DATASET_JOB_STATE_UNSPECIFIED: _ClassVar[DatasetJobState]
    DATASET_JOB_STATE_QUEUED: _ClassVar[DatasetJobState]
    DATASET_JOB_STATE_RUNNING: _ClassVar[DatasetJobState]
    DATASET_JOB_STATE_DONE: _ClassVar[DatasetJobState]
    DATASET_JOB_STATE_FAILED: _ClassVar[DatasetJobState]
    DATASET_JOB_STATE_CANCELLED: _ClassVar[DatasetJobState]
DATASET_JOB_STATE_UNSPECIFIED: DatasetJobState
DATASET_JOB_STATE_QUEUED: DatasetJobState
DATASET_JOB_STATE_RUNNING: DatasetJobState
DATASET_JOB_STATE_DONE: DatasetJobState
DATASET_JOB_STATE_FAILED: DatasetJobState
DATASET_JOB_STATE_CANCELLED: DatasetJobState

class ExportLeRobotRequest(_message.Message):
    __slots__ = ("trajectory_path", "output_dir", "repo_id", "task", "overwrite")
    TRAJECTORY_PATH_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_DIR_FIELD_NUMBER: _ClassVar[int]
    REPO_ID_FIELD_NUMBER: _ClassVar[int]
    TASK_FIELD_NUMBER: _ClassVar[int]
    OVERWRITE_FIELD_NUMBER: _ClassVar[int]
    trajectory_path: str
    output_dir: str
    repo_id: str
    task: str
    overwrite: bool
    def __init__(self, trajectory_path: _Optional[str] = ..., output_dir: _Optional[str] = ..., repo_id: _Optional[str] = ..., task: _Optional[str] = ..., overwrite: _Optional[bool] = ...) -> None: ...

class DatasetJobAccepted(_message.Message):
    __slots__ = ("job_id",)
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    def __init__(self, job_id: _Optional[str] = ...) -> None: ...

class DatasetJobRequest(_message.Message):
    __slots__ = ("job_id",)
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    def __init__(self, job_id: _Optional[str] = ...) -> None: ...

class DatasetCancelResponse(_message.Message):
    __slots__ = ("cancelled",)
    CANCELLED_FIELD_NUMBER: _ClassVar[int]
    cancelled: bool
    def __init__(self, cancelled: _Optional[bool] = ...) -> None: ...

class DatasetJobStatus(_message.Message):
    __slots__ = ("job_id", "state", "progress", "output_dir", "frame_count", "error_message")
    JOB_ID_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_DIR_FIELD_NUMBER: _ClassVar[int]
    FRAME_COUNT_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    job_id: str
    state: DatasetJobState
    progress: float
    output_dir: str
    frame_count: int
    error_message: str
    def __init__(self, job_id: _Optional[str] = ..., state: _Optional[_Union[DatasetJobState, str]] = ..., progress: _Optional[float] = ..., output_dir: _Optional[str] = ..., frame_count: _Optional[int] = ..., error_message: _Optional[str] = ...) -> None: ...

class DatasetMappingRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class DatasetFieldMapping(_message.Message):
    __slots__ = ("source", "target", "dtype", "shape")
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    TARGET_FIELD_NUMBER: _ClassVar[int]
    DTYPE_FIELD_NUMBER: _ClassVar[int]
    SHAPE_FIELD_NUMBER: _ClassVar[int]
    source: str
    target: str
    dtype: str
    shape: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, source: _Optional[str] = ..., target: _Optional[str] = ..., dtype: _Optional[str] = ..., shape: _Optional[_Iterable[int]] = ...) -> None: ...

class DatasetMappingResponse(_message.Message):
    __slots__ = ("format_version", "fields")
    FORMAT_VERSION_FIELD_NUMBER: _ClassVar[int]
    FIELDS_FIELD_NUMBER: _ClassVar[int]
    format_version: str
    fields: _containers.RepeatedCompositeFieldContainer[DatasetFieldMapping]
    def __init__(self, format_version: _Optional[str] = ..., fields: _Optional[_Iterable[_Union[DatasetFieldMapping, _Mapping]]] = ...) -> None: ...
