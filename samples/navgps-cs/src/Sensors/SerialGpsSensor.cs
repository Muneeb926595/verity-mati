namespace Nav.Sensors;

/// <summary>
/// Reads NMEA sentences from a line source (normally a serial port), merges GGA quality
/// data into RMC fixes and raises <see cref="FixReceived"/> for every decoded fix.
/// </summary>
public sealed class SerialGpsSensor
{
    private readonly ILineSource _source;
    private double _lastHdop = 99.0;
    private FixQuality _lastQuality = FixQuality.Invalid;
    private int _badChecksums;

    public SerialGpsSensor(ILineSource source)
    {
        _source = source;
    }

    /// <summary>Raised on the reader thread for every decoded RMC fix.</summary>
    public event EventHandler<GpsFixEventArgs>? FixReceived;

    public int BadChecksums => _badChecksums;

    /// <summary>Reads and dispatches lines until the source ends or cancellation is requested.</summary>
    public int Pump(CancellationToken token)
    {
        int fixes = 0;
        while (!token.IsCancellationRequested)
        {
            string? line = _source.ReadLine();
            if (line == null)
                break;

            if (HandleLine(line.Trim()))
                fixes++;
        }
        return fixes;
    }

    internal bool HandleLine(string line)
    {
        if (line.Length == 0)
            return false;

        if (!NmeaParser.ValidateChecksum(line))
        {
            _badChecksums++;
            return false;
        }

        if (line.Contains("GGA", StringComparison.Ordinal))
        {
            if (NmeaParser.TryParseGga(line, out double hdop, out FixQuality quality))
            {
                _lastHdop = hdop;
                _lastQuality = quality;
            }
            return false;
        }

        GpsFix? fix = NmeaParser.ParseRmc(line);
        if (fix == null)
            return false;

        fix.Hdop = _lastHdop;
        if (fix.Quality != FixQuality.Invalid)
            fix.Quality = _lastQuality == FixQuality.Invalid ? FixQuality.Gps : _lastQuality;

        OnFixReceived(fix);
        return true;
    }

    private void OnFixReceived(GpsFix fix)
    {
        FixReceived?.Invoke(this, new GpsFixEventArgs(fix));
    }
}
