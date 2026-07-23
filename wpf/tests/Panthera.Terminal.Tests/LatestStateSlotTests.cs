using Panthera.Terminal.Core;

namespace Panthera.Terminal.Tests;

public sealed class LatestStateSlotTests
{
    [Fact]
    public void Publish_ReplacesOlderFrameAndAdvancesVersion()
    {
        var slot = new LatestStateSlot<string>();

        var firstVersion = slot.Publish("first");
        var secondVersion = slot.Publish("second");
        var (value, version) = slot.Read();

        Assert.Equal(1, firstVersion);
        Assert.Equal(2, secondVersion);
        Assert.Equal("second", value);
        Assert.Equal(secondVersion, version);
    }
}

public sealed class LatestCameraFramesTests
{
    [Fact]
    public void Publish_KeepsWristAndOverheadColorFramesIndependent()
    {
        var frames = new LatestCameraFrames();
        var wrist = Frame(CameraSourceKind.Wrist, 10);
        var overhead = Frame(CameraSourceKind.Overhead, 20);

        frames.Publish(wrist);
        frames.Publish(overhead);

        Assert.Equal(wrist, frames.Read(CameraSourceKind.Wrist, CameraStreamKind.Color).Value);
        Assert.Equal(overhead, frames.Read(CameraSourceKind.Overhead, CameraStreamKind.Color).Value);
    }

    private static CameraFrameSnapshot Frame(CameraSourceKind source, long sequence) =>
        new(
            source,
            CameraStreamKind.Color,
            source == CameraSourceKind.Overhead ? CameraPixelKind.Jpeg : CameraPixelKind.Rgb8,
            sequence,
            100,
            8,
            6,
            24,
            0,
            [1, 2, 3],
            200);
}
