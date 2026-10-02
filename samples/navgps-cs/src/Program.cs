using Nav.Control;
using Nav.Sensors;
using Nav.Services;

namespace Nav;

/// <summary>Wires sensor → service → controller → loop → autopilot link.</summary>
public static class Program
{
    public static int Main(string[] args)
    {
        NavOptions options = NavOptions.FromArgs(args);
        var state = new NavState();
        var controller = new NavigationController(options, state);

        if (args.Contains("--selftest"))
            return RunSelfTest(controller) ? 0 : 1;

        string? replayPath = args.FirstOrDefault(a => !a.StartsWith("--", StringComparison.Ordinal));
        using ILineSource source = replayPath != null
            ? new ReplayLineSource(replayPath)
            : new StreamLineSource(Console.OpenStandardInput());
        var sensor = new SerialGpsSensor(source);
        using var service = new GpsService(sensor, controller, options);

        using AutopilotTransport transport = CreateTransport(options);
        var link = new AutopilotLink(transport);
        var loop = new NavLoop(state, options, link);
        controller.SetTarget(options.TargetHeadingDeg);

        using var cts = new CancellationTokenSource();
        Console.CancelKeyPress += (_, e) =>
        {
            e.Cancel = true;
            cts.Cancel();
        };

        using var timer = new Timer(_ => loop.Tick(DateTime.UtcNow), null, TimeSpan.Zero, TimeSpan.FromMilliseconds(100));
        Task<int> pump = Task.Run(() => sensor.Pump(cts.Token));
        int fixes = pump.GetAwaiter().GetResult();

        Console.Error.WriteLine(
            $"fixes={fixes} accepted={service.Accepted} rejected={service.Rejected} " +
            $"badChecksums={sensor.BadChecksums} commands={link.CommandsSent} heading={state.CurrentHeading:F1}");
        return 0;
    }

    private static AutopilotTransport CreateTransport(NavOptions options)
    {
        if (options.UdpEndpoint is { Length: > 0 } endpoint)
        {
            string[] hostPort = endpoint.Split(':');
            return new UdpAutopilotTransport(hostPort[0], int.Parse(hostPort[1]));
        }
        return new StreamAutopilotTransport(Console.OpenStandardOutput());
    }

    private static bool RunSelfTest(NavigationController controller)
    {
        var fix = new GpsFix
        {
            TimestampUtc = DateTime.UtcNow,
            LatitudeDeg = 47.6205,
            LongitudeDeg = -122.3493,
            SpeedKnots = 12.0,
            CourseDeg = 90.0,
            Hdop = 0.9,
        };
        bool accepted = controller.ProcessGpsData(fix);
        controller.ApplyManualHeading(90.0);
        Console.WriteLine(accepted ? "selftest: fix accepted" : "selftest: fix rejected");
        return accepted;
    }
}
