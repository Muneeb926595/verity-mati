using System.Collections.Concurrent;
using System.Text;
using System.Xml.Linq;
using Microsoft.CodeAnalysis;

namespace Verity.Roslyn;

/// <summary>
/// Stable symbol keys: Roslyn DocumentationCommentId for declared symbols;
/// <c>&lt;fn&gt;#param:name</c>, <c>&lt;fn&gt;#local:name@line</c>, <c>&lt;fn&gt;#lambda@line:col</c>, <c>&lt;fn&gt;#fn:name</c> otherwise.
/// </summary>
internal sealed class KeyCache
{
    private readonly ConcurrentDictionary<ISymbol, string> _cache = new(SymbolEqualityComparer.Default);

    public string? Of(ISymbol? s)
    {
        if (s == null) return null;
        if (_cache.TryGetValue(s, out var k)) return k;
        k = Compute(s, 0);
        if (k != null) _cache.TryAdd(s, k);
        return k;
    }

    private string? OfDepth(ISymbol? s, int depth)
    {
        if (s == null) return null;
        if (depth > 32) return "?:" + SafeDisplay(s);
        if (_cache.TryGetValue(s, out var k)) return k;
        k = Compute(s, depth);
        if (k != null) _cache.TryAdd(s, k);
        return k;
    }

    private string? Compute(ISymbol s, int depth)
    {
        // Anonymous types have no stable, unique name ("P:.Name"); they are not cross-reference targets.
        if (s is INamedTypeSymbol { IsAnonymousType: true } || s.ContainingType is { IsAnonymousType: true }) return null;
        switch (s)
        {
            case IAliasSymbol a:
                return OfDepth(a.Target, depth + 1);
            case IParameterSymbol p:
            {
                // Indexer accessor parameters are the indexer's parameters (only the setter's 'value' is its own).
                if (p.ContainingSymbol is IMethodSymbol { AssociatedSymbol: IPropertySymbol { IsIndexer: true } ix } && p.Ordinal < ix.Parameters.Length)
                {
                    var ik = OfDepth(ix, depth + 1);
                    return ik == null ? null : ik + "#param:" + p.Name;
                }
                var c = OfDepth(p.ContainingSymbol, depth + 1);
                return c == null ? null : c + "#param:" + p.Name;
            }
            case ILocalSymbol l:
            {
                var c = OfDepth(l.ContainingSymbol, depth + 1);
                return c == null ? null : c + "#local:" + l.Name + "@" + Line(l);
            }
            case IRangeVariableSymbol r:
            {
                var c = OfDepth(r.ContainingSymbol, depth + 1);
                return c == null ? null : c + "#range:" + r.Name + "@" + Line(r);
            }
            case IMethodSymbol m when m.MethodKind == MethodKind.AnonymousFunction:
            {
                var c = OfDepth(m.ContainingSymbol, depth + 1) ?? "?";
                var syn = m.DeclaringSyntaxReferences.FirstOrDefault()?.GetSyntax();
                var pos = syn != null ? LineCol(syn) : LineCol(m);
                return $"{c}#lambda@{pos.line}:{pos.col}";
            }
            case IMethodSymbol m when m.MethodKind == MethodKind.LocalFunction:
            {
                var c = OfDepth(m.ContainingSymbol, depth + 1) ?? "?";
                return $"{c}#fn:{m.Name}";
            }
            case IMethodSymbol m when m.ReducedFrom != null:
                return OfDepth(m.ReducedFrom, depth + 1);
        }
        var def = s.OriginalDefinition;
        if (def != null && !SymbolEqualityComparer.Default.Equals(def, s)) return OfDepth(def, depth + 1);
        if (s is IMethodSymbol cm && cm.ConstructedFrom != null && !SymbolEqualityComparer.Default.Equals(cm.ConstructedFrom, s))
            return OfDepth(cm.ConstructedFrom, depth + 1);
        try
        {
            var id = DocumentationCommentId.CreateDeclarationId(s);
            if (!string.IsNullOrEmpty(id))
            {
                // Recent Roslyn appends "~ReturnType" to every method id. The Verity contract (and the ECMA
                // doc-id format) only keeps it for conversion operators, where it is needed for uniqueness.
                if (s is IMethodSymbol { MethodKind: not MethodKind.Conversion } && id.StartsWith("M:", StringComparison.Ordinal))
                {
                    int tilde = id.IndexOf('~');
                    if (tilde > 0) id = id[..tilde];
                }
                return id;
            }
        }
        catch { }
        return "?:" + SafeDisplay(s);
    }

