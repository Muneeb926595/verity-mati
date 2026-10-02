using System.Collections.Concurrent;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

namespace Verity.Roslyn;

/// <summary>Maps target frameworks to the bundled Basic.Reference.Assemblies packs (no restore, fully offline).</summary>
internal static class References
{
    private static readonly ConcurrentDictionary<string, IReadOnlyList<MetadataReference>> Cache = new();

    public static string PackFor(string tfm, bool windowsDesktop)
    {
        string t = tfm.ToLowerInvariant();
        bool win = windowsDesktop || t.Contains("-windows");
        var m = Regex.Match(t, @"^net(\d+)\.\d+");
        if (m.Success && int.TryParse(m.Groups[1].Value, out int major) && major >= 5)
        {
            string core = major >= 9 ? "Net100" : "Net80";
            return win ? core + "+Net80Windows" : core;
        }
        if (t.StartsWith("netcoreapp")) return win ? "Net80+Net80Windows" : "Net80";
        if (Regex.IsMatch(t, @"^net[1-4]\d*$") || t.StartsWith("v4") || t.StartsWith("net4")) return "Net472";
        if (t.StartsWith("netstandard")) return "NetStandard20";
        return win ? "Net80+Net80Windows" : "Net80";
    }

    public static IReadOnlyList<MetadataReference> For(string pack)
    {
        return Cache.GetOrAdd(pack, p =>
        {
            var list = new List<MetadataReference>();
            foreach (var part in p.Split('+'))
            {
                IEnumerable<PortableExecutableReference> refs = part switch
                {
                    "Net80" => Basic.Reference.Assemblies.Net80.References.All,
                    "Net80Windows" => Basic.Reference.Assemblies.Net80Windows.References.All,
                    "Net100" => Basic.Reference.Assemblies.Net100.References.All,
                    "Net472" => Basic.Reference.Assemblies.Net472.References.All,
                    "NetStandard20" => Basic.Reference.Assemblies.NetStandard20.References.All,
                    _ => Basic.Reference.Assemblies.Net80.References.All,
                };
                list.AddRange(refs);
            }
            // The desktop pack is built against 8.0 System.Runtime; drop duplicate simple names, keep the first (core) one.
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var dedup = new List<MetadataReference>();
            foreach (var r in list)
            {
                string name = Path.GetFileName((r as PortableExecutableReference)?.FilePath ?? r.Display ?? "");
                if (name.Length == 0 || seen.Add(name)) dedup.Add(r);
            }
            return dedup;
        });
    }

    /// <summary>Implicit preprocessor symbols the SDK defines for a TFM.</summary>
    public static IEnumerable<string> TfmSymbols(string tfm)
    {
        string t = tfm.ToLowerInvariant();
        var m = Regex.Match(t, @"^net(\d+)\.(\d+)");
        if (m.Success && int.Parse(m.Groups[1].Value) >= 5)
        {
            int major = int.Parse(m.Groups[1].Value), minor = int.Parse(m.Groups[2].Value);
            yield return "NET";
            yield return "NETCOREAPP";
            yield return $"NET{major}_{minor}";
            for (int v = 5; v <= major; v++) yield return $"NET{v}_0_OR_GREATER";
            foreach (var s in new[] { "NETCOREAPP1_0", "NETCOREAPP1_1", "NETCOREAPP2_0", "NETCOREAPP2_1", "NETCOREAPP2_2", "NETCOREAPP3_0", "NETCOREAPP3_1" })
                yield return s + "_OR_GREATER";
            if (t.Contains("-windows")) { yield return "WINDOWS"; yield return "WINDOWS7_0_OR_GREATER"; }
            yield break;
        }
        m = Regex.Match(t, @"^netcoreapp(\d+)\.(\d+)");
        if (m.Success)
        {
            yield return "NETCOREAPP";
            yield return $"NETCOREAPP{m.Groups[1].Value}_{m.Groups[2].Value}";
            yield break;
        }
        m = Regex.Match(t, @"^net(\d)(\d)(\d?)$");
        if (m.Success)
        {
            yield return "NETFRAMEWORK";
            string ver = m.Groups[1].Value + m.Groups[2].Value + m.Groups[3].Value;
            yield return "NET" + ver;
            int me = int.Parse(ver.PadRight(3, '0'));
            foreach (var v in new[] { "20", "30", "35", "40", "45", "451", "452", "46", "461", "462", "47", "471", "472", "48", "481" })
                if (int.Parse(v.PadRight(3, '0')) <= me) yield return $"NET{v}_OR_GREATER";
            yield break;
        }
        m = Regex.Match(t, @"^netstandard(\d+)\.(\d+)");
        if (m.Success)
        {
            yield return "NETSTANDARD";
            yield return $"NETSTANDARD{m.Groups[1].Value}_{m.Groups[2].Value}";
            yield return "NETSTANDARD2_0_OR_GREATER";
            yield return "NETSTANDARD1_6_OR_GREATER";
        }
    }

    /// <summary>The global usings the .NET SDK generates for ImplicitUsings=enable.</summary>
    public static string? ImplicitUsingsSource(ProjectInfo p, LanguageVersion lang)
    {
        if (lang.MapSpecifiedToEffectiveVersion() < LanguageVersion.CSharp10) return null;
        var ns = new List<string>();
        if (p.ImplicitUsings)
        {
            ns.AddRange(new[] { "System", "System.Collections.Generic", "System.IO", "System.Linq", "System.Net.Http", "System.Threading", "System.Threading.Tasks" });
            if (p.Sdk.Contains("Web", StringComparison.OrdinalIgnoreCase))
                ns.AddRange(new[] { "System.Net.Http.Json", "Microsoft.AspNetCore.Builder", "Microsoft.AspNetCore.Hosting", "Microsoft.AspNetCore.Http", "Microsoft.AspNetCore.Routing", "Microsoft.Extensions.Configuration", "Microsoft.Extensions.DependencyInjection", "Microsoft.Extensions.Hosting", "Microsoft.Extensions.Logging" });
            else if (p.Sdk.Contains("Worker", StringComparison.OrdinalIgnoreCase))
                ns.AddRange(new[] { "Microsoft.Extensions.Configuration", "Microsoft.Extensions.DependencyInjection", "Microsoft.Extensions.Hosting", "Microsoft.Extensions.Logging" });
            if (p.UseWindowsForms) ns.AddRange(new[] { "System.Drawing", "System.Windows.Forms" });
        }
        var sb = new StringBuilder();
        var removed = new HashSet<string>(p.Usings.Where(u => u.Remove).Select(u => u.Name), StringComparer.Ordinal);
        foreach (var n in ns.Distinct())
            if (!removed.Contains(n)) sb.Append("global using global::").Append(n).Append(";\n");
        foreach (var u in p.Usings.Where(u => !u.Remove))
        {
            if (u.Alias != null) sb.Append("global using ").Append(u.Alias).Append(" = global::").Append(u.Name).Append(";\n");
            else if (u.Static) sb.Append("global using static global::").Append(u.Name).Append(";\n");
            else sb.Append("global using global::").Append(u.Name).Append(";\n");
        }
        return sb.Length == 0 ? null : sb.ToString();
    }

    public static LanguageVersion ParseLang(string? v)
    {
        if (v != null && LanguageVersionFacts.TryParse(v, out var lv)) return lv;
        return LanguageVersion.Latest;
    }
}
