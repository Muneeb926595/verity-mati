using System.Globalization;

namespace Nav;

/// <summary>Which heading filter implementation the controller uses.</summary>
public enum HeadingFilterKind
{
    Complementary,
    Kalman,
}

/// <summary>Tunable navigation parameters.</summary>
public sealed class NavOptions
{
    /// <summary>Fixes with a horizontal dilution of precision above this are rejected.</summary>
    public double MaxHdop { get; set; } = 2.5;

    public FixQuality MinQuality { get; set; } = FixQuality.Gps;

    /// <summary>Below this speed the GPS course is noise and heading is not updated.</summary>
    public double MinSpeedForHeadingKnots { get; set; } = 0.5;

    /// <summary>Plausibility limit for ground speed.</summary>
    public double MaxSpeedMps { get; set; } = 90.0;

    public TimeSpan StaleFixTimeout { get; set; } = TimeSpan.FromSeconds(2);

    public HeadingFilterKind Filter { get; set; } = HeadingFilterKind.Complementary;

    public double ComplementaryAlpha { get; set; } = 0.8;

    public double KalmanProcessNoise { get; set; } = 0.5;

    public double KalmanMeasurementNoise { get; set; } = 4.0;

    public double Kp { get; set; } = 0.9;

    public double Ki { get; set; } = 0.05;

    public double Kd { get; set; } = 0.2;

    /// <summary>Maximum commanded bank angle in degrees (symmetric).</summary>
    public double MaxBankDeg { get; set; } = 25.0;

    public double TargetHeadingDeg { get; set; }

    public string? UdpEndpoint { get; set; }

    /// <summary>Parses --kalman, --max-hdop=N, --target=N and --udp=host:port.</summary>
    public static NavOptions FromArgs(string[] args)
    {
        var options = new NavOptions();
        foreach (string arg in args)
        {
            if (!arg.StartsWith("--", StringComparison.Ordinal))
                continue;

            string[] kv = arg.Substring(2).Split('=', 2);
            string value = kv.Length > 1 ? kv[1] : string.Empty;
            switch (kv[0])
            {
                case "kalman":
                    options.Filter = HeadingFilterKind.Kalman;
                    break;
                case "max-hdop":
                    options.MaxHdop = double.Parse(value, CultureInfo.InvariantCulture);
                    break;
                case "target":
                    options.TargetHeadingDeg = double.Parse(value, CultureInfo.InvariantCulture);
                    break;
                case "udp":
                    options.UdpEndpoint = value;
                    break;
            }
        }
        return options;
    }
}
