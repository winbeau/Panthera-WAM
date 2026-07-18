namespace Panthera.Terminal.Core;

public sealed class LatestStateSlot<T>
    where T : class
{
    private T? _value;
    private long _version;

    public long Publish(T value)
    {
        Interlocked.Exchange(ref _value, value);
        return Interlocked.Increment(ref _version);
    }

    public (T? Value, long Version) Read() => (Volatile.Read(ref _value), Volatile.Read(ref _version));
}
