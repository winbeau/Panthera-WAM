using System.Windows;
using System.Windows.Controls;

namespace Panthera.Terminal.Controls;

public partial class JointStatusCard : UserControl
{
    public static readonly DependencyProperty TitleProperty = DependencyProperty.Register(
        nameof(Title), typeof(string), typeof(JointStatusCard), new PropertyMetadata("J1"));
    public static readonly DependencyProperty SubtitleProperty = DependencyProperty.Register(
        nameof(Subtitle), typeof(string), typeof(JointStatusCard), new PropertyMetadata(string.Empty));
    public static readonly DependencyProperty PositionProperty = DependencyProperty.Register(
        nameof(Position), typeof(double), typeof(JointStatusCard), new PropertyMetadata(0.0, OnRangeChanged));
    public static readonly DependencyProperty MinimumProperty = DependencyProperty.Register(
        nameof(Minimum), typeof(double), typeof(JointStatusCard), new PropertyMetadata(-Math.PI, OnRangeChanged));
    public static readonly DependencyProperty MaximumProperty = DependencyProperty.Register(
        nameof(Maximum), typeof(double), typeof(JointStatusCard), new PropertyMetadata(Math.PI, OnRangeChanged));
    public static readonly DependencyProperty VelocityProperty = DependencyProperty.Register(
        nameof(Velocity), typeof(double), typeof(JointStatusCard), new PropertyMetadata(0.0));
    public static readonly DependencyProperty IsOnlineProperty = DependencyProperty.Register(
        nameof(IsOnline), typeof(bool), typeof(JointStatusCard), new PropertyMetadata(false));
    public static readonly DependencyProperty IsWarningProperty = DependencyProperty.Register(
        nameof(IsWarning), typeof(bool), typeof(JointStatusCard), new PropertyMetadata(false));
    private static readonly DependencyPropertyKey PositionPercentPropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(PositionPercent), typeof(double), typeof(JointStatusCard), new PropertyMetadata(50.0));

    public JointStatusCard()
    {
        InitializeComponent();
        UpdatePositionPercent();
    }

    public string Title { get => (string)GetValue(TitleProperty); set => SetValue(TitleProperty, value); }
    public string Subtitle { get => (string)GetValue(SubtitleProperty); set => SetValue(SubtitleProperty, value); }
    public double Position { get => (double)GetValue(PositionProperty); set => SetValue(PositionProperty, value); }
    public double Minimum { get => (double)GetValue(MinimumProperty); set => SetValue(MinimumProperty, value); }
    public double Maximum { get => (double)GetValue(MaximumProperty); set => SetValue(MaximumProperty, value); }
    public double Velocity { get => (double)GetValue(VelocityProperty); set => SetValue(VelocityProperty, value); }
    public bool IsOnline { get => (bool)GetValue(IsOnlineProperty); set => SetValue(IsOnlineProperty, value); }
    public bool IsWarning { get => (bool)GetValue(IsWarningProperty); set => SetValue(IsWarningProperty, value); }
    public double PositionPercent => (double)GetValue(PositionPercentPropertyKey.DependencyProperty);

    private static void OnRangeChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs eventArgs) =>
        ((JointStatusCard)dependencyObject).UpdatePositionPercent();

    private void UpdatePositionPercent()
    {
        var range = Maximum - Minimum;
        var percent = range <= 0 ? 50 : Math.Clamp((Position - Minimum) / range * 100.0, 0, 100);
        SetValue(PositionPercentPropertyKey, percent);
    }
}
