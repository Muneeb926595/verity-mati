namespace Nav.Control;

/// <summary>Textbook PID controller with integral anti-windup.</summary>
public sealed class PidController
{
    private readonly double _kp;
    private readonly double _ki;
    private readonly double _kd;
    private readonly double _integralLimit;
    private double _integral;
    private double _previousError;

    public PidController(double kp, double ki, double kd, double integralLimit = 50.0)
    {
        _kp = kp;
        _ki = ki;
        _kd = kd;
        _integralLimit = integralLimit;
    }

    public double Update(double error, double dtSeconds)
    {
        if (dtSeconds <= 0.0)
            return _kp * error;

        _integral = Math.Clamp(_integral + error * dtSeconds, -_integralLimit, _integralLimit);
        double derivative = (error - _previousError) / dtSeconds;
        _previousError = error;
        return _kp * error + _ki * _integral + _kd * derivative;
    }

    public void Reset()
    {
        _integral = 0.0;
        _previousError = 0.0;
    }
}
