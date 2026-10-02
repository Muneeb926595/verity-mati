using System.Globalization;

namespace Nav;

/// <summary>Quality indicator of a GPS position fix (NMEA GGA field 6).</summary>
public enum FixQuality
{
    Invalid = 0,
    Gps = 1,
    Dgps = 2,
    Rtk = 4,
}

/// <summary>A single decoded GPS fix as delivered by the sensor layer.</summary>
public sealed class GpsFix
{
    /// <summary>UTC time of the fix as reported by the receiver.</summary>
    public DateTime TimestampUtc { get; init; }

    public double LatitudeDeg { get; init; }

    public double LongitudeDeg { get; init; }

    /// <summary>Speed over ground in knots.</summary>
    public double SpeedKnots { get; init; }

    /// <summary>Course over ground in degrees true (0..360).</summary>
    public double CourseDeg { get; init; }

    /// <summary>Horizontal dilution of precision; lower is better.</summary>
    public double Hdop { get; set; } = 1.0;

    public FixQuality Quality { get; set; } = FixQuality.Gps;

    public bool IsValid => Quality != FixQuality.Invalid;

    public override string ToString()
    {
        return string.Format(
            CultureInfo.InvariantCulture,
            "{0:O} lat={1:F5} lon={2:F5} sog={3:F1}kn cog={4:F1} hdop={5:F1} q={6}",
            TimestampUtc, LatitudeDeg, LongitudeDeg, SpeedKnots, CourseDeg, Hdop, Quality);
    }
}

/// <summary>Event payload for <see cref="Sensors.SerialGpsSensor.FixReceived"/>.</summary>
public sealed class GpsFixEventArgs : EventArgs
{
    public GpsFixEventArgs(GpsFix fix)
    {
        Fix = fix;
    }

    public GpsFix Fix { get; }
}
