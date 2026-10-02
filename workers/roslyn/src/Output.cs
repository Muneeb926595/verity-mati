using System.Buffers;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Verity.Roslyn;

/// <summary>Growable byte buffer that supports truncation (used to roll back a half-written record).</summary>
internal sealed class ByteBuffer : IBufferWriter<byte>
{
    private byte[] _data;
    private int _len;

    public ByteBuffer(int initial = 16 * 1024) { _data = new byte[initial]; }

    public int Length => _len;

    public ReadOnlySpan<byte> Span => _data.AsSpan(0, _len);

    public void Advance(int count) => _len += count;

    public Memory<byte> GetMemory(int sizeHint = 0) { Ensure(sizeHint); return _data.AsMemory(_len); }

    public Span<byte> GetSpan(int sizeHint = 0) { Ensure(sizeHint); return _data.AsSpan(_len); }

    public void WriteByte(byte b) { Ensure(1); _data[_len++] = b; }

    public void Truncate(int len) { if (len < _len) _len = len; }

    public byte[] ToArray() => _data.AsSpan(0, _len).ToArray();

    private void Ensure(int hint)
    {
        if (hint < 256) hint = 256;
        if (_data.Length - _len >= hint) return;
        int size = Math.Max(_data.Length * 2, _len + hint);
        Array.Resize(ref _data, size);
    }
}

/// <summary>Per-file JSON Lines buffer. Each record is one line; a failing record is rolled back.</summary>
internal sealed class RecordBuffer
{
    private static readonly JsonWriterOptions Opts = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        SkipValidation = true,
        Indented = false,
    };

    private readonly ByteBuffer _buf = new();
    private readonly Utf8JsonWriter _w;

    public readonly Dictionary<string, int> Counts = new(StringComparer.Ordinal);
    public int EmitErrors;
    public string? FirstEmitError;

    public RecordBuffer() { _w = new Utf8JsonWriter(_buf, Opts); }

    public ReadOnlySpan<byte> Span => _buf.Span;

    public byte[] ToArray() => _buf.ToArray();

    public void Record(string t, Action<Utf8JsonWriter> body)
    {
        int start = _buf.Length;
        try
        {
            _w.WriteStartObject();
            _w.WriteString("t", t);
            body(_w);
            _w.WriteEndObject();
            _w.Flush();
            _buf.WriteByte((byte)'\n');
            Counts[t] = Counts.TryGetValue(t, out int c) ? c + 1 : 1;
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _w.Reset();
            _buf.Truncate(start);
            EmitErrors++;
            FirstEmitError ??= t + ": " + ex.GetType().Name + ": " + ex.Message;
        }
        finally
        {
            _w.Reset();
        }
    }

    /// <summary>Serialises a single standalone record line (used for diag/done records).</summary>
    public static byte[] Line(string t, Action<Utf8JsonWriter> body)
    {
        var rb = new RecordBuffer();
        rb.Record(t, body);
        return rb.ToArray();
    }
}

internal static class J
{
    /// <summary>Replaces lone surrogates so Utf8JsonWriter never throws on odd source text.</summary>
    public static string Clean(string s)
    {
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            if (char.IsSurrogate(c))
            {
                bool ok = char.IsHighSurrogate(c) && i + 1 < s.Length && char.IsLowSurrogate(s[i + 1]);
                if (!ok) return Rebuild(s);
                i++;
            }
        }
        return s;
    }

    private static string Rebuild(string s)
    {
        var sb = new StringBuilder(s.Length);
        for (int i = 0; i < s.Length; i++)
        {
            char c = s[i];
            if (char.IsHighSurrogate(c) && i + 1 < s.Length && char.IsLowSurrogate(s[i + 1])) { sb.Append(c).Append(s[i + 1]); i++; }
            else if (char.IsSurrogate(c)) sb.Append('�');
            else sb.Append(c);
        }
        return sb.ToString();
    }

    /// <summary>Collapses whitespace runs and truncates long source snippets.</summary>
    public static string Snip(string s, int max = 300)
    {
        var sb = new StringBuilder(Math.Min(s.Length, max + 8));
        bool ws = false;
        foreach (char c in s)
        {
            if (char.IsWhiteSpace(c)) { ws = true; continue; }
            if (ws && sb.Length > 0) sb.Append(' ');
            ws = false;
            sb.Append(c);
            if (sb.Length >= max) { sb.Append('…'); break; }
        }
        return Clean(sb.ToString());
    }

    public static void Str(this Utf8JsonWriter w, string name, string? value)
    {
        if (value == null) w.WriteNull(name); else w.WriteString(name, Clean(value));
    }

    public static void StrOpt(this Utf8JsonWriter w, string name, string? value)
    {
        if (value != null) w.WriteString(name, Clean(value));
    }

    public static void Arr(this Utf8JsonWriter w, string name, IEnumerable<string> values)
    {
        w.WriteStartArray(name);
        foreach (var v in values) w.WriteStringValue(Clean(v));
        w.WriteEndArray();
    }

    public static void Range(this Utf8JsonWriter w, string name, (int, int)? r)
    {
        if (r == null) { w.WriteNull(name); return; }
        w.WriteStartArray(name);
        w.WriteNumberValue(r.Value.Item1);
        w.WriteNumberValue(r.Value.Item2);
        w.WriteEndArray();
    }
}