    private static string SafeDisplay(ISymbol s)
    {
        try { return s.ToDisplayString(Fmt.Qual); } catch { return s.Name; }
    }

    public static int Line(ISymbol s)
    {
        var loc = s.Locations.FirstOrDefault();
        return loc == null || !loc.IsInSource ? 0 : loc.GetLineSpan().StartLinePosition.Line + 1;
    }

    public static (int line, int col) LineCol(ISymbol s)
    {
        var loc = s.Locations.FirstOrDefault();
        if (loc == null || !loc.IsInSource) return (0, 0);
        var p = loc.GetLineSpan().StartLinePosition;
        return (p.Line + 1, p.Character + 1);
    }

    public static (int line, int col) LineCol(SyntaxNode n)
    {
        var p = n.GetLocation().GetLineSpan().StartLinePosition;
        return (p.Line + 1, p.Character + 1);
    }
}

internal static class Fmt
{
    public static readonly SymbolDisplayFormat Qual = new(
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypesAndNamespaces,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters,
        memberOptions: SymbolDisplayMemberOptions.IncludeContainingType | SymbolDisplayMemberOptions.IncludeExplicitInterface,
        miscellaneousOptions: SymbolDisplayMiscellaneousOptions.UseSpecialTypes | SymbolDisplayMiscellaneousOptions.EscapeKeywordIdentifiers);

    public static readonly SymbolDisplayFormat Sig = new(
        globalNamespaceStyle: SymbolDisplayGlobalNamespaceStyle.Omitted,
        typeQualificationStyle: SymbolDisplayTypeQualificationStyle.NameAndContainingTypes,
        genericsOptions: SymbolDisplayGenericsOptions.IncludeTypeParameters | SymbolDisplayGenericsOptions.IncludeVariance,
        memberOptions: SymbolDisplayMemberOptions.IncludeParameters | SymbolDisplayMemberOptions.IncludeType |
                       SymbolDisplayMemberOptions.IncludeRef | SymbolDisplayMemberOptions.IncludeModifiers |
                       SymbolDisplayMemberOptions.IncludeExplicitInterface | SymbolDisplayMemberOptions.IncludeConstantValue,
        delegateStyle: SymbolDisplayDelegateStyle.NameAndSignature,
        extensionMethodStyle: SymbolDisplayExtensionMethodStyle.StaticMethod,
        parameterOptions: SymbolDisplayParameterOptions.IncludeType | SymbolDisplayParameterOptions.IncludeName |
                          SymbolDisplayParameterOptions.IncludeParamsRefOut | SymbolDisplayParameterOptions.IncludeDefaultValue |
                          SymbolDisplayParameterOptions.IncludeExtensionThis,
        propertyStyle: SymbolDisplayPropertyStyle.ShowReadWriteDescriptor,
        kindOptions: SymbolDisplayKindOptions.IncludeTypeKeyword,
        miscellaneousOptions: SymbolDisplayMiscellaneousOptions.UseSpecialTypes | SymbolDisplayMiscellaneousOptions.EscapeKeywordIdentifiers |
                              SymbolDisplayMiscellaneousOptions.IncludeNullableReferenceTypeModifier);

    public static readonly SymbolDisplayFormat Type = SymbolDisplayFormat.CSharpErrorMessageFormat;

    public static string Display(ISymbol? s, SymbolDisplayFormat f)
    {
        if (s == null) return "";
        try { return s.ToDisplayString(f); } catch { return s.Name; }
    }

