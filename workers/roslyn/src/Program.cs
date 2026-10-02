using System.Collections.Concurrent;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;

namespace Verity.Roslyn;

internal sealed class Options
{
    public string Root = "";
    public string? Out;
    public List<string> Symbols = new() { "DEBUG", "TRACE" };
    public bool Progress;
    public int TimeoutSec;          // 0 = none
    public int FileTimeoutSec = 120;
    public int Threads = Math.Clamp(Environment.ProcessorCount, 1, 8);
    public long MaxFileBytes = 16L * 1024 * 1024;
    public string? Tfm;
    public bool TypeOcc = true;
    public int MaxDiags = 200;
}

public static class Program
{
    public const string Version = "0.1.0";
    private const int WorkerStack = 256 * 1024 * 1024;

    public static int Main(string[] args)
    {
        Options opt;
        try { opt = ParseArgs(args); }
        catch (ArgumentException ex)
        {
            Console.Error.WriteLine("error: " + ex.Message);
            Console.Error.WriteLine(Usage);
            return 1;
        }
        if (opt.Root == "--version") { Console.Out.WriteLine($"VerityRoslyn {Version} (Roslyn {typeof(CSharpCompilation).Assembly.GetName().Version})"); return 0; }
        if (!Directory.Exists(opt.Root)) { Console.Error.WriteLine($"error: repository root not found: {opt.Root}"); return 1; }
        opt.Root = Path.GetFullPath(opt.Root).TrimEnd('/', '\\');

        var sw = Stopwatch.StartNew();
        Stream raw;
        try
        {
            raw = opt.Out != null
                ? new FileStream(opt.Out, FileMode.Create, FileAccess.Write, FileShare.Read, 1 << 16)
                : Console.OpenStandardOutput();
        }
        catch (Exception ex) { Console.Error.WriteLine("error: cannot open output: " + ex.Message); return 1; }

        using var output = new BufferedStream(raw, 1 << 20);
        using var sink = new OrderedSink(output, opt.MaxDiags);
        using var globalCts = new CancellationTokenSource();
        var runner = new Runner(opt, sink, globalCts.Token);

        Exception? fatal = null;
        var main = new Thread(() =>
        {
            try { runner.Run(); }
            catch (OperationCanceledException) { }
            catch (Exception ex) { fatal = ex; }
        }, WorkerStack) { IsBackground = true, Name = "verity-main" };
        main.Start();

        bool timedOut = false;
        if (opt.TimeoutSec > 0)
        {
            if (!main.Join(TimeSpan.FromSeconds(opt.TimeoutSec)))
            {
                timedOut = true;
                globalCts.Cancel();
                // Grace period for workers to notice cancellation; never hang past it.
                main.Join(TimeSpan.FromSeconds(10));
            }
        }
        else main.Join();

        if (fatal != null) Console.Error.WriteLine("fatal: " + fatal);
        timedOut |= runner.TimedOut;
        var done = RecordBuffer.Line("done", w =>
        {
            w.WriteNumber("files", sink.Files + runner.SkippedFiles);
            w.WriteNumber("symbols", sink.Totals.GetValueOrDefault("symbol"));
            w.WriteNumber("errors", sink.CompileErrors);
            w.WriteNumber("elapsed_ms", sw.ElapsedMilliseconds);
            w.WriteNumber("projects", runner.Projects);
            w.WriteNumber("extractor_errors", sink.ExtractorErrors + (fatal != null ? 1 : 0));
            w.WriteNumber("skipped", runner.SkippedFiles);
            w.WriteBoolean("timed_out", timedOut);
            w.WriteString("worker", "roslyn " + Version);
            w.WriteStartObject("records");
            foreach (var kv in sink.Totals.OrderBy(k => k.Key, StringComparer.Ordinal)) w.WriteNumber(kv.Key, kv.Value);
            w.WriteEndObject();
            if (fatal != null) w.Str("fatal", J.Snip(fatal.GetType().Name + ": " + fatal.Message, 400));
        });
        sink.Close(done);
        output.Flush();
        if (opt.Progress) Console.Error.WriteLine($"DONE files={sink.Files} symbols={sink.Totals.GetValueOrDefault("symbol")} errors={sink.CompileErrors} elapsed_ms={sw.ElapsedMilliseconds}");
        return fatal != null ? 3 : timedOut ? 2 : 0;
    }

    private const string Usage =
        "usage: VerityRoslyn <repo_root> [--out facts.jsonl] [--symbols DEBUG;TRACE] [--progress]\n" +
        "                     [--timeout SEC] [--file-timeout SEC] [--threads N] [--tfm TFM] [--max-file-mb N] [--no-type-occ]\n" +
        "       VerityRoslyn --version";

