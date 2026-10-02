using System.Globalization;
using Nav.Util;

namespace Nav.Sensors;

/// <summary>Minimal NMEA-0183 decoder for the RMC and GGA sentences.</summary>
public static class NmeaParser
{
    /// <summary>Conversion factor from knots to metres per second.</summary>
    public const double KnotsToMetersPerSecond = 0.514444;

    /// <summary>Verifies the XOR checksum between '$' and '*'.</summary>
    public static bool ValidateChecksum(string sentence)
    {
        if (string.IsNullOrEmpty(sentence) || sentence[0] != '$')
            return false;

        int star = sentence.LastIndexOf('*');
        if (star < 0 || star + 3 > sentence.Length)
            return false;

        byte sum = 0;
        for (int i = 1; i < star; i++)
            sum ^= (byte)sentence[i];

        string given = sentence.Substring(star + 1, 2);
        return byte.TryParse(given, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out byte expected)
            && expected == sum;
    }

    /// <summary>Parses a $xxRMC sentence into a fix; returns null when malformed.</summary>
    public static GpsFix? ParseRmc(string sentence)
    {
        if (!ValidateChecksum(sentence))
            return null;

        string[] f = Fields(sentence);
        if (f.Length < 10 || !f[0].EndsWith("RMC", StringComparison.Ordinal))
            return null;

        if (f[2] != "A")
            return new GpsFix { Quality = FixQuality.Invalid };

        DateTime timestamp = ParseTimestamp(f[1], f[9]);
        double lat = ParseCoordinate(f[3], f[4]);
        double lon = ParseCoordinate(f[5], f[6]);
        double knots = ParseKnots(f[7]);
        double course = ParseCourse(f[8]);

        return new GpsFix
        {
            TimestampUtc = timestamp,
            LatitudeDeg = lat,
            LongitudeDeg = lon,
            SpeedKnots = knots,
            CourseDeg = course,
        };
    }

    /// <summary>Extracts HDOP and fix quality from a $xxGGA sentence.</summary>
    public static bool TryParseGga(string sentence, out double hdop, out FixQuality quality)
    {
        hdop = 99.0;
        quality = FixQuality.Invalid;
        if (!ValidateChecksum(sentence))
            return false;

        string[] f = Fields(sentence);
        if (f.Length < 9 || !f[0].EndsWith("GGA", StringComparison.Ordinal))
            return false;

        if (int.TryParse(f[6], NumberStyles.Integer, CultureInfo.InvariantCulture, out int q))
            quality = (FixQuality)q;
        if (!double.TryParse(f[8], NumberStyles.Float, CultureInfo.InvariantCulture, out hdop))
            hdop = 99.0;
        return true;
    }

    /// <summary>Speed field in knots; empty or garbage means stationary.</summary>
    public static double ParseKnots(string field)
    {
        if (double.TryParse(field, NumberStyles.Float, CultureInfo.InvariantCulture, out double knots) && knots >= 0.0)
            return knots;
        return 0.0;
    }

    /// <summary>Course-over-ground field in degrees, normalised to [0,360).</summary>
    public static double ParseCourse(string field)
    {
        if (!double.TryParse(field, NumberStyles.Float, CultureInfo.InvariantCulture, out double course))
            return 0.0;
        return GeoMath.Normalize360(course);
    }

    /// <summary>Converts NMEA ddmm.mmmm plus hemisphere into signed decimal degrees.</summary>
    public static double ParseCoordinate(string value, string hemisphere)
    {
        if (!double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out double raw))
            return double.NaN;
        double degrees = Math.Floor(raw / 100.0);
        double minutes = raw - degrees * 100.0;
        double result = degrees + minutes / 60.0;
        return hemisphere == "S" || hemisphere == "W" ? -result : result;
    }

    private static DateTime ParseTimestamp(string hhmmss, string ddmmyy)
    {
        const string format = "ddMMyy HHmmss.FF";
        string text = ddmmyy + " " + (hhmmss.Contains('.') ? hhmmss : hhmmss + ".00");
        return DateTime.TryParseExact(text, format, CultureInfo.InvariantCulture,
            DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out DateTime ts)
            ? ts
            : DateTime.UtcNow;
    }

    private static string[] Fields(string sentence)
    {
        int star = sentence.IndexOf('*');
        return sentence.Substring(1, star - 1).Split(',');
    }
}
