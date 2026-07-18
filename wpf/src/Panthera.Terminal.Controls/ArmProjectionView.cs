using System.Globalization;
using System.Windows;
using System.Windows.Media;

namespace Panthera.Terminal.Controls;

public sealed class ArmProjectionView : FrameworkElement
{
    public static readonly DependencyProperty ViewModeProperty = DependencyProperty.Register(
        nameof(ViewMode), typeof(string), typeof(ArmProjectionView), new FrameworkPropertyMetadata("Top", FrameworkPropertyMetadataOptions.AffectsRender));
    public static readonly DependencyProperty XProperty = DependencyProperty.Register(
        nameof(X), typeof(double), typeof(ArmProjectionView), new FrameworkPropertyMetadata(0.0, FrameworkPropertyMetadataOptions.AffectsRender));
    public static readonly DependencyProperty YProperty = DependencyProperty.Register(
        nameof(Y), typeof(double), typeof(ArmProjectionView), new FrameworkPropertyMetadata(0.0, FrameworkPropertyMetadataOptions.AffectsRender));
    public static readonly DependencyProperty ZProperty = DependencyProperty.Register(
        nameof(Z), typeof(double), typeof(ArmProjectionView), new FrameworkPropertyMetadata(0.0, FrameworkPropertyMetadataOptions.AffectsRender));

    public string ViewMode { get => (string)GetValue(ViewModeProperty); set => SetValue(ViewModeProperty, value); }
    public double X { get => (double)GetValue(XProperty); set => SetValue(XProperty, value); }
    public double Y { get => (double)GetValue(YProperty); set => SetValue(YProperty, value); }
    public double Z { get => (double)GetValue(ZProperty); set => SetValue(ZProperty, value); }

    protected override void OnRender(DrawingContext drawingContext)
    {
        base.OnRender(drawingContext);
        var background = FindBrush("SolidBackgroundFillColorSecondaryBrush", Brushes.Transparent);
        var glow = FindBrush("AccentFillColorDefaultBrush", SystemColors.HighlightBrush);
        var foreground = FindBrush("TextFillColorPrimaryBrush", SystemColors.ControlTextBrush);
        var secondary = FindBrush("TextFillColorTertiaryBrush", SystemColors.GrayTextBrush);
        var ring = FindBrush("DividerStrokeColorDefaultBrush", SystemColors.InactiveBorderBrush);
        var ringStrong = FindBrush("ControlStrongStrokeColorDefaultBrush", SystemColors.ActiveBorderBrush);
        var accent = FindBrush("SystemAccentColorPrimaryBrush", SystemColors.HighlightBrush);
        var tcp = FindBrush("SystemFillColorCriticalBrush", Brushes.IndianRed);

        drawingContext.DrawRectangle(background, null, new Rect(0, 0, ActualWidth, ActualHeight));
        if (ActualWidth < 80 || ActualHeight < 80)
        {
            return;
        }

        var center = new Point(ActualWidth / 2, (ActualHeight / 2) + 4);
        var radius = Math.Max(20, Math.Min(ActualWidth, ActualHeight) * 0.41);
        var glowBrush = new RadialGradientBrush(
            Color.FromArgb(78, GetColor(glow).R, GetColor(glow).G, GetColor(glow).B),
            Colors.Transparent)
        {
            RadiusX = 0.68,
            RadiusY = 0.68,
        };
        glowBrush.Freeze();
        drawingContext.DrawEllipse(glowBrush, null, center, radius, radius);

        var ringPen = new Pen(ring, 0.9);
        var strongPen = new Pen(ringStrong, 1.0);
        ringPen.Freeze();
        strongPen.Freeze();
        for (var ringIndex = 1; ringIndex <= 4; ringIndex++)
        {
            var ringRadius = radius * ringIndex / 4;
            drawingContext.DrawEllipse(null, ringIndex is 1 or 4 ? strongPen : ringPen, center, ringRadius, ringRadius);
        }

        var crossPen = new Pen(ring, 0.8) { DashStyle = new DashStyle([2.0, 5.0], 0) };
        crossPen.Freeze();
        drawingContext.DrawLine(crossPen, new Point(center.X - radius, center.Y), new Point(center.X + radius, center.Y));
        drawingContext.DrawLine(crossPen, new Point(center.X, center.Y - radius), new Point(center.X, center.Y + radius));

        DrawAzimuthTicks(drawingContext, center, radius, ringStrong, secondary);

        var (horizontal, vertical, plane) = ViewMode.ToLowerInvariant() switch
        {
            "side" => (X, Z, "SIDE · X / Z"),
            "front" => (Y, Z, "FRONT · Y / Z"),
            _ => (X, Y, "TOP · X / Y"),
        };
        var scale = radius / 0.72;
        var target = new Point(
            center.X + Math.Clamp(horizontal * scale, -radius * 0.93, radius * 0.93),
            center.Y - Math.Clamp(vertical * scale, -radius * 0.93, radius * 0.93));

        var glowPen = new Pen(new SolidColorBrush(Color.FromArgb(55, GetColor(accent).R, GetColor(accent).G, GetColor(accent).B)), 10)
        {
            StartLineCap = PenLineCap.Round,
            EndLineCap = PenLineCap.Round,
        };
        var armPen = new Pen(accent, 4)
        {
            StartLineCap = PenLineCap.Round,
            EndLineCap = PenLineCap.Round,
        };
        drawingContext.DrawLine(glowPen, center, target);
        drawingContext.DrawLine(armPen, center, target);
        drawingContext.DrawEllipse(background, new Pen(accent, 2), center, 8, 8);
        drawingContext.DrawEllipse(accent, new Pen(background, 1.5), target, 6, 6);
        drawingContext.DrawEllipse(null, new Pen(tcp, 1.5), target, 10, 10);
        drawingContext.DrawLine(new Pen(tcp, 1), new Point(target.X - 13, target.Y), new Point(target.X + 13, target.Y));
        drawingContext.DrawLine(new Pen(tcp, 1), new Point(target.X, target.Y - 13), new Point(target.X, target.Y + 13));

        DrawCallout(drawingContext, target, horizontal, vertical, foreground, secondary, background, ringStrong);
        DrawText(drawingContext, $"base frame · {plane}", 11, secondary, new Point(12, 10), "Cascadia Mono");
        DrawText(drawingContext, $"TCP  {X:+0.000;-0.000;0.000}  {Y:+0.000;-0.000;0.000}  {Z:+0.000;-0.000;0.000} m",
            10.5, secondary, new Point(12, ActualHeight - 24), "Cascadia Mono");
    }

