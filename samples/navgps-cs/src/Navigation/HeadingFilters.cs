using Nav.Util;

namespace Nav;

/// <summary>Smooths noisy GPS course-over-ground into a usable heading.</summary>
public interface IHeadingFilter
{
    /// <summary>Feeds a measured course (degrees) and returns the filtered heading (degrees).</summary>
    double Update(double measuredDeg, double dtSeconds);

    /// <summary>Forces the filter state to a known heading.</summary>
    void Reset(double headingDeg);
}

/// <summary>First-order complementary (exponential) heading filter.</summary>
public sealed class ComplementaryHeadingFilter : IHeadingFilter
{
    private readonly double _alpha;
    private double _estimate;
    private bool _initialized;

    public ComplementaryHeadingFilter(double alpha)
    {
        _alpha = Math.Clamp(alpha, 0.0, 1.0);
    }

    public double Update(double measuredDeg, double dtSeconds)
    {
        if (!_initialized)
        {
            Reset(measuredDeg);
            return _estimate;
        }

        double delta = GeoMath.Wrap180(measuredDeg - _estimate);
        _estimate = GeoMath.Normalize360(_estimate + (1.0 - _alpha) * delta);
        return _estimate;
    }

    public void Reset(double headingDeg)
    {
        _estimate = GeoMath.Normalize360(headingDeg);
        _initialized = true;
    }
}

/// <summary>Scalar Kalman filter over heading with angle wrapping on the innovation.</summary>
public sealed class KalmanHeadingFilter : IHeadingFilter
{
    private readonly double _processNoise;
    private readonly double _measurementNoise;
    private double _estimate;
    private double _variance = 1000.0;

    public KalmanHeadingFilter(double processNoise, double measurementNoise)
    {
        _processNoise = processNoise;
        _measurementNoise = measurementNoise;
    }

    public double Update(double measuredDeg, double dtSeconds)
    {
        _variance += _processNoise * Math.Max(dtSeconds, 0.0);
        double gain = _variance / (_variance + _measurementNoise);
        double innovation = GeoMath.Wrap180(measuredDeg - _estimate);
        _estimate = GeoMath.Normalize360(_estimate + gain * innovation);
        _variance = (1.0 - gain) * _variance;
        return _estimate;
    }

    public void Reset(double headingDeg)
    {
        _estimate = GeoMath.Normalize360(headingDeg);
        _variance = _measurementNoise;
    }
}
