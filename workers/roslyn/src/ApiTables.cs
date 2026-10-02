using Microsoft.CodeAnalysis;

namespace Verity.Roslyn;

/// <summary>Small data tables of well-known APIs ("apipack"): I/O sources/sinks, thread/timer starters, test attributes.</summary>
internal static class ApiTables
{
    // (declaring type full name, members, direction). Matching walks the receiver's base types and interfaces.
    private static readonly (string Type, string[] Members, string Dir)[] IoTable =
    {
        ("System.IO.Ports.SerialPort", new[] { "Read", "ReadLine", "ReadExisting", "ReadByte", "ReadChar", "ReadTo" }, "source"),
        ("System.IO.Ports.SerialPort", new[] { "Write", "WriteLine" }, "sink"),
        ("System.IO.Stream", new[] { "Read", "ReadAsync", "ReadByte", "ReadExactly", "ReadExactlyAsync", "ReadAtLeast", "ReadAtLeastAsync", "BeginRead" }, "source"),
        ("System.IO.Stream", new[] { "Write", "WriteAsync", "WriteByte", "BeginWrite" }, "sink"),
        ("System.Net.Sockets.NetworkStream", new[] { "Read", "ReadAsync", "ReadByte" }, "source"),
        ("System.Net.Sockets.NetworkStream", new[] { "Write", "WriteAsync", "WriteByte" }, "sink"),
        ("System.Net.Sockets.Socket", new[] { "Receive", "ReceiveAsync", "ReceiveFrom", "ReceiveFromAsync", "ReceiveMessageFrom", "ReceiveMessageFromAsync", "BeginReceive", "BeginReceiveFrom" }, "source"),
        ("System.Net.Sockets.Socket", new[] { "Send", "SendAsync", "SendTo", "SendToAsync", "SendFile", "SendFileAsync", "BeginSend", "BeginSendTo" }, "sink"),
        ("System.Net.Sockets.UdpClient", new[] { "Receive", "ReceiveAsync", "BeginReceive" }, "source"),
        ("System.Net.Sockets.UdpClient", new[] { "Send", "SendAsync", "BeginSend" }, "sink"),
        ("System.IO.File", new[] { "ReadAllText", "ReadAllLines", "ReadAllBytes", "ReadLines", "ReadAllTextAsync", "ReadAllLinesAsync", "ReadAllBytesAsync", "ReadLinesAsync", "OpenRead", "OpenText" }, "source"),
        ("System.IO.File", new[] { "WriteAllText", "WriteAllLines", "WriteAllBytes", "AppendAllText", "AppendAllLines", "WriteAllTextAsync", "WriteAllLinesAsync", "WriteAllBytesAsync", "AppendAllTextAsync", "AppendAllLinesAsync", "OpenWrite", "Create", "CreateText", "AppendText" }, "sink"),
        ("System.Console", new[] { "Read", "ReadLine", "ReadKey", "OpenStandardInput" }, "source"),
        ("System.Console", new[] { "Write", "WriteLine", "OpenStandardOutput", "OpenStandardError" }, "sink"),
        ("System.IO.TextReader", new[] { "Read", "ReadLine", "ReadToEnd", "ReadBlock", "ReadAsync", "ReadLineAsync", "ReadToEndAsync", "ReadBlockAsync" }, "source"),
        ("System.IO.TextWriter", new[] { "Write", "WriteLine", "WriteAsync", "WriteLineAsync" }, "sink"),
        ("System.IO.BinaryReader", new[] { "Read", "ReadBytes", "ReadByte", "ReadChars", "ReadChar", "ReadBoolean", "ReadInt16", "ReadUInt16", "ReadInt32", "ReadUInt32", "ReadInt64", "ReadUInt64", "ReadSingle", "ReadDouble", "ReadDecimal", "ReadString" }, "source"),
        ("System.IO.BinaryWriter", new[] { "Write" }, "sink"),
        ("System.Net.Http.HttpClient", new[] { "GetAsync", "GetStringAsync", "GetByteArrayAsync", "GetStreamAsync" }, "source"),
        ("System.Net.Http.HttpClient", new[] { "PostAsync", "PutAsync", "PatchAsync", "DeleteAsync", "SendAsync", "Send" }, "sink"),
        ("System.Net.WebClient", new[] { "DownloadString", "DownloadData", "DownloadFile", "DownloadStringTaskAsync", "DownloadDataTaskAsync", "OpenRead" }, "source"),
        ("System.Net.WebClient", new[] { "UploadString", "UploadData", "UploadFile", "UploadValues", "UploadStringTaskAsync", "OpenWrite" }, "sink"),
        ("System.Environment", new[] { "GetEnvironmentVariable", "GetCommandLineArgs" }, "source"),
        ("System.Diagnostics.Process", new[] { "Start" }, "sink"),
    };

