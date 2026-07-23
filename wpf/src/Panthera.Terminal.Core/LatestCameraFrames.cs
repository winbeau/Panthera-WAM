namespace Panthera.Terminal.Core;

public sealed class LatestCameraFrames
{
    private readonly Dictionary<(CameraSourceKind Source, CameraStreamKind Stream), LatestStateSlot<CameraFrameSnapshot>> _slots = new()
    {
        [(CameraSourceKind.Wrist, CameraStreamKind.Depth)] = new(),
        [(CameraSourceKind.Wrist, CameraStreamKind.Color)] = new(),
        [(CameraSourceKind.Overhead, CameraStreamKind.Color)] = new(),
    };

    public long Publish(CameraFrameSnapshot frame) =>
        Slot(frame.Source, frame.Stream).Publish(frame);

    public (CameraFrameSnapshot? Value, long Version) Read(
        CameraSourceKind source,
        CameraStreamKind stream) =>
        Slot(source, stream).Read();

    private LatestStateSlot<CameraFrameSnapshot> Slot(
        CameraSourceKind source,
        CameraStreamKind stream)
    {
        if (_slots.TryGetValue((source, stream), out var slot))
        {
            return slot;
        }
        throw new ArgumentOutOfRangeException(nameof(stream), $"{source} 不提供 {stream} 流");
    }
}
