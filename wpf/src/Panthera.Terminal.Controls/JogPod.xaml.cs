using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;

namespace Panthera.Terminal.Controls;

public partial class JogPod : UserControl
{
    public static readonly DependencyProperty TitleProperty = DependencyProperty.Register(
        nameof(Title), typeof(string), typeof(JogPod), new PropertyMetadata("J1"));
    public static readonly DependencyProperty SubtitleProperty = DependencyProperty.Register(
        nameof(Subtitle), typeof(string), typeof(JogPod), new PropertyMetadata(string.Empty));
    public static readonly DependencyProperty PositionProperty = DependencyProperty.Register(
        nameof(Position), typeof(double), typeof(JogPod), new PropertyMetadata(0.0, OnRangeChanged));
    public static readonly DependencyProperty MinimumProperty = DependencyProperty.Register(
        nameof(Minimum), typeof(double), typeof(JogPod), new PropertyMetadata(-1.0, OnRangeChanged));
    public static readonly DependencyProperty MaximumProperty = DependencyProperty.Register(
        nameof(Maximum), typeof(double), typeof(JogPod), new PropertyMetadata(1.0, OnRangeChanged));
    public static readonly DependencyProperty NegativeParameterProperty = DependencyProperty.Register(
        nameof(NegativeParameter), typeof(object), typeof(JogPod));
    public static readonly DependencyProperty PositiveParameterProperty = DependencyProperty.Register(
        nameof(PositiveParameter), typeof(object), typeof(JogPod));
    public static readonly DependencyProperty PressCommandProperty = DependencyProperty.Register(
        nameof(PressCommand), typeof(ICommand), typeof(JogPod));
    public static readonly DependencyProperty ReleaseCommandProperty = DependencyProperty.Register(
        nameof(ReleaseCommand), typeof(ICommand), typeof(JogPod));
    private static readonly DependencyPropertyKey PositionPercentPropertyKey = DependencyProperty.RegisterReadOnly(
        nameof(PositionPercent), typeof(double), typeof(JogPod), new PropertyMetadata(50.0));

    public JogPod()
    {
        InitializeComponent();
        UpdatePositionPercent();
    }

    public string Title { get => (string)GetValue(TitleProperty); set => SetValue(TitleProperty, value); }
    public string Subtitle { get => (string)GetValue(SubtitleProperty); set => SetValue(SubtitleProperty, value); }
    public double Position { get => (double)GetValue(PositionProperty); set => SetValue(PositionProperty, value); }
    public double Minimum { get => (double)GetValue(MinimumProperty); set => SetValue(MinimumProperty, value); }
    public double Maximum { get => (double)GetValue(MaximumProperty); set => SetValue(MaximumProperty, value); }
    public object? NegativeParameter { get => GetValue(NegativeParameterProperty); set => SetValue(NegativeParameterProperty, value); }
    public object? PositiveParameter { get => GetValue(PositiveParameterProperty); set => SetValue(PositiveParameterProperty, value); }
    public ICommand? PressCommand { get => (ICommand?)GetValue(PressCommandProperty); set => SetValue(PressCommandProperty, value); }
    public ICommand? ReleaseCommand { get => (ICommand?)GetValue(ReleaseCommandProperty); set => SetValue(ReleaseCommandProperty, value); }
    public double PositionPercent => (double)GetValue(PositionPercentPropertyKey.DependencyProperty);

    private static void OnRangeChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs eventArgs) =>
        ((JogPod)dependencyObject).UpdatePositionPercent();

    private void UpdatePositionPercent()
    {
        var signedRange = Math.Max(Math.Abs(Minimum), Math.Abs(Maximum));
        var percent = signedRange <= 0
            ? 50.0
            : Math.Clamp(50.0 + Position / signedRange * 50.0, 0.0, 100.0);
        SetValue(PositionPercentPropertyKey, percent);
    }
}