    // Event-shaped I/O sources: subscribing a handler means data enters through it.
    private static readonly (string Type, string Event, string Dir)[] IoEvents =
    {
        ("System.IO.Ports.SerialPort", "DataReceived", "source"),
        ("System.Diagnostics.Process", "OutputDataReceived", "source"),
        ("System.IO.FileSystemWatcher", "Changed", "source"),
    };

    // Delegate-accepting APIs that start concurrent execution: (type, member (".ctor" for constructors), entry kind).
    private static readonly (string Type, string Member, string Kind)[] StartTable =
    {
        ("System.Threading.Thread", ".ctor", "thread"),
        ("System.Threading.Tasks.Task", "Run", "thread"),
        ("System.Threading.Tasks.Task", ".ctor", "thread"),
        ("System.Threading.Tasks.Task", "ContinueWith", "thread"),
        ("System.Threading.Tasks.Task`1", "ContinueWith", "thread"),
        ("System.Threading.Tasks.TaskFactory", "StartNew", "thread"),
        ("System.Threading.Tasks.TaskFactory`1", "StartNew", "thread"),
        ("System.Threading.ThreadPool", "QueueUserWorkItem", "thread"),
        ("System.Threading.ThreadPool", "UnsafeQueueUserWorkItem", "thread"),
        ("System.Threading.ThreadPool", "RegisterWaitForSingleObject", "thread"),
        ("System.Threading.Tasks.Parallel", "For", "thread"),
        ("System.Threading.Tasks.Parallel", "ForEach", "thread"),
        ("System.Threading.Tasks.Parallel", "ForEachAsync", "thread"),
        ("System.Threading.Tasks.Parallel", "Invoke", "thread"),
        ("System.Threading.Timer", ".ctor", "timer"),
        ("System.Threading.PeriodicTimer", ".ctor", "timer"),
        ("System.Windows.Threading.Dispatcher", "BeginInvoke", "callback"),
        ("System.Windows.Threading.Dispatcher", "InvokeAsync", "callback"),
        ("System.Windows.Forms.Control", "BeginInvoke", "callback"),
    };

    private static readonly (string Type, string Event, string Kind)[] EventKinds =
    {
        ("System.Timers.Timer", "Elapsed", "timer"),
        ("System.Windows.Forms.Timer", "Tick", "timer"),
        ("System.Windows.Threading.DispatcherTimer", "Tick", "timer"),
        ("System.ComponentModel.BackgroundWorker", "DoWork", "thread"),
        ("System.AppDomain", "UnhandledException", "handler"),
        ("System.AppDomain", "ProcessExit", "handler"),
        ("System.Console", "CancelKeyPress", "handler"),
    };

    public static readonly HashSet<string> TestAttributes = new(StringComparer.Ordinal)
    {
        "Fact", "Theory", "Test", "TestCase", "TestCaseSource", "TestMethod", "DataTestMethod", "SkippableFact", "SkippableTheory",
    };

    public static readonly HashSet<string> HandlerAttributes = new(StringComparer.Ordinal)
    {
        "HttpGet", "HttpPost", "HttpPut", "HttpDelete", "HttpPatch", "HttpHead", "HttpOptions", "Route", "Function", "FunctionName",
    };

    public static readonly HashSet<string> ConfigMethods = new(StringComparer.Ordinal)
    {
        "GetValue", "GetSection", "GetConnectionString", "GetRequiredSection",
    };

    private static readonly Dictionary<string, List<(string Type, string Dir)>> IoByMember = BuildIo();