    private void DrawAzimuthTicks(DrawingContext context, Point center, double radius, Brush tick, Brush text)
    {
        var pen = new Pen(tick, 1);
        for (var angle = 0; angle < 360; angle += 30)
        {
            var radians = angle * Math.PI / 180.0;
            var outer = new Point(center.X + (Math.Sin(radians) * radius), center.Y - (Math.Cos(radians) * radius));
            var innerRadius = radius - (angle % 90 == 0 ? 8 : 5);
            var inner = new Point(center.X + (Math.Sin(radians) * innerRadius), center.Y - (Math.Cos(radians) * innerRadius));
            context.DrawLine(pen, inner, outer);
        }
        DrawCenteredText(context, "0°", 9, text, new Point(center.X, center.Y - radius + 10));
        DrawCenteredText(context, "+90°", 9, text, new Point(center.X + radius - 18, center.Y - 4));
        DrawCenteredText(context, "180°", 9, text, new Point(center.X, center.Y + radius - 12));
        DrawCenteredText(context, "−90°", 9, text, new Point(center.X - radius + 18, center.Y - 4));
    }

    private void DrawCallout(
        DrawingContext context,
        Point target,
        double horizontal,
        double vertical,
        Brush foreground,
        Brush secondary,
        Brush background,
        Brush border)
    {
        const double width = 108;
        const double height = 43;
        var left = target.X + 14;
        if (left + width > ActualWidth - 8)
        {
            left = target.X - width - 14;
        }
        var top = Math.Clamp(target.Y - 21, 30, Math.Max(30, ActualHeight - height - 30));
        var rect = new Rect(left, top, width, height);
        context.DrawRoundedRectangle(background, new Pen(border, 1), rect, 5, 5);
        DrawText(context, $"U  {horizontal:+0.000;-0.000;0.000}", 9.5, foreground, new Point(left + 8, top + 6), "Cascadia Mono");
        DrawText(context, $"V  {vertical:+0.000;-0.000;0.000}", 9.5, secondary, new Point(left + 8, top + 22), "Cascadia Mono");
    }

    private void DrawCenteredText(DrawingContext context, string value, double size, Brush brush, Point center)
    {
        var text = CreateText(value, size, brush, "Cascadia Mono");
        context.DrawText(text, new Point(center.X - (text.Width / 2), center.Y - (text.Height / 2)));
    }

    private void DrawText(DrawingContext context, string value, double size, Brush brush, Point point, string family) =>
        context.DrawText(CreateText(value, size, brush, family), point);

    private FormattedText CreateText(string value, double size, Brush brush, string family) => new(
        value,
        CultureInfo.CurrentUICulture,
        FlowDirection.LeftToRight,
        new Typeface(family),
        size,
        brush,
        VisualTreeHelper.GetDpi(this).PixelsPerDip);

    private Brush FindBrush(string key, Brush fallback) => TryFindResource(key) as Brush ?? fallback;

    private static Color GetColor(Brush brush) => brush is SolidColorBrush solid ? solid.Color : Colors.Transparent;
}
