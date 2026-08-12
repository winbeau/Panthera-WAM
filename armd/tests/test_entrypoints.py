from armd.__main__ import build_parser as build_armd_parser
from armd.__main__ import parse_policy_camera_boxes
from armd.camera.__main__ import build_parser as build_camera_parser


def test_armd_bind_uses_environment(monkeypatch):
    monkeypatch.setenv("PANTHERA_ARM_BIND", "100.64.0.10:50051")

    args = build_armd_parser().parse_args([])

    assert args.bind == "100.64.0.10:50051"


def test_policy_path_gate_is_disabled_until_table_height_is_explicit(monkeypatch):
    monkeypatch.delenv("PANTHERA_POLICY_TABLE_Z_MIN", raising=False)
    monkeypatch.setenv("PANTHERA_POLICY_BASE_RADIUS", "0.12")

    args = build_armd_parser().parse_args([])

    assert args.policy_table_z_min is None
    assert args.policy_base_radius == 0.12


def test_policy_path_gate_uses_environment(monkeypatch):
    monkeypatch.setenv("PANTHERA_POLICY_TABLE_Z_MIN", "0.10")

    args = build_armd_parser().parse_args([])

    assert args.policy_table_z_min == 0.10


def test_policy_camera_boxes_parse_from_deployment_json():
    boxes = parse_policy_camera_boxes('[{"lower":[0.1,0.2,0.3],"upper":[0.4,0.5,0.6]}]')
    assert boxes == (((0.1, 0.2, 0.3), (0.4, 0.5, 0.6)),)


def test_camera_bind_uses_environment(monkeypatch):
    monkeypatch.setenv("PANTHERA_CAMERA_BIND", "100.64.0.10:50052")

    args = build_camera_parser().parse_args([])

    assert args.bind == "100.64.0.10:50052"