    private static Options ParseArgs(string[] args)
    {
        var o = new Options();
        string? root = null;
        for (int i = 0; i < args.Length; i++)
        {
            string a = args[i];
            string Next() => i + 1 < args.Length ? args[++i] : throw new ArgumentException($"missing value for {a}");
            switch (a)
            {
                case "--version": return new Options { Root = "--version" };
                case "--out": case "-o": o.Out = Next(); break;
                case "--symbols": o.Symbols = Next().Split(new[] { ';', ',' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).ToList(); break;
                case "--progress": o.Progress = true; break;
                case "--timeout": o.TimeoutSec = int.Parse(Next()); break;
                case "--file-timeout": o.FileTimeoutSec = int.Parse(Next()); break;
                case "--threads": o.Threads = Math.Max(1, int.Parse(Next())); break;
                case "--tfm": o.Tfm = Next(); break;
                case "--max-file-mb": o.MaxFileBytes = long.Parse(Next()) * 1024 * 1024; break;
                case "--no-type-occ": o.TypeOcc = false; break;
                case "--max-diags": o.MaxDiags = int.Parse(Next()); break;
                case "-h": case "--help": throw new ArgumentException("help");
                default:
                    if (a.StartsWith("--", StringComparison.Ordinal)) throw new ArgumentException($"unknown option {a}");
                    if (root != null) throw new ArgumentException("only one repository root may be given");
                    root = a;
                    break;
            }
        }
        o.Root = root ?? throw new ArgumentException("repository root is required");
        return o;
    }
}

internal sealed class Runner
{
    private readonly Options _opt;
    private readonly OrderedSink _sink;
    private readonly CancellationToken _ct;
    private readonly Dictionary<string, CSharpCompilation> _compilations = new(StringComparer.Ordinal);
    private readonly Dictionary<string, HashSet<string>> _typeNames = new(StringComparer.Ordinal);
    private readonly ConcurrentDictionary<string, byte> _emittedGlobal = new(StringComparer.Ordinal);
    private readonly object _progressLock = new();
    private int _done;
    private int _total;

    public int Projects;
    public int SkippedFiles;
    public bool TimedOut;

    public Runner(Options opt, OrderedSink sink, CancellationToken ct)
    {
        _opt = opt;
        _sink = sink;
        _ct = ct;
    }

    public void Run()
    {
        var scan = Discovery.Scan(_opt.Root, _opt.MaxFileBytes);
        var projects = Discovery.BuildProjects(_opt.Root, scan, _opt.Tfm);
        _total = projects.Sum(p => p.Files.Count);
        if (_opt.Progress) Console.Error.WriteLine($"SCAN files={scan.CsFiles.Count} projects={projects.Count} skipped={scan.Skipped.Count}");

        foreach (var p in projects)
        {
            if (_ct.IsCancellationRequested) { TimedOut = true; break; }
            try { RunProject(p); }
            catch (OperationCanceledException) { TimedOut = true; break; }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"WARN project {p.Name} failed: {ex.GetType().Name}: {ex.Message}");
                _sink.WriteRaw(RecordBuffer.Line("diag", w =>
                {
                    w.Str("file", p.ProjectPath ?? "");
                    w.WriteNumber("line", 1);
                    w.WriteString("severity", "internal");
                    w.WriteString("code", "VR003");
                    w.Str("msg", J.Snip($"project analysis failed: {ex.GetType().Name}: {ex.Message}", 400));
                }));
            }
            _sink.ResetIndex();
        }

