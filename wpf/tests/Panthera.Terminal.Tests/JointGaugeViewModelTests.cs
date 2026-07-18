using Panthera.Terminal.Core;

namespace Panthera.Terminal.Tests;

public sealed class JointGaugeViewModelTests
{
    [Fact]
    public void StatusText_ReportsOfflineFaultAndLimitStates()
    {
        var viewModel = new JointGaugeViewModel(0, "J1", -1, 1);
        Assert.Equal("离线", viewModel.StatusText);

        viewModel.Valid = true;
        viewModel.LimitWarning = true;
        Assert.Equal("接近限位", viewModel.StatusText);

        viewModel.Fault = 4;
        Assert.Equal("故障 0x04", viewModel.StatusText);
    }
}
