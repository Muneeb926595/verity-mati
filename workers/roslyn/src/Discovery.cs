using System.Text.RegularExpressions;
using System.Xml.Linq;

namespace Verity.Roslyn;

internal sealed class PackageRef
{
    public required string Id;
    public string? Version;
}

/// <summary>One analysis unit: a csproj (or the loose files of the tree).</summary>
internal sealed class ProjectInfo
{
    public required string Name;
    public string? ProjectPath;            // relative to root, '/' separated; null for loose group
    public required string Dir;            // absolute
    public List<string> TargetFrameworks = new();
    public string Tfm = "net8.0";
    public string RefPack = "Net80";
    public bool TfmInferred;
    public List<PackageRef> Packages = new();
    public List<string> ProjectRefs = new(); // absolute csproj paths
    public List<string> Defines = new();
    public string? LangVersion;
    public bool NullableEnabled;
    public bool ImplicitUsings;
    public bool UseWindowsForms;
    public bool UseWpf;
    public string Sdk = "";
    public string OutputType = "Library";
    public string? AssemblyName;
    public List<(string Name, string? Alias, bool Static, bool Remove)> Usings = new();
    public List<string> ExplicitCompile = new(); // absolute, for old-style projects
    public bool OldStyle;
    public List<string> Files = new();       // absolute paths of .cs files
    public List<string> DependsOn = new();   // project names
}

internal static class Discovery
{
    private static readonly HashSet<string> SkipDirs = new(StringComparer.OrdinalIgnoreCase)
    {
        "bin", "obj", ".git", "node_modules", ".vs", ".idea", ".build", ".svn", ".hg",
    };

    public sealed class Result
    {
        public List<string> CsFiles = new();
        public List<string> Csproj = new();
        public List<string> BuildProps = new();
        public bool HasXaml;
        public List<string> Skipped = new();
    }

    public static Result Scan(string root, long maxFileBytes)
    {
        var r = new Result();
        var stack = new Stack<DirectoryInfo>();
        stack.Push(new DirectoryInfo(root));
        while (stack.Count > 0)
        {
            var dir = stack.Pop();
            IEnumerable<FileSystemInfo> entries;
            try { entries = dir.EnumerateFileSystemInfos().ToList(); }
            catch { continue; }
            foreach (var e in entries)
            {
                try
                {
                    if (e is DirectoryInfo d)
                    {
                        if ((d.Attributes & FileAttributes.ReparsePoint) != 0) continue; // avoid symlink loops
                        if (SkipDirs.Contains(d.Name)) continue;
                        if (d.Name.StartsWith('.') && d.Name.Length > 1) continue;
                        stack.Push(d);
                    }
                    else if (e is FileInfo f)
                    {
                        string ext = f.Extension;
                        if (ext.Equals(".cs", StringComparison.OrdinalIgnoreCase))
                        {
                            if (f.Length > maxFileBytes) { r.Skipped.Add(f.FullName); continue; }
                            r.CsFiles.Add(f.FullName);
                        }
                        else if (ext.Equals(".csproj", StringComparison.OrdinalIgnoreCase)) r.Csproj.Add(f.FullName);
                        else if (ext.Equals(".xaml", StringComparison.OrdinalIgnoreCase)) r.HasXaml = true;
                        else if (f.Name.Equals("Directory.Build.props", StringComparison.OrdinalIgnoreCase)) r.BuildProps.Add(f.FullName);
                    }
                }
                catch { }
            }
        }
        r.CsFiles.Sort(StringComparer.Ordinal);
        r.Csproj.Sort(StringComparer.Ordinal);
        return r;
    }

    public static string Rel(string root, string abs)
    {
        string rel = Path.GetRelativePath(root, abs).Replace('\\', '/');
        return rel;
    }