/// <summary>Result of analysing one file, written to the output in deterministic file order.</summary>
internal sealed class FileResult
{
    public required int Index;
    public required string Path;
    public byte[] Data = Array.Empty<byte>();
    public List<byte[]> Diags = new();
    public Dictionary<string, int> Counts = new(StringComparer.Ordinal);
    public int CompileErrors;
    public int ExtractorErrors;
}

/// <summary>Writes per-file results in index order even though files finish out of order.</summary>
internal sealed class OrderedSink : IDisposable
{
    private readonly Stream _out;
    private readonly object _lock = new();
    private readonly Dictionary<int, FileResult> _pending = new();
    private int _next;
    private int _diagBudget;
    private bool _closed;

    public readonly Dictionary<string, int> Totals = new(StringComparer.Ordinal);
    public int Files;
    public int CompileErrors;
    public int ExtractorErrors;

    public OrderedSink(Stream output, int diagBudget)
    {
        _out = output;
        _diagBudget = diagBudget;
    }

    public object Lock => _lock;

    /// <summary>Writes a standalone line immediately (project headers). Caller must ensure ordering.</summary>
    public void WriteRaw(byte[] line)
    {
        lock (_lock)
        {
            if (_closed) return;
            _out.Write(line);
        }
    }

    public void Count(string t, int n = 1)
    {
        lock (_lock) Totals[t] = Totals.TryGetValue(t, out int c) ? c + n : n;
    }

    public void Submit(FileResult r)
    {
        lock (_lock)
        {
            if (_closed) return;
            _pending[r.Index] = r;
            while (_pending.Remove(_next, out var x))
            {
                WriteResult(x);
                _next++;
            }
        }
    }

    /// <summary>Starts a new index space (one per project) after flushing whatever is pending.</summary>
    public void ResetIndex()
    {
        lock (_lock)
        {
            FlushPendingSkippingGaps();
            _next = 0;
        }
    }

    public void FlushPendingSkippingGaps()
    {
        lock (_lock)
        {
            foreach (var k in _pending.Keys.OrderBy(k => k).ToList())
            {
                WriteResult(_pending[k]);
                _pending.Remove(k);
            }
        }
    }

    private void WriteResult(FileResult r)
    {
        if (_closed) return;
        _out.Write(r.Data);
        foreach (var kv in r.Counts) Totals[kv.Key] = Totals.TryGetValue(kv.Key, out int c) ? c + kv.Value : kv.Value;
        foreach (var d in r.Diags)
        {
            if (_diagBudget <= 0) break;
            _out.Write(d);
            _diagBudget--;
            Totals["diag"] = Totals.TryGetValue("diag", out int c) ? c + 1 : 1;
        }
        Files++;
        CompileErrors += r.CompileErrors;
        ExtractorErrors += r.ExtractorErrors;
    }

    /// <summary>Writes the final line and closes the sink; later submissions are ignored.</summary>
    public void Close(byte[] finalLine)
    {
        lock (_lock)
        {
            if (_closed) return;
            FlushPendingSkippingGaps();
            _out.Write(finalLine);
            _out.Flush();
            _closed = true;
        }
    }

    public void Dispose()
    {
        lock (_lock)
        {
            try { _out.Flush(); } catch { }
        }
    }
}
