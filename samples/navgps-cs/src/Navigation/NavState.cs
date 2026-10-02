namespace Nav;

/// <summary>Operating mode of the navigation stack.</summary>
public enum NavMode
{
    Initializing,
    GpsTracking,
    DeadReckoning,
}

/// <summary>Shared navigation state written by the controller and read by the control loop.</summary>
public sealed class NavState
{
    /// <summary>Filtered heading in degrees true.</summary>
    public double CurrentHeading { get; set; }

    /// <summary>Desired heading in degrees true.</summary>
    public double TargetHeading { get; set; }

    public double GroundSpeedMps { get; set; }

    public double LatitudeDeg { get; set; }

    public double LongitudeDeg { get; set; }

    /// <summary>UTC time of the last accepted fix.</summary>
    public DateTime LastFixUtc { get; set; } = DateTime.MinValue;

    public NavMode Mode { get; set; } = NavMode.Initializing;

    public int RejectedFixes { get; set; }
}
