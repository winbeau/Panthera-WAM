namespace Panthera.Terminal.Core;

public sealed class LatestCameraFrames
{
    private readonly LatestStateSlot<CameraFrameSnapshot> _depth = new();
    private readonly LatestStateSlot<CameraFrameSnapshot> _color = new();

    public long Publish(CameraFrameSnapshot frame) =>
        Slot(frame.Stream).Publish(frame);

    public (CameraFrameSnapshot? Value, long Version) Read(CameraStreamKind stream) =>
        Slot(stream).Read();

    private LatestStateSlot<CameraFrameSnapshot> Slot(CameraStreamKind stream) =>
        stream == CameraStreamKind.Depth ? _depth : _color;
}
