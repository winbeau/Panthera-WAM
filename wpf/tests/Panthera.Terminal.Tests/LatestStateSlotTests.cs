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
