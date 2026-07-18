using System.Windows;
using System.Windows.Controls;

namespace Panthera.Terminal.Controls;

public partial class JointGauge : UserControl
{
    public static readonly DependencyProperty TitleProperty = DependencyProperty.Register(
        nameof(Title), typeof(string), typeof(JointGauge), new PropertyMetadata("J1"));
    public static readonly DependencyProperty PositionProperty = DependencyProperty.Register(
        nameof(Position), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0, OnGaugeChanged));
    public static readonly DependencyProperty MinimumProperty = DependencyProperty.Register(
        nameof(Minimum), typeof(double), typeof(JointGauge), new PropertyMetadata(-Math.PI, OnGaugeChanged));
    public static readonly DependencyProperty MaximumProperty = DependencyProperty.Register(
        nameof(Maximum), typeof(double), typeof(JointGauge), new PropertyMetadata(Math.PI, OnGaugeChanged));
    public static readonly DependencyProperty VelocityProperty = DependencyProperty.Register(
        nameof(Velocity), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0, OnVelocityChanged));
    public static readonly DependencyProperty TorqueProperty = DependencyProperty.Register(
        nameof(Torque), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0, OnTorqueChanged));
    public static readonly DependencyProperty StatusTextProperty = DependencyProperty.Register(
        nameof(StatusText), typeof(string), typeof(JointGauge), new PropertyMetadata("离线"));
    private static readonly DependencyPropertyKey AngleDegreesPropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(AngleDegrees), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0));
    private static readonly DependencyPropertyKey PositionDegreesPropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(PositionDegrees), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0));
    private static readonly DependencyPropertyKey SpeedMagnitudePropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(SpeedMagnitude), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0));
    private static readonly DependencyPropertyKey TorqueMagnitudePropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(TorqueMagnitude), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0));

    public JointGauge()
    {
        InitializeComponent();
        UpdateGauge();
    }

    public string Title { get => (string)GetValue(TitleProperty); set => SetValue(TitleProperty, value); }
    public double Position { get => (double)GetValue(PositionProperty); set => SetValue(PositionProperty, value); }
    public double Minimum { get => (double)GetValue(MinimumProperty); set => SetValue(MinimumProperty, value); }
    public double Maximum { get => (double)GetValue(MaximumProperty); set => SetValue(MaximumProperty, value); }
    public double Velocity { get => (double)GetValue(VelocityProperty); set => SetValue(VelocityProperty, value); }
    public double Torque { get => (double)GetValue(TorqueProperty); set => SetValue(TorqueProperty, value); }
    public string StatusText { get => (string)GetValue(StatusTextProperty); set => SetValue(StatusTextProperty, value); }
    public double AngleDegrees => (double)GetValue(AngleDegreesPropertyKey.DependencyProperty);
    public double PositionDegrees => (double)GetValue(PositionDegreesPropertyKey.DependencyProperty);
    public double SpeedMagnitude => (double)GetValue(SpeedMagnitudePropertyKey.DependencyProperty);
    public double TorqueMagnitude => (double)GetValue(TorqueMagnitudePropertyKey.DependencyProperty);

    private static void OnGaugeChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs eventArgs) =>
        ((JointGauge)dependencyObject).UpdateGauge();

    private static void OnVelocityChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs eventArgs) =>
        dependencyObject.SetValue(SpeedMagnitudePropertyKey, Math.Abs((double)eventArgs.NewValue));

    private static void OnTorqueChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs eventArgs) =>
        dependencyObject.SetValue(TorqueMagnitudePropertyKey, Math.Abs((double)eventArgs.NewValue));

    private void UpdateGauge()
    {
        var range = Maximum - Minimum;
        var fraction = range <= 0 ? 0.5 : Math.Clamp((Position - Minimum) / range, 0, 1);
        SetValue(AngleDegreesPropertyKey, -135 + (fraction * 270));
        SetValue(PositionDegreesPropertyKey, Position * 180.0 / Math.PI);
    }
}
