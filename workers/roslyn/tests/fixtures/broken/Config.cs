using Microsoft.Extensions.Configuration;

namespace Broken.App
{
    public class Settings
    {
        public double MaxHdop;
        public string Port;
        public string Conn;

        public Settings(IConfiguration config)
        {
            MaxHdop = config.GetValue<double>("Nav:MaxHdop");
            Port = config["Serial:Port"];
            var section = config.GetSection("Autopilot");
            Conn = config.GetConnectionString("Telemetry");
            int baud = int.Parse(config.GetSection("Serial")["Baud"]);
        }
    }
}
