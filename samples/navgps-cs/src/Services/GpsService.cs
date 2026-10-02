using Nav.Sensors;

namespace Nav.Services;

/// <summary>Bridges the sensor event stream to the navigation controller.</summary>
public sealed class GpsService : IDisposable
{
    private readonly SerialGpsSensor _sensor;
    private readonly NavigationController _controller;
    private readonly NavOptions _options;
    private long _accepted;
    private long _rejected;

    public GpsService(SerialGpsSensor sensor, NavigationController controller, NavOptions options)
    {
        _sensor = sensor;
        _controller = controller;
        _options = options;
        _sensor.FixReceived += OnFix;
    }

    /// <summary>When false, incoming fixes are counted but ignored.</summary>
    public bool Enabled { get; set; } = true;

    public long Accepted => Interlocked.Read(ref _accepted);

    public long Rejected => Interlocked.Read(ref _rejected);

    private void OnFix(object? sender, GpsFixEventArgs e)
    {
        GpsFix fix = e.Fix;
        if (Enabled && fix.Quality >= _options.MinQuality)
        {
            if (_controller.ProcessGpsData(fix))
                Interlocked.Increment(ref _accepted);
            else
                Interlocked.Increment(ref _rejected);
        }
        else
        {
            Interlocked.Increment(ref _rejected);
        }
    }

    public void Dispose()
    {
        _sensor.FixReceived -= OnFix;
    }
}