    private static Dictionary<string, List<(string, string)>> BuildIo()
    {
        var d = new Dictionary<string, List<(string, string)>>(StringComparer.Ordinal);
        foreach (var (type, members, dir) in IoTable)
            foreach (var m in members)
            {
                if (!d.TryGetValue(m, out var l)) d[m] = l = new List<(string, string)>();
                l.Add((type, dir));
            }
        return d;
    }

    public static string FullName(ITypeSymbol t)
    {
        var def = t.OriginalDefinition;
        string ns = def.ContainingNamespace == null || def.ContainingNamespace.IsGlobalNamespace ? "" : def.ContainingNamespace.ToDisplayString() + ".";
        string name = def.MetadataName;
        var ct = def.ContainingType;
        while (ct != null) { name = ct.MetadataName + "+" + name; ct = ct.ContainingType; }
        return ns + name;
    }

    private static IEnumerable<ITypeSymbol> SelfAndBases(ITypeSymbol t)
    {
        for (ITypeSymbol? c = t; c != null; c = c.BaseType) yield return c;
        foreach (var i in t.AllInterfaces) yield return i;
    }

    /// <summary>Matches a resolved method against the I/O table. Returns (dir, api) or null.</summary>
    public static (string Dir, string Api)? MatchIo(IMethodSymbol m, ITypeSymbol? receiverType)
    {
        if (!IoByMember.TryGetValue(m.Name, out var cands)) return null;
        var owner = m.ContainingType;
        if (owner == null) return null;
        foreach (var t in SelfAndBases(owner))
        {
            string fn = FullName(t);
            foreach (var (type, dir) in cands)
                if (type == fn) return (dir, FullName(owner) + "." + m.Name);
        }
        return null;
    }

    /// <summary>Name-only match for unresolved receivers (error types from missing packages).</summary>
    public static (string Dir, string Api)? MatchIoByName(string typeName, string member)
    {
        if (!IoByMember.TryGetValue(member, out var cands)) return null;
        foreach (var (type, dir) in cands)
        {
            string simple = type[(type.LastIndexOf('.') + 1)..];
            if (simple == typeName) return (dir, type + "." + member);
        }
        return null;
    }

    public static (string Dir, string Api)? MatchIoEvent(IEventSymbol e)
    {
        var owner = e.ContainingType;
        if (owner == null) return null;
        foreach (var t in SelfAndBases(owner))
        {
            string fn = FullName(t);
            foreach (var (type, ev, dir) in IoEvents)
                if (type == fn && ev == e.Name) return (dir, type + "." + ev);
        }
        return null;
    }

    public static string? StartKind(IMethodSymbol m)
    {
        var owner = m.ContainingType;
        if (owner == null) return null;
        string member = m.MethodKind == MethodKind.Constructor ? ".ctor" : m.Name;
        foreach (var t in SelfAndBases(owner))
        {
            string fn = FullName(t);
            foreach (var (type, mem, kind) in StartTable)
                if (type == fn && mem == member) return kind;
        }
        return null;
    }

    public static string EventEntryKind(IEventSymbol? e)
    {
        if (e?.ContainingType == null) return "event";
        foreach (var t in SelfAndBases(e.ContainingType))
        {
            string fn = FullName(t);
            foreach (var (type, ev, kind) in EventKinds)
                if (type == fn && ev == e.Name) return kind;
        }
        return "event";
    }

    public static bool LooksLikeConfig(ITypeSymbol? t, string receiverText)
    {
        if (t != null && t.TypeKind != TypeKind.Error)
        {
            string n = t.Name;
            if (n.Contains("Configuration", StringComparison.Ordinal)) return true;
            if (n == "NameValueCollection" && receiverText.EndsWith("AppSettings", StringComparison.Ordinal)) return true;
            if (n == "ConnectionStringSettingsCollection") return true;
            foreach (var i in t.AllInterfaces) if (i.Name.StartsWith("IConfiguration", StringComparison.Ordinal)) return true;
            return false;
        }
        if (t != null && t.Name.Contains("Configuration", StringComparison.Ordinal)) return true;
        string last = receiverText[(receiverText.LastIndexOf('.') + 1)..];
        return last.Contains("config", StringComparison.OrdinalIgnoreCase) || last is "AppSettings" or "ConnectionStrings";
    }
}