    public static string? TypeOf(ISymbol s) => s switch
    {
        IMethodSymbol m => m.MethodKind == MethodKind.Constructor ? null : Display(m.ReturnType, Type),
        IFieldSymbol f => Display(f.Type, Type),
        IPropertySymbol p => Display(p.Type, Type),
        IEventSymbol e => Display(e.Type, Type),
        IParameterSymbol p => Display(p.Type, Type),
        ILocalSymbol l => Display(l.Type, Type),
        INamedTypeSymbol t when t.TypeKind == TypeKind.Enum => Display(t.EnumUnderlyingType, Type),
        INamedTypeSymbol t when t.TypeKind == TypeKind.Delegate => Display(t.DelegateInvokeMethod?.ReturnType, Type),
        INamedTypeSymbol t => t.BaseType != null && t.BaseType.SpecialType != SpecialType.System_Object && t.TypeKind == TypeKind.Class ? Display(t.BaseType, Type) : null,
        _ => null,
    };

    public static string Access(ISymbol s) => s.DeclaredAccessibility switch
    {
        Accessibility.Public => "public",
        Accessibility.Private => "private",
        Accessibility.Protected => "protected",
        Accessibility.Internal => "internal",
        Accessibility.ProtectedOrInternal => "protected internal",
        Accessibility.ProtectedAndInternal => "private protected",
        _ => "",
    };
}

internal static class Docs
{
    /// <summary>Extracts the &lt;summary&gt; (or &lt;param&gt; for parameters) text from XML documentation.</summary>
    public static string? Summary(ISymbol s, CancellationToken ct)
    {
        try
        {
            if (s is IParameterSymbol p)
            {
                if (p.ContainingSymbol == null) return null;
                var xml = p.ContainingSymbol.GetDocumentationCommentXml(null, false, ct);
                if (string.IsNullOrWhiteSpace(xml)) return null;
                var root = XElement.Parse(xml);
                var el = root.Elements("param").FirstOrDefault(e => (string?)e.Attribute("name") == p.Name);
                return el == null ? null : Render(el);
            }
            if (s is ILocalSymbol) return null;
            var x = s.GetDocumentationCommentXml(null, false, ct);
            if (string.IsNullOrWhiteSpace(x)) return null;
            var r = XElement.Parse(x);
            var sum = r.Element("summary");
            string? text = sum != null ? Render(sum) : null;
            if (string.IsNullOrEmpty(text))
            {
                var inh = r.Element("inheritdoc");
                if (inh != null) return "(inheritdoc)";
                text = Render(r);
            }
            return string.IsNullOrWhiteSpace(text) ? null : text;
        }
        catch (OperationCanceledException) { throw; }
        catch { return null; }
    }

    private static string Render(XElement el)
    {
        var sb = new StringBuilder();
        void Walk(XNode n)
        {
            switch (n)
            {
                case XText t: sb.Append(t.Value); break;
                case XElement e:
                    if (e.Name.LocalName is "see" or "seealso")
                    {
                        string? cref = (string?)e.Attribute("cref") ?? (string?)e.Attribute("langword") ?? (string?)e.Attribute("href");
                        if (!e.Nodes().Any() && cref != null)
                        {
                            string c = cref.Length > 2 && cref[1] == ':' ? cref[2..] : cref;
                            int paren = c.IndexOf('(');
                            if (paren >= 0) c = c[..paren];
                            sb.Append(c.Split('.').Last());
                            break;
                        }
                    }
                    else if (e.Name.LocalName is "paramref" or "typeparamref")
                    {
                        sb.Append((string?)e.Attribute("name"));
                        break;
                    }
                    foreach (var c2 in e.Nodes()) Walk(c2);
                    if (e.Name.LocalName is "para" or "br") sb.Append(' ');
                    break;
            }
        }
        foreach (var n in el.Nodes()) Walk(n);
        return J.Snip(sb.ToString(), 1000);
    }
}
