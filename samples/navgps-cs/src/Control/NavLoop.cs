using Nav.Util;

namespace Nav.Control;

/// <summary>Periodic heading-hold loop: computes a bank command from heading error.</summary>
public sealed class NavLoop
{
    private readonly NavState _state;
    private readonly NavOptions _options;
    private readonly AutopilotLink _link;
    private readonly PidController _pid;
    private DateTime _lastTick = DateTime.MinValue;

    public NavLoop(NavState state, NavOptions options, AutopilotLink link)
    {
        _state = state;
        _options = options;
        _link = link;
        _pid = new PidController(options.Kp, options.Ki, options.Kd);
    }

    public double LastCommandDeg { get; private set; }

    /// <summary>One control step; called by a timer at ~10 Hz.</summary>
    public void Tick(DateTime now)
    {
        double dt = _lastTick == DateTime.MinValue ? 0.0 : (now - _lastTick).TotalSeconds;
        _lastTick = now;

        if (_state.Mode == NavMode.Initializing)
            return;

        if (now - _state.LastFixUtc > _options.StaleFixTimeout)
        {
            if (_state.Mode != NavMode.DeadReckoning)
            {
                _state.Mode = NavMode.DeadReckoning;
                _pid.Reset();
            }
        }

        double error = GeoMath.Wrap180(_state.TargetHeading - _state.CurrentHeading);
        double bank = _pid.Update(error, dt);

        if (bank > _options.MaxBankDeg)
            bank = _options.MaxBankDeg;
        else if (bank < -_options.MaxBankDeg)
            bank = -_options.MaxBankDeg;

        if (_state.Mode == NavMode.DeadReckoning)
            bank *= 0.5;

        LastCommandDeg = bank;
        _link.SendBankCommand(bank);
    }
}
