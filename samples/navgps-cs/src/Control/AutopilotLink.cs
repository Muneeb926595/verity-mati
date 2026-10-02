using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace Nav.Control;

/// <summary>Byte transport towards the autopilot.</summary>
public abstract class AutopilotTransport : IDisposable
{
    public abstract void Send(byte[] payload);

    public virtual void Dispose()
    {
    }
}

/// <summary>Sends autopilot frames as UDP datagrams.</summary>
public sealed class UdpAutopilotTransport : AutopilotTransport
{
    private readonly UdpClient _client;
    private readonly IPEndPoint _endpoint;

    public UdpAutopilotTransport(string host, int port)
    {
        _client = new UdpClient();
        _endpoint = new IPEndPoint(IPAddress.Parse(host), port);
    }

    public override void Send(byte[] payload)
    {
        _client.Send(payload, payload.Length, _endpoint);
    }

    public override void Dispose()
    {
        _client.Dispose();
    }
}

/// <summary>Writes autopilot frames to a stream (serial device node or a log).</summary>
public sealed class StreamAutopilotTransport : AutopilotTransport
{
    private readonly Stream _stream;

    public StreamAutopilotTransport(Stream stream)
    {
        _stream = stream;
    }

    public override void Send(byte[] payload)
    {
        _stream.Write(payload, 0, payload.Length);
        _stream.Flush();
    }
}

/// <summary>Encodes bank-angle commands into $APBNK frames and ships them.</summary>
public sealed class AutopilotLink
{
    private readonly AutopilotTransport _transport;
    private int _sequence;

    public AutopilotLink(AutopilotTransport transport)
    {
        _transport = transport;
    }

    public int CommandsSent => _sequence;

    /// <summary>Sends a bank command in degrees (positive = right wing down).</summary>
    public void SendBankCommand(double bankDeg)
    {
        if (double.IsNaN(bankDeg) || double.IsInfinity(bankDeg))
            throw new ArgumentOutOfRangeException(nameof(bankDeg), bankDeg, "bank must be finite");

        string frame = FormatFrame(++_sequence, bankDeg);
        byte[] payload = Encoding.ASCII.GetBytes(frame);
        _transport.Send(payload);
    }

    internal static string FormatFrame(int sequence, double bankDeg)
    {
        string body = string.Format(CultureInfo.InvariantCulture, "APBNK,{0},{1:F2}", sequence, bankDeg);
        byte checksum = 0;
        foreach (char c in body)
            checksum ^= (byte)c;
        return "$" + body + "*" + checksum.ToString("X2", CultureInfo.InvariantCulture) + "\r\n";
    }
}
