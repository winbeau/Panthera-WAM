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

    public JogButton()
    {
        IsEnabledChanged += HandleIsEnabledChanged;
        AddHandler(
            Mouse.PreviewMouseDownEvent,
            new MouseButtonEventHandler(HandlePreviewMouseDown),
            handledEventsToo: true);
        AddHandler(
            Mouse.PreviewMouseUpEvent,
            new MouseButtonEventHandler(HandlePreviewMouseUp),
            handledEventsToo: true);
        AddHandler(
            Keyboard.PreviewKeyDownEvent,
            new KeyEventHandler(HandlePreviewKeyDown),
            handledEventsToo: true);
        AddHandler(
            Keyboard.PreviewKeyUpEvent,
            new KeyEventHandler(HandlePreviewKeyUp),
            handledEventsToo: true);
    }

    public ICommand? PressCommand { get => (ICommand?)GetValue(PressCommandProperty); set => SetValue(PressCommandProperty, value); }
    public ICommand? ReleaseCommand { get => (ICommand?)GetValue(ReleaseCommandProperty); set => SetValue(ReleaseCommandProperty, value); }
    public object? JogParameter { get => GetValue(JogParameterProperty); set => SetValue(JogParameterProperty, value); }

    private void HandlePreviewMouseDown(object sender, MouseButtonEventArgs eventArgs)
    {
        if (eventArgs.ChangedButton != MouseButton.Left || _pressed)
        {
            return;
        }

        Focus();
        if (!CaptureMouse())
        {
            return;
        }
        BeginJog();
        eventArgs.Handled = true;
    }

    private void HandlePreviewMouseUp(object sender, MouseButtonEventArgs eventArgs)
    {
        if (eventArgs.ChangedButton != MouseButton.Left)
        {
            return;
        }

        EndJog();
        if (IsMouseCaptured)
        {
            ReleaseMouseCapture();
        }
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

    private void HandlePreviewKeyDown(object sender, KeyEventArgs eventArgs)
    {
        if (!eventArgs.IsRepeat && eventArgs.Key is Key.Space or Key.Enter)
        {
            BeginJog();
            eventArgs.Handled = true;
        }
    }

    private void HandlePreviewKeyUp(object sender, KeyEventArgs eventArgs)
    {
        if (eventArgs.Key is Key.Space or Key.Enter)
        {
            EndJog();
            eventArgs.Handled = true;
        }
    }

    protected override void OnLostKeyboardFocus(KeyboardFocusChangedEventArgs eventArgs)
    {
        EndJog();
        base.OnLostKeyboardFocus(eventArgs);
    }

    private void HandleIsEnabledChanged(object sender, DependencyPropertyChangedEventArgs eventArgs)
    {
        if (eventArgs.NewValue is false)
        {
            EndJog();
        }
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
