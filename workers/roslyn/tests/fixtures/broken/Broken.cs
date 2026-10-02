using System;
using Newtonsoft.Json;            // package not available: error types
using System.IO.Ports;            // not in the net8 reference pack: SerialPort is an error type

namespace Broken.App
{
    public class Telemetry
    {
        private SerialPort _port = new SerialPort("/dev/ttyUSB0", 9600);
        private readonly JsonSerializerSettings _settings;
        public event EventHandler<string> LineReceived;

        public Telemetry()
        {
            _port.DataReceived += OnData;
        }

        private void OnData(object sender, EventArgs e)
        {
            string line = _port.ReadLine();
            var obj = JsonConvert.DeserializeObject<Packet>(line);
            if (obj == null) return;
            Process(obj, line);
            LineReceived?.Invoke(this, line);
        }

        public void Process(Packet p, string raw
        {
            if (p.Speed > 10)
                Undefined.Call(p.Speed);
            p.Speed = Clamp(p.Speed);
        }

        private static double Clamp(double v) => v > 100 ? 100 : v;

        public void Unfinished(
    }

    public class Packet { public double Speed { get; set; } }
