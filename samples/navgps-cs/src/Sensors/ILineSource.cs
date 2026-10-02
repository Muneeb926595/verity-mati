using System.Text;

namespace Nav.Sensors;

/// <summary>A source of text lines, e.g. a serial port or a recorded log.</summary>
public interface ILineSource : IDisposable
{
    /// <summary>Reads the next line, or returns null when the source is exhausted.</summary>
    string? ReadLine();
}

/// <summary>Reads NMEA lines from any byte stream (serial device node, TCP, stdin).</summary>
public sealed class StreamLineSource : ILineSource
{
    private readonly StreamReader _reader;

    public StreamLineSource(Stream stream)
    {
        _reader = new StreamReader(stream, Encoding.ASCII);
    }

    public string? ReadLine() => _reader.ReadLine();

    public void Dispose() => _reader.Dispose();
}

/// <summary>Replays a recorded NMEA log file line by line.</summary>
public sealed class ReplayLineSource : ILineSource
{
    private readonly string[] _lines;
    private int _next;

    public ReplayLineSource(string path)
    {
        _lines = File.ReadAllLines(path);
    }

    public ReplayLineSource(IEnumerable<string> lines)
    {
        _lines = lines.ToArray();
    }

    public string? ReadLine()
    {
        if (_next >= _lines.Length)
            return null;
        return _lines[_next++];
    }

    public void Dispose()
    {
    }
}