        foreach (var f in scan.Skipped)
        {
            SkippedFiles++;
            string rel = Discovery.Rel(_opt.Root, f);
            long len = 0;
            try { len = new FileInfo(f).Length; } catch { }
            _sink.WriteRaw(RecordBuffer.Line("file", w =>
            {
                w.WriteString("path", rel);
                w.WriteString("lang", "csharp");
                w.WriteString("sha", "");
                w.WriteNumber("loc", 0);
                w.WriteString("fidelity", "F4");
                w.WriteNumber("errors", 0);
                w.Arr("stubs", Array.Empty<string>());
                w.WriteString("profile", "skipped");
                w.WriteBoolean("skipped", true);
            }));
            _sink.WriteRaw(RecordBuffer.Line("diag", w =>
            {
                w.WriteString("file", rel);
                w.WriteNumber("line", 1);
                w.WriteString("severity", "warning");
                w.WriteString("code", "VR004");
                w.WriteString("msg", $"file skipped: {len} bytes exceeds --max-file-mb");
            }));
        }
    }

    private void RunProject(ProjectInfo p)
    {
        Projects++;
        var lang = References.ParseLang(p.LangVersion);
        var symbols = new List<string>(_opt.Symbols);
        symbols.AddRange(p.Defines);
        symbols.AddRange(References.TfmSymbols(p.Tfm));
        var parseOpts = new CSharpParseOptions(lang, DocumentationMode.Parse, SourceCodeKind.Regular, symbols.Distinct(StringComparer.Ordinal));

        // Parse (parallel, order preserved).
        var metas = new FileMeta?[p.Files.Count];
        var readErrors = new ConcurrentBag<(string Rel, string Msg)>();
        Parallel.For(0, p.Files.Count, new ParallelOptions { MaxDegreeOfParallelism = _opt.Threads, CancellationToken = _ct }, i =>
        {
            string abs = p.Files[i];
            string rel = Discovery.Rel(_opt.Root, abs);
            try
            {
                byte[] bytes = File.ReadAllBytes(abs);
                string sha = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
                var text = SourceText.From(bytes, bytes.Length, encoding: null, checksumAlgorithm: SourceHashAlgorithm.Sha256, throwIfBinaryDetected: false, canBeEmbedded: false);
                var tree = CSharpSyntaxTree.ParseText(text, parseOpts, rel, _ct);
                int loc = text.Length == 0 ? 0 : text.Lines.Count;
                if (loc > 0 && text.Lines[^1].Span.Length == 0) loc--; // trailing newline
                metas[i] = new FileMeta { Abs = abs, Rel = rel, Sha = sha, Loc = loc, Tree = tree };
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception ex) { readErrors.Add((rel, ex.Message)); }
        });

        var files = metas.Where(m => m != null).Cast<FileMeta>().ToList();
        var trees = new List<SyntaxTree>(files.Select(f => f.Tree));
        if (References.ImplicitUsingsSource(p, lang) is string gu)
            trees.Add(CSharpSyntaxTree.ParseText(gu, parseOpts, "<verity-implicit-usings>.g.cs", cancellationToken: _ct));

        var refs = new List<MetadataReference>(References.For(p.RefPack));
        var typeNames = new HashSet<string>(StringComparer.Ordinal);
        foreach (var dep in p.DependsOn)
        {
            if (_compilations.TryGetValue(dep, out var dc)) refs.Add(dc.ToMetadataReference());
            if (_typeNames.TryGetValue(dep, out var tn)) typeNames.UnionWith(tn);
        }
        foreach (var f in files) CollectTypeNames(f.Tree, typeNames);

        bool hasTopLevel = files.Any(f => f.Tree.GetRoot(_ct) is CompilationUnitSyntax cu && cu.Members.Any(m => m is GlobalStatementSyntax));
        var kind = p.OutputType.Equals("WinExe", StringComparison.OrdinalIgnoreCase) ? OutputKind.WindowsApplication
                 : p.OutputType.Equals("Exe", StringComparison.OrdinalIgnoreCase) || hasTopLevel ? OutputKind.ConsoleApplication
                 : OutputKind.DynamicallyLinkedLibrary;
        var compOpts = new CSharpCompilationOptions(kind,
            allowUnsafe: true,
            nullableContextOptions: p.NullableEnabled ? NullableContextOptions.Enable : NullableContextOptions.Disable,
            concurrentBuild: true,
            deterministic: true,
            metadataImportOptions: MetadataImportOptions.Public);
        var comp = CSharpCompilation.Create(p.AssemblyName ?? p.Name.Replace("(", "").Replace(")", ""), trees, refs, compOpts);
        _compilations[p.Name] = comp;
        _typeNames[p.Name] = typeNames;

        IMethodSymbol? entry = null;
        if (kind != OutputKind.DynamicallyLinkedLibrary)
        {
            try { entry = comp.GetEntryPoint(_ct); } catch (OperationCanceledException) { throw; } catch { }
        }

        string profile = $"{p.Tfm}{(p.TfmInferred ? " (inferred)" : "")}; refs={p.RefPack}; lang={lang.ToDisplayString()}";
        var ctx = new ProjectContext
        {
            Info = p,
            Compilation = comp,
            Keys = new KeyCache(),
            Opt = _opt,
            SourceTypeNames = typeNames,
            EmittedGlobal = _emittedGlobal,
            Profile = profile,
            EntryPoint = entry,
        };

        _sink.WriteRaw(RecordBuffer.Line("project", w =>
        {
            w.WriteString("name", p.Name);
            w.Str("path", p.ProjectPath);
            w.WriteString("tfm", p.Tfm);
            w.Arr("tfms", p.TargetFrameworks);
            w.WriteBoolean("tfm_inferred", p.TfmInferred);
            w.WriteString("refs", p.RefPack);
            w.WriteString("lang_version", lang.ToDisplayString());
            w.WriteString("output", kind.ToString());
            w.WriteNumber("files", files.Count);
            w.Arr("defines", symbols.Distinct(StringComparer.Ordinal));
            w.Arr("depends_on", p.DependsOn);
            w.WriteStartArray("packages");
            foreach (var pk in p.Packages)
            {
                w.WriteStartObject();
                w.WriteString("id", pk.Id);
                w.Str("version", pk.Version);
                w.WriteEndObject();
            }
            w.WriteEndArray();
            w.WriteBoolean("implicit_usings", p.ImplicitUsings);
            w.WriteBoolean("windows_desktop", p.UseWindowsForms || p.UseWpf);
        }));
        _sink.Count("project");
        foreach (var (rel, msg) in readErrors)
            _sink.WriteRaw(RecordBuffer.Line("diag", w =>
            {
                w.WriteString("file", rel);
                w.WriteNumber("line", 1);
                w.WriteString("severity", "internal");
                w.WriteString("code", "VR005");
                w.Str("msg", J.Snip("cannot read/parse file: " + msg, 400));
            }));

        RunParallel(files.Count, i => AnalyzeFile(ctx, files[i], i));
    }

    private static void CollectTypeNames(SyntaxTree tree, HashSet<string> names)
    {
        var root = tree.GetRoot();
        foreach (var n in root.DescendantNodes(n => n is CompilationUnitSyntax or BaseNamespaceDeclarationSyntax or TypeDeclarationSyntax))
        {
            if (n is BaseTypeDeclarationSyntax t) names.Add(t.Identifier.ValueText);
            else if (n is DelegateDeclarationSyntax d) names.Add(d.Identifier.ValueText);
        }
    }

    private void RunParallel(int count, Action<int> body)
    {
        int next = -1;
        int threads = Math.Min(_opt.Threads, Math.Max(1, count));
        var ts = new List<Thread>(threads);
        for (int t = 0; t < threads; t++)
        {
            var th = new Thread(() =>
            {
                while (!_ct.IsCancellationRequested)
                {
                    int i = Interlocked.Increment(ref next);
                    if (i >= count) break;
                    body(i);
                }
            }, 256 * 1024 * 1024) { IsBackground = true, Name = "verity-worker-" + t };
            th.Start();
            ts.Add(th);
        }
        foreach (var th in ts) th.Join();
        if (_ct.IsCancellationRequested) TimedOut = true;
    }

    private void AnalyzeFile(ProjectContext ctx, FileMeta meta, int index)
    {
        var fr = new FileResult { Index = index, Path = meta.Rel };
        var body = new RecordBuffer();
        byte[]? head = null;
        using var fileCts = CancellationTokenSource.CreateLinkedTokenSource(_ct);
        if (_opt.FileTimeoutSec > 0) fileCts.CancelAfter(TimeSpan.FromSeconds(_opt.FileTimeoutSec));
        FileAnalyzer? fa = null;
        try
        {
            var model = ctx.Compilation.GetSemanticModel(meta.Tree);
            fa = new FileAnalyzer(ctx, meta, model, body, fr, fileCts.Token);
            head = fa.Run();
        }
        catch (Exception ex)
        {
            fr.ExtractorErrors++;
            fr.Diags.Add(RecordBuffer.Line("diag", w =>
            {
                w.WriteString("file", meta.Rel);
                w.WriteNumber("line", 1);
                w.WriteString("severity", "internal");
                w.WriteString("code", "VR001");
                w.Str("msg", J.Snip($"file analysis failed: {ex.GetType().Name}: {ex.Message}", 400));
            }));
        }
        head ??= RecordBuffer.Line("file", w =>
        {
            w.WriteString("path", meta.Rel);
            w.WriteString("lang", "csharp");
            w.WriteString("sha", meta.Sha);
            w.WriteNumber("loc", meta.Loc);
            w.WriteString("fidelity", "F4");
            w.WriteNumber("errors", 0);
            w.Arr("stubs", Array.Empty<string>());
            w.WriteString("profile", ctx.Profile);
        });
        var bodySpan = body.Span;
        var data = new byte[head.Length + bodySpan.Length];
        head.CopyTo(data, 0);
        bodySpan.CopyTo(data.AsSpan(head.Length));
        fr.Data = data;
        fr.Counts = new Dictionary<string, int>(body.Counts, StringComparer.Ordinal);
        fr.Counts["file"] = fr.Counts.GetValueOrDefault("file") + 1;
        _sink.Submit(fr);

        int d = Interlocked.Increment(ref _done);
        if (_opt.Progress)
            lock (_progressLock) Console.Error.WriteLine($"PROGRESS {d}/{_total} {meta.Rel}");
    }
}
