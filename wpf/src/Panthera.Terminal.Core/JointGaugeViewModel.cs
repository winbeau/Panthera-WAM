using CommunityToolkit.Mvvm.ComponentModel;

namespace Panthera.Terminal.Core;

public sealed partial class JointGaugeViewModel : ObservableObject
{
    public JointGaugeViewModel(int index, string name, double minimum, double maximum)
    {
        Index = index;
        Name = name;
        Minimum = minimum;
        Maximum = maximum;
    }

    public int Index { get; }

    public string Name { get; }

    public string Subtitle => Index switch
    {
        0 => "底座旋转",
        1 => "肩部",
        2 => "肘部",
        3 => "腕部旋转",
        4 => "腕部俯仰",
        _ => "末端旋转",
    };

    [ObservableProperty]
    private double _minimum;

    [ObservableProperty]
    private double _maximum;

    [ObservableProperty]
    private double _position;

    [ObservableProperty]
    private double _velocity;

    [ObservableProperty]
    private double _torque;

    [ObservableProperty]
    private uint _fault;

    [ObservableProperty]
    private bool _valid;

    [ObservableProperty]
    private bool _limitWarning;

    public double PositionDegrees => Position * 180.0 / Math.PI;

    public string StatusText => !Valid ? "离线" : Fault != 0 ? $"故障 0x{Fault:X2}" : LimitWarning ? "接近限位" : "正常";

    partial void OnPositionChanged(double value) => OnPropertyChanged(nameof(PositionDegrees));

    partial void OnFaultChanged(uint value) => OnPropertyChanged(nameof(StatusText));

    partial void OnValidChanged(bool value) => OnPropertyChanged(nameof(StatusText));

    partial void OnLimitWarningChanged(bool value) => OnPropertyChanged(nameof(StatusText));
}
