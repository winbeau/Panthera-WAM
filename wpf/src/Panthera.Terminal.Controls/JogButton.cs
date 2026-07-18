using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;

namespace Panthera.Terminal.Controls;

public sealed class JogButton : Button
{
    public static readonly DependencyProperty PressCommandProperty = DependencyProperty.Register(
        nameof(PressCommand), typeof(ICommand), typeof(JogButton));
    public static readonly DependencyProperty ReleaseCommandProperty = DependencyProperty.Register(
        nameof(ReleaseCommand), typeof(ICommand), typeof(JogButton));
    public static readonly DependencyProperty JogParameterProperty = DependencyProperty.Register(
        nameof(JogParameter), typeof(object), typeof(JogButton));
    private bool _pressed;

    public ICommand? PressCommand { get => (ICommand?)GetValue(PressCommandProperty); set => SetValue(PressCommandProperty, value); }
    public ICommand? ReleaseCommand { get => (ICommand?)GetValue(ReleaseCommandProperty); set => SetValue(ReleaseCommandProperty, value); }
    public object? JogParameter { get => GetValue(JogParameterProperty); set => SetValue(JogParameterProperty, value); }

    protected override void OnPreviewMouseLeftButtonDown(MouseButtonEventArgs eventArgs)
    {
        base.OnPreviewMouseLeftButtonDown(eventArgs);
        CaptureMouse();
        BeginJog();
        eventArgs.Handled = true;
    }

    protected override void OnPreviewMouseLeftButtonUp(MouseButtonEventArgs eventArgs)
    {
        EndJog();
        ReleaseMouseCapture();
        base.OnPreviewMouseLeftButtonUp(eventArgs);
        eventArgs.Handled = true;
    }

    protected override void OnLostMouseCapture(MouseEventArgs eventArgs)
    {
        EndJog();
        base.OnLostMouseCapture(eventArgs);
    }

    protected override void OnMouseLeave(MouseEventArgs eventArgs)
    {
        if (_pressed && Mouse.LeftButton != MouseButtonState.Pressed)
        {
            EndJog();
        }
        base.OnMouseLeave(eventArgs);
    }

    protected override void OnPreviewKeyDown(KeyEventArgs eventArgs)
    {
        if (!eventArgs.IsRepeat && eventArgs.Key is Key.Space or Key.Enter)
        {
            BeginJog();
            eventArgs.Handled = true;
        }
        base.OnPreviewKeyDown(eventArgs);
    }

    protected override void OnPreviewKeyUp(KeyEventArgs eventArgs)
    {
        if (eventArgs.Key is Key.Space or Key.Enter)
        {
            EndJog();
            eventArgs.Handled = true;
        }
        base.OnPreviewKeyUp(eventArgs);
    }

    private void BeginJog()
    {
        if (_pressed || PressCommand?.CanExecute(JogParameter) != true)
        {
            return;
        }
        _pressed = true;
        PressCommand.Execute(JogParameter);
    }

    private void EndJog()
    {
        if (!_pressed)
        {
            return;
        }
        _pressed = false;
        if (ReleaseCommand?.CanExecute(null) == true)
        {
            ReleaseCommand.Execute(null);
        }
    }
}
