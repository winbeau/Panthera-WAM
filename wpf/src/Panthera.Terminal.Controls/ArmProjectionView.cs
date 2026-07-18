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
        var background = TryFindResource(SystemColors.ControlBrushKey) as Brush ?? SystemColors.ControlBrush;
        var foreground = TryFindResource(SystemColors.ControlTextBrushKey) as Brush ?? SystemColors.ControlTextBrush;
        var grid = TryFindResource(SystemColors.InactiveBorderBrushKey) as Brush ?? SystemColors.InactiveBorderBrush;
        var accent = TryFindResource(SystemColors.HighlightBrushKey) as Brush ?? SystemColors.HighlightBrush;
        drawingContext.DrawRoundedRectangle(background, new Pen(grid, 1), new Rect(0, 0, ActualWidth, ActualHeight), 10, 10);
        if (ActualWidth < 40 || ActualHeight < 40)
        {
            return;
        }

        var center = new Point(ActualWidth / 2, ActualHeight / 2 + 8);
        var radius = Math.Min(ActualWidth, ActualHeight) * 0.34;
        for (var ring = 1; ring <= 3; ring++)
        {
            drawingContext.DrawEllipse(null, new Pen(grid, 0.8), center, radius * ring / 3, radius * ring / 3);
        }
        drawingContext.DrawLine(new Pen(grid, 0.8), new Point(center.X - radius, center.Y), new Point(center.X + radius, center.Y));
        drawingContext.DrawLine(new Pen(grid, 0.8), new Point(center.X, center.Y - radius), new Point(center.X, center.Y + radius));

        var (horizontal, vertical, title) = ViewMode.ToLowerInvariant() switch
        {
            "side" => (X, Z, "侧视图  X / Z"),
            "front" => (Y, Z, "主视图  Y / Z"),
            _ => (X, Y, "俯视图  X / Y"),
        };
        var scale = radius / 0.8;
        var point = new Point(center.X + Math.Clamp(horizontal * scale, -radius, radius), center.Y - Math.Clamp(vertical * scale, -radius, radius));
        drawingContext.DrawLine(new Pen(accent, 3), center, point);
        drawingContext.DrawEllipse(accent, new Pen(foreground, 1), point, 6, 6);
        drawingContext.DrawText(
            new FormattedText(title, CultureInfo.CurrentUICulture, FlowDirection.LeftToRight,
                new Typeface("Segoe UI Semibold"), 13, foreground, VisualTreeHelper.GetDpi(this).PixelsPerDip),
            new Point(12, 8));
        drawingContext.DrawText(
            new FormattedText($"TCP  {horizontal:+0.000;-0.000;0.000} / {vertical:+0.000;-0.000;0.000} m",
                CultureInfo.InvariantCulture, FlowDirection.LeftToRight, new Typeface("Consolas"), 11, foreground,
                VisualTreeHelper.GetDpi(this).PixelsPerDip),
            new Point(12, ActualHeight - 24));
    }
}