    /// <summary>Builds analysis units: one per csproj plus a loose group for unowned files.</summary>
    public static List<ProjectInfo> BuildProjects(string root, Result scan, string? tfmOverride)
    {
        var projects = new List<ProjectInfo>();
        foreach (var csproj in scan.Csproj)
        {
            try { projects.Add(ParseCsproj(root, csproj, scan.BuildProps)); }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"WARN cannot parse {csproj}: {ex.Message}");
                projects.Add(new ProjectInfo { Name = Path.GetFileNameWithoutExtension(csproj), ProjectPath = Rel(root, csproj), Dir = Path.GetDirectoryName(csproj)! });
            }
        }

        // Assign files: explicit old-style Compile items first, then nearest project directory.
        var owner = new Dictionary<string, ProjectInfo>(StringComparer.Ordinal);
        foreach (var p in projects.Where(p => p.OldStyle && p.ExplicitCompile.Count > 0))
            foreach (var f in p.ExplicitCompile)
                if (File.Exists(f) && !owner.ContainsKey(f)) owner[f] = p;

        var byDir = projects.Where(p => !(p.OldStyle && p.ExplicitCompile.Count > 0))
            .GroupBy(p => Norm(p.Dir))
            .ToDictionary(g => g.Key, g => g.OrderBy(p => p.Name, StringComparer.Ordinal).First(), StringComparer.Ordinal);
        var explicitDirs = new HashSet<string>(projects.Where(p => p.OldStyle && p.ExplicitCompile.Count > 0).Select(p => Norm(p.Dir)), StringComparer.Ordinal);

        var loose = new ProjectInfo { Name = "(loose)", Dir = root };
        foreach (var f in scan.CsFiles)
        {
            if (owner.TryGetValue(f, out var op)) { op.Files.Add(f); continue; }
            string? dir = Norm(Path.GetDirectoryName(f)!);
            ProjectInfo? found = null;
            while (dir != null)
            {
                if (byDir.TryGetValue(dir, out var p)) { found = p; break; }
                if (explicitDirs.Contains(dir)) break; // owned by an old-style project that did not list it: loose
                if (dir.Length <= Norm(root).Length) break;
                dir = Path.GetDirectoryName(dir) is string pd ? Norm(pd) : null;
            }
            (found ?? loose).Files.Add(f);
        }
        if (loose.Files.Count > 0) projects.Add(loose);

        // Target framework resolution.
        foreach (var p in projects) ResolveFramework(p, scan, tfmOverride);

        // Project references → dependency names.
        var byPath = projects.Where(p => p.ProjectPath != null)
            .ToDictionary(p => Norm(Path.Combine(root, p.ProjectPath!)), p => p, StringComparer.Ordinal);
        foreach (var p in projects)
            foreach (var pr in p.ProjectRefs)
                if (byPath.TryGetValue(Norm(pr), out var dep) && dep != p) p.DependsOn.Add(dep.Name);

        return TopoSort(projects);
    }

    private static string Norm(string p) => Path.GetFullPath(p).TrimEnd('/', '\\');

    private static List<ProjectInfo> TopoSort(List<ProjectInfo> projects)
    {
        // Disambiguate duplicate names so dependency lookup is well defined.
        var seen = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var p in projects)
        {
            if (seen.TryGetValue(p.Name, out int n)) { seen[p.Name] = n + 1; p.Name = p.Name + "#" + (n + 1); }
            else seen[p.Name] = 1;
        }
        var byName = projects.ToDictionary(p => p.Name, StringComparer.Ordinal);
        var result = new List<ProjectInfo>();
        var state = new Dictionary<string, int>(StringComparer.Ordinal);
        void Visit(ProjectInfo p)
        {
            if (state.TryGetValue(p.Name, out int s)) { return; } // visiting (cycle) or done
            state[p.Name] = 1;
            foreach (var d in p.DependsOn.OrderBy(x => x, StringComparer.Ordinal))
                if (byName.TryGetValue(d, out var dp)) Visit(dp);
            state[p.Name] = 2;
            result.Add(p);
        }
        foreach (var p in projects.OrderBy(p => p.ProjectPath ?? "￿", StringComparer.Ordinal)) Visit(p);
        return result;
    }

    private static ProjectInfo ParseCsproj(string root, string csproj, List<string> buildProps)
    {
        var doc = XDocument.Load(csproj, LoadOptions.None);
        var proj = doc.Root!;
        var p = new ProjectInfo
        {
            Name = Path.GetFileNameWithoutExtension(csproj),
            ProjectPath = Rel(root, csproj),
            Dir = Path.GetDirectoryName(csproj)!,
            Sdk = (string?)proj.Attribute("Sdk") ?? "",
        };
        if (p.Sdk.Length == 0)
        {
            var sdkEl = proj.Elements().FirstOrDefault(e => e.Name.LocalName == "Sdk");
            if (sdkEl != null) p.Sdk = (string?)sdkEl.Attribute("Name") ?? "";
            else if (proj.Elements().Any(e => e.Name.LocalName == "Import" && ((string?)e.Attribute("Sdk"))?.Length > 0))
                p.Sdk = (string?)proj.Elements().First(e => e.Name.LocalName == "Import" && ((string?)e.Attribute("Sdk"))?.Length > 0).Attribute("Sdk") ?? "";
        }
        p.OldStyle = p.Sdk.Length == 0;

        // Properties: Directory.Build.props (nearest wins) then the csproj itself.
        var props = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var defines = new List<string>();
        foreach (var bp in buildProps.Where(bp => IsAncestorDir(Path.GetDirectoryName(bp)!, p.Dir) && IsAncestorDir(root, Path.GetDirectoryName(bp)!))
                                     .OrderBy(bp => bp.Length))
        {
            try { ReadProps(XDocument.Load(bp).Root!, props, defines); } catch { }
        }
        ReadProps(proj, props, defines);

        string? Get(string k) => props.TryGetValue(k, out var v) && !string.IsNullOrWhiteSpace(v) ? v.Trim() : null;

        if (Get("TargetFramework") is string tf) p.TargetFrameworks.Add(tf);
        else if (Get("TargetFrameworks") is string tfs) p.TargetFrameworks.AddRange(tfs.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        else if (Get("TargetFrameworkVersion") is string tfv) p.TargetFrameworks.Add("net" + tfv.TrimStart('v', 'V').Replace(".", ""));
        p.TargetFrameworks.RemoveAll(t => t.Contains("$("));

        p.LangVersion = Get("LangVersion");
        p.NullableEnabled = string.Equals(Get("Nullable"), "enable", StringComparison.OrdinalIgnoreCase);
        p.ImplicitUsings = Get("ImplicitUsings") is string iu && (iu.Equals("enable", StringComparison.OrdinalIgnoreCase) || iu.Equals("true", StringComparison.OrdinalIgnoreCase));
        p.UseWindowsForms = string.Equals(Get("UseWindowsForms"), "true", StringComparison.OrdinalIgnoreCase);
        p.UseWpf = string.Equals(Get("UseWPF"), "true", StringComparison.OrdinalIgnoreCase);
        p.OutputType = Get("OutputType") ?? (p.Sdk.Contains("Web", StringComparison.OrdinalIgnoreCase) || p.Sdk.Contains("Worker", StringComparison.OrdinalIgnoreCase) ? "Exe" : "Library");
        p.AssemblyName = Get("AssemblyName");
        p.Defines = defines.Where(d => d.Length > 0 && !d.Contains("$(")).Distinct(StringComparer.Ordinal).ToList();

        foreach (var item in proj.Descendants().Where(e => e.Parent?.Name.LocalName == "ItemGroup"))
        {
            string ln = item.Name.LocalName;
            string? include = (string?)item.Attribute("Include");
            if (ln == "PackageReference")
            {
                string? id = include ?? (string?)item.Attribute("Update");
                if (id == null) continue;
                string? ver = (string?)item.Attribute("Version") ?? item.Elements().FirstOrDefault(e => e.Name.LocalName == "Version")?.Value;
                p.Packages.Add(new PackageRef { Id = id, Version = ver });
            }
            else if (ln == "Reference" && include != null)
            {
                p.Packages.Add(new PackageRef { Id = include.Split(',')[0].Trim(), Version = "assembly" });
            }
            else if (ln == "ProjectReference" && include != null)
            {
                p.ProjectRefs.Add(Path.GetFullPath(Path.Combine(p.Dir, include.Replace('\\', '/'))));
            }
            else if (ln == "Using")
            {
                string? name = include ?? (string?)item.Attribute("Remove");
                if (name == null) continue;
                p.Usings.Add((name, (string?)item.Attribute("Alias"),
                    string.Equals((string?)item.Attribute("Static"), "true", StringComparison.OrdinalIgnoreCase),
                    include == null));
            }
            else if (ln == "Compile" && include != null && p.OldStyle)
            {
                foreach (var part in include.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
                {
                    if (part.Contains('*') || part.Contains("$(")) { p.ExplicitCompile.Clear(); p.OldStyle = false; break; }
                    p.ExplicitCompile.Add(Path.GetFullPath(Path.Combine(p.Dir, part.Replace('\\', '/'))));
                }
                if (!p.OldStyle) break;
            }
        }

        // packages.config (old-style NuGet)
        string pkgConfig = Path.Combine(p.Dir, "packages.config");
        if (File.Exists(pkgConfig))
        {
            try
            {
                foreach (var e in XDocument.Load(pkgConfig).Descendants().Where(e => e.Name.LocalName == "package"))
                    if ((string?)e.Attribute("id") is string id) p.Packages.Add(new PackageRef { Id = id, Version = (string?)e.Attribute("version") });
            }
            catch { }
        }
        return p;
    }

    private static bool IsAncestorDir(string ancestor, string dir)
    {
        string a = Norm(ancestor) + "/";
        string d = Norm(dir) + "/";
        return d.StartsWith(a, StringComparison.Ordinal);
    }

    private static void ReadProps(XElement proj, Dictionary<string, string> props, List<string> defines)
    {
        foreach (var pg in proj.Elements().Where(e => e.Name.LocalName == "PropertyGroup"))
        {
            string pgCond = (string?)pg.Attribute("Condition") ?? "";
            if (pgCond.Contains("Release", StringComparison.OrdinalIgnoreCase)) continue;
            foreach (var e in pg.Elements())
            {
                string cond = (string?)e.Attribute("Condition") ?? "";
                if (cond.Contains("Release", StringComparison.OrdinalIgnoreCase)) continue;
                string name = e.Name.LocalName;
                string val = e.Value.Trim();
                if (name == "DefineConstants")
                {
                    defines.AddRange(val.Replace("$(DefineConstants)", "").Split(new[] { ';', ',', ' ' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
                    continue;
                }
                // Conditioned TFM entries (e.g. per-OS) must not override an unconditioned one.
                if (cond.Length > 0 && props.ContainsKey(name)) continue;
                props[name] = SubstituteProps(val, props);
            }
        }
    }

    private static string SubstituteProps(string val, Dictionary<string, string> props)
    {
        if (!val.Contains("$(")) return val;
        return Regex.Replace(val, @"\$\(([A-Za-z0-9_]+)\)", m => props.TryGetValue(m.Groups[1].Value, out var v) ? v : m.Value);
    }

    private static void ResolveFramework(ProjectInfo p, Result scan, string? tfmOverride)
    {
        if (tfmOverride != null)
        {
            p.Tfm = tfmOverride;
        }
        else if (p.TargetFrameworks.Count > 0)
        {
            p.Tfm = PickTfm(p.TargetFrameworks);
        }
        else
        {
            // No csproj (or no TFM): infer from API usage.
            p.TfmInferred = true;
            bool winforms = false, wpf = scan.HasXaml;
            int sampled = 0;
            foreach (var f in p.Files)
            {
                if (sampled++ > 400) break;
                try
                {
                    string head = ReadHead(f, 16 * 1024);
                    if (head.Contains("System.Windows.Forms", StringComparison.Ordinal)) winforms = true;
                    if (head.Contains("System.Windows.Controls", StringComparison.Ordinal) || head.Contains("System.Windows.Markup", StringComparison.Ordinal)) wpf = true;
                }
                catch { }
            }
            p.UseWindowsForms |= winforms;
            p.UseWpf |= wpf;
            p.Tfm = (winforms || wpf) ? "net8.0-windows" : "net8.0";
            if (p.ProjectPath == null && p.Files.Count > 0)
            {
                // Modern loose snippets (top-level statements, no 'using System;') rely on implicit usings.
                int withUsingSystem = 0, topLevel = 0;
                foreach (var f in p.Files.Take(400))
                {
                    try
                    {
                        string head = ReadHead(f, 8 * 1024);
                        if (Regex.IsMatch(head, @"^\s*using\s+System\s*;", RegexOptions.Multiline)) withUsingSystem++;
                        if (!Regex.IsMatch(head, @"^\s*(namespace|(public|internal|static|sealed|abstract|partial)\s|class\s)", RegexOptions.Multiline) &&
                            Regex.IsMatch(head, @"^\s*(Console\.|var\s|await\s)", RegexOptions.Multiline)) topLevel++;
                    }
                    catch { }
                }
                p.ImplicitUsings = topLevel > 0 || withUsingSystem * 2 < Math.Min(p.Files.Count, 400);
                p.OutputType = topLevel > 0 ? "Exe" : "Library";
            }
        }
        p.RefPack = References.PackFor(p.Tfm, p.UseWindowsForms || p.UseWpf);
    }

    private static string ReadHead(string path, int bytes)
    {
        using var fs = File.OpenRead(path);
        var buf = new byte[Math.Min(bytes, (int)Math.Min(fs.Length, int.MaxValue))];
        int n = fs.Read(buf, 0, buf.Length);
        return System.Text.Encoding.UTF8.GetString(buf, 0, n);
    }

    /// <summary>Prefers the newest .NET (5+) TFM, then .NET Framework, then .NET Standard.</summary>
    public static string PickTfm(List<string> tfms)
    {
        (int rank, double ver) Score(string t)
        {
            var m = Regex.Match(t, @"^net(\d+)\.(\d+)", RegexOptions.IgnoreCase);
            if (m.Success) return (3, double.Parse(m.Groups[1].Value) + double.Parse(m.Groups[2].Value) / 10.0);
            m = Regex.Match(t, @"^netcoreapp(\d+)\.(\d+)", RegexOptions.IgnoreCase);
            if (m.Success) return (2, double.Parse(m.Groups[1].Value) + double.Parse(m.Groups[2].Value) / 10.0);
            m = Regex.Match(t, @"^net(\d)(\d)(\d?)$", RegexOptions.IgnoreCase);
            if (m.Success) return (1, double.Parse(m.Groups[1].Value + "." + m.Groups[2].Value + m.Groups[3].Value));
            m = Regex.Match(t, @"^netstandard(\d+)\.(\d+)", RegexOptions.IgnoreCase);
            if (m.Success) return (0, double.Parse(m.Groups[1].Value) + double.Parse(m.Groups[2].Value) / 10.0);
            return (-1, 0);
        }
        return tfms.OrderByDescending(t => Score(t).rank).ThenByDescending(t => Score(t).ver).First();
    }
}
