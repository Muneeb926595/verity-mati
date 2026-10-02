using Nav.Sensors;
using Nav.Util;

namespace Nav;

/// <summary>Validates GPS fixes and folds them into the shared <see cref="NavState"/>.</summary>
public sealed class NavigationController
{
    private readonly NavOptions _options;
    private readonly NavState _state;
    private readonly IHeadingFilter _headingFilter;
    private DateTime _previousFixUtc = DateTime.MinValue;

    public NavigationController(NavOptions options, NavState state)
    {
        _options = options;
        _state = state;
        _headingFilter = CreateHeadingFilter(options);
    }

    public NavState State => _state;

    private static IHeadingFilter CreateHeadingFilter(NavOptions options)
    {
        switch (options.Filter)
        {
            case HeadingFilterKind.Kalman:
                return new KalmanHeadingFilter(options.KalmanProcessNoise, options.KalmanMeasurementNoise);
            default:
                return new ComplementaryHeadingFilter(options.ComplementaryAlpha);
        }
    }

    /// <summary>
    /// Accepts or rejects a fix. Rejected when HDOP is too high, the position is invalid or the
    /// speed is implausible. Heading is only updated above the minimum speed.
    /// </summary>
    public bool ProcessGpsData(GpsFix fix)
    {
        if (fix.Hdop > _options.MaxHdop)
        {
            _state.RejectedFixes++;
            return false;
        }

        if (!GeoMath.IsValidPosition(fix.LatitudeDeg, fix.LongitudeDeg))
        {
            _state.RejectedFixes++;
            return false;
        }

        double speedMps = fix.SpeedKnots * NmeaParser.KnotsToMetersPerSecond;
        if (speedMps > _options.MaxSpeedMps)
            return false;

        double dt = _previousFixUtc == DateTime.MinValue
            ? 0.0
            : (fix.TimestampUtc - _previousFixUtc).TotalSeconds;
        _previousFixUtc = fix.TimestampUtc;

        _state.LatitudeDeg = fix.LatitudeDeg;
        _state.LongitudeDeg = fix.LongitudeDeg;
        _state.GroundSpeedMps = speedMps;

        if (fix.SpeedKnots >= _options.MinSpeedForHeadingKnots)
        {
            _state.CurrentHeading = _headingFilter.Update(fix.CourseDeg, dt);
        }

        _state.LastFixUtc = fix.TimestampUtc;
        _state.Mode = NavMode.GpsTracking;
        return true;
    }

    /// <summary>Operator override: snaps the heading estimate to a known value.</summary>
    public void ApplyManualHeading(double headingDeg)
    {
        double heading = GeoMath.Normalize360(headingDeg);
        _headingFilter.Reset(heading);
        _state.CurrentHeading = heading;
    }

    public void SetTarget(double headingDeg)
    {
        _state.TargetHeading = GeoMath.Normalize360(headingDeg);
    }
}
