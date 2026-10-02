namespace Nav.Util;

/// <summary>Small geodesy and angle helpers.</summary>
public static class GeoMath
{
    public const double EarthRadiusM = 6371000.0;

    /// <summary>Wraps an angle into the range (-180, 180].</summary>
    public static double Wrap180(double deg)
    {
        double d = deg % 360.0;
        if (d > 180.0)
            d -= 360.0;
        else if (d <= -180.0)
            d += 360.0;
        return d;
    }

    /// <summary>Normalises an angle into the range [0, 360).</summary>
    public static double Normalize360(double deg)
    {
        double d = deg % 360.0;
        return d < 0.0 ? d + 360.0 : d;
    }

    public static double ToRadians(double deg) => deg * Math.PI / 180.0;

    /// <summary>True when the coordinates are on the globe and not the (0,0) null island.</summary>
    public static bool IsValidPosition(double latDeg, double lonDeg)
    {
        if (double.IsNaN(latDeg) || double.IsNaN(lonDeg))
            return false;
        if (latDeg == 0.0 && lonDeg == 0.0)
            return false;
        return latDeg >= -90.0 && latDeg <= 90.0 && lonDeg >= -180.0 && lonDeg <= 180.0;
    }

    /// <summary>Great-circle distance in metres (haversine).</summary>
    public static double DistanceMeters(double lat1, double lon1, double lat2, double lon2)
    {
        double dLat = ToRadians(lat2 - lat1);
        double dLon = ToRadians(lon2 - lon1);
        double a = Math.Sin(dLat / 2) * Math.Sin(dLat / 2)
                 + Math.Cos(ToRadians(lat1)) * Math.Cos(ToRadians(lat2)) * Math.Sin(dLon / 2) * Math.Sin(dLon / 2);
        return 2.0 * EarthRadiusM * Math.Asin(Math.Min(1.0, Math.Sqrt(a)));
    }

    /// <summary>Initial bearing from point 1 to point 2, degrees true.</summary>
    public static double BearingDeg(double lat1, double lon1, double lat2, double lon2)
    {
        double phi1 = ToRadians(lat1);
        double phi2 = ToRadians(lat2);
        double dLon = ToRadians(lon2 - lon1);
        double y = Math.Sin(dLon) * Math.Cos(phi2);
        double x = Math.Cos(phi1) * Math.Sin(phi2) - Math.Sin(phi1) * Math.Cos(phi2) * Math.Cos(dLon);
        return Normalize360(Math.Atan2(y, x) * 180.0 / Math.PI);
    }
}
