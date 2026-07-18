using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Panthera.Terminal.Controls;

public partial class JointGauge : UserControl
{
    public static readonly DependencyProperty TitleProperty = DependencyProperty.Register(
        nameof(Title), typeof(string), typeof(JointGauge), new PropertyMetadata("J1"));
    public static readonly DependencyProperty SubtitleProperty = DependencyProperty.Register(
        nameof(Subtitle), typeof(string), typeof(JointGauge), new PropertyMetadata(string.Empty));
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
    public static readonly DependencyProperty IsOnlineProperty = DependencyProperty.Register(
        nameof(IsOnline), typeof(bool), typeof(JointGauge), new PropertyMetadata(false));
    public static readonly DependencyProperty IsWarningProperty = DependencyProperty.Register(
        nameof(IsWarning), typeof(bool), typeof(JointGauge), new PropertyMetadata(false));
    private static readonly DependencyPropertyKey AngleDegreesPropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(AngleDegrees), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0));
    private static readonly DependencyPropertyKey PositionDegreesPropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(PositionDegrees), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0));
    private static readonly DependencyPropertyKey SpeedMagnitudePropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(SpeedMagnitude), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0));
    private static readonly DependencyPropertyKey TorqueMagnitudePropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(TorqueMagnitude), typeof(double), typeof(JointGauge), new PropertyMetadata(0.0));
    private static readonly DependencyPropertyKey ArcGeometryPropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(ArcGeometry), typeof(Geometry), typeof(JointGauge), new PropertyMetadata(Geometry.Empty));

    public JointGauge()
    {
        InitializeComponent();
        UpdateGauge();
    }

    public string Title { get => (string)GetValue(TitleProperty); set => SetValue(TitleProperty, value); }
    public string Subtitle { get => (string)GetValue(SubtitleProperty); set => SetValue(SubtitleProperty, value); }
    public double Position { get => (double)GetValue(PositionProperty); set => SetValue(PositionProperty, value); }
    public double Minimum { get => (double)GetValue(MinimumProperty); set => SetValue(MinimumProperty, value); }
    public double Maximum { get => (double)GetValue(MaximumProperty); set => SetValue(MaximumProperty, value); }
    public double Velocity { get => (double)GetValue(VelocityProperty); set => SetValue(VelocityProperty, value); }
    public double Torque { get => (double)GetValue(TorqueProperty); set => SetValue(TorqueProperty, value); }
    public string StatusText { get => (string)GetValue(StatusTextProperty); set => SetValue(StatusTextProperty, value); }
    public bool IsOnline { get => (bool)GetValue(IsOnlineProperty); set => SetValue(IsOnlineProperty, value); }
    public bool IsWarning { get => (bool)GetValue(IsWarningProperty); set => SetValue(IsWarningProperty, value); }
    public double AngleDegrees => (double)GetValue(AngleDegreesPropertyKey.DependencyProperty);
    public double PositionDegrees => (double)GetValue(PositionDegreesPropertyKey.DependencyProperty);
    public double SpeedMagnitude => (double)GetValue(SpeedMagnitudePropertyKey.DependencyProperty);
    public double TorqueMagnitude => (double)GetValue(TorqueMagnitudePropertyKey.DependencyProperty);
    public Geometry ArcGeometry => (Geometry)GetValue(ArcGeometryPropertyKey.DependencyProperty);

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
        SetValue(ArcGeometryPropertyKey, CreateArcGeometry(fraction));
    }

    private static Geometry CreateArcGeometry(double fraction)
    {
        if (fraction <= 0)
        {
            return Geometry.Empty;
        }

        const double centerX = 60;
        const double centerY = 52;
        const double radius = 40;
        const double startAngle = 135;
        var sweepAngle = Math.Clamp(fraction, 0, 1) * 270;
        var start = Polar(centerX, centerY, radius, startAngle);
        var end = Polar(centerX, centerY, radius, startAngle + sweepAngle);
        var figure = new PathFigure { StartPoint = start, IsClosed = false };
        figure.Segments.Add(new ArcSegment(
            end,
            new Size(radius, radius),
            0,
            sweepAngle > 180,
            SweepDirection.Clockwise,
            true));
        var geometry = new PathGeometry([figure]);
        geometry.Freeze();
        return geometry;
    }

    private static Point Polar(double centerX, double centerY, double radius, double angleDegrees)
    {
        var radians = angleDegrees * Math.PI / 180.0;
        return new Point(centerX + (radius * Math.Cos(radians)), centerY + (radius * Math.Sin(radians)));
    }
}
