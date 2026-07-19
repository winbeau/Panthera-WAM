using System.Text.Json;
using Panthera.Terminal.Settings;

namespace Panthera.Terminal.Tests;

public sealed class UsbIpdDeviceLocatorTests
{
    [Fact]
    public void LocateDevice_MatchesVidPidAndSerialWithoutBusIdAssumption()
    {
        using var document = JsonDocument.Parse(
            """
            {
              "Devices": [
                { "BusId": "9-4", "InstanceId": "USB\\VID_1234&PID_5678\\x" },
                { "BusId": "7-2", "InstanceId": "USB\\VID_CAF1&PID_FFFF\\TEST_SERIAL_001" }
              ]
            }
            """);

        var result = WindowsEnvironmentGuideService.LocateDevice(document.RootElement, "TEST_SERIAL_001");

        Assert.NotNull(result);
        Assert.Equal("7-2", result.BusId);
    }

    [Fact]
    public void LocateDevice_MatchesD405CompositeDevice()
    {
        using var document = JsonDocument.Parse(
            """
            {
              "Devices": [
                { "BusId": "4-1", "InstanceId": "USB\\VID_8086&PID_0B5B\\251323070051" }
              ]
            }
            """);

        var result = WindowsEnvironmentGuideService.LocateDevice(
            document.RootElement,
            "VID_8086&PID_0B5B",
            string.Empty);

        Assert.NotNull(result);
        Assert.Equal("4-1", result.BusId);
    }
}
