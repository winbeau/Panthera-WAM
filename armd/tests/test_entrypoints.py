from armd.__main__ import build_parser as build_armd_parser
from armd.camera.__main__ import build_parser as build_camera_parser


def test_armd_bind_uses_environment(monkeypatch):
    monkeypatch.setenv("PANTHERA_ARM_BIND", "100.64.0.10:50051")

    args = build_armd_parser().parse_args([])

    assert args.bind == "100.64.0.10:50051"


def test_camera_bind_uses_environment(monkeypatch):
    monkeypatch.setenv("PANTHERA_CAMERA_BIND", "100.64.0.10:50052")

    args = build_camera_parser().parse_args([])

    assert args.bind == "100.64.0.10:50052"
