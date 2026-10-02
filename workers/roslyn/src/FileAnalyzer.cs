using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Operations;

namespace Verity.Roslyn;

/// <summary>
/// Extracts all facts for one syntax tree. The declaration walk (syntax) emits symbols, inheritance,
/// branches, type references, entries and config keys; every executable body is additionally walked
/// through the IOperation tree (see FileAnalyzer.Ops.cs) for occurrences, calls, flows and wiring.
/// </summary>
internal sealed partial class FileAnalyzer
{
    private readonly ProjectContext P;
    private readonly FileMeta Meta;
    private readonly SemanticModel M;
    private readonly RecordBuffer B;
    private readonly FileResult R;
    private readonly CancellationToken Ct;
    private readonly GuardComputer G;
    private readonly KeyCache K;
    private readonly string FilePath;
    private readonly bool IsDesigner;
    private readonly HashSet<string> _entryDedupe = new(StringComparer.Ordinal);
    private int _opCount;
    private int _symbols;

    public FileAnalyzer(ProjectContext p, FileMeta meta, SemanticModel model, RecordBuffer body, FileResult result, CancellationToken ct)
    {
        P = p;
        Meta = meta;
        M = model;
        B = body;
        R = result;
        Ct = ct;
        K = p.Keys;
        G = new GuardComputer(model, p.Keys, ct);
        FilePath = meta.Rel;
        IsDesigner = meta.Rel.EndsWith(".Designer.cs", StringComparison.OrdinalIgnoreCase);
    }

    public int Symbols => _symbols;

    /// <summary>Runs the analysis; returns the serialized file record (emitted first by the caller).</summary>
    public byte[] Run()
    {
        string fidelity = "F2";
        int errors = 0;
        var stubs = new SortedSet<string>(StringComparer.Ordinal);
        var errorDiags = new List<Diagnostic>();
        bool semanticOk = true;
        try
        {
            foreach (var d in M.GetDiagnostics(null, Ct))
            {
                if (d.Severity != DiagnosticSeverity.Error) continue;
                errors++;
                if (errorDiags.Count < 200) errorDiags.Add(d);
                if ((d.Id == "CS0246" || d.Id == "CS0234" || d.Id == "CS0103") && stubs.Count < 50)
                {
                    var m = Regex.Match(d.GetMessage(), "'([^']+)'");
                    if (m.Success) stubs.Add(m.Groups[1].Value);
                }
            }
            if (errors > 0) fidelity = "F3";
        }
        catch (OperationCanceledException) { fidelity = "F4"; semanticOk = false; }
        catch (Exception ex) { fidelity = "F3"; Internal("diagnostics", ex); }

        bool timedOut = !semanticOk;
        if (semanticOk)
        {
            try
            {
                new DeclWalker(this).Visit(Meta.Tree.GetRoot(Ct));
            }
            catch (OperationCanceledException) { timedOut = true; fidelity = "F4"; }
            catch (InsufficientExecutionStackException ex) { Internal("walk (nesting too deep)", ex); }
            catch (Exception ex) { Internal("walk", ex); }
        }
        if (timedOut)
        {
            R.ExtractorErrors++;
            R.Diags.Add(RecordBuffer.Line("diag", w =>
            {
                w.WriteString("file", FilePath);
                w.WriteNumber("line", 1);
                w.WriteString("severity", "warning");
                w.WriteString("code", "VR002");
                w.WriteString("msg", "analysis timed out; facts for this file are partial");
            }));
        }
        if (B.EmitErrors > 0) Internal("emit", new Exception($"{B.EmitErrors} record(s) dropped: {B.FirstEmitError}"));

        foreach (var d in errorDiags)
        {
            var ls = d.Location.GetLineSpan();
            R.Diags.Add(RecordBuffer.Line("diag", w =>
            {
                w.WriteString("file", FilePath);
                w.WriteNumber("line", ls.StartLinePosition.Line + 1);
                w.WriteString("severity", "error");
                w.WriteString("code", d.Id);
                w.Str("msg", J.Snip(d.Id + ": " + d.GetMessage(), 400));
            }));
        }
        R.CompileErrors = errors;

        return RecordBuffer.Line("file", w =>
        {
            w.WriteString("path", FilePath);
            w.WriteString("lang", "csharp");
            w.WriteString("sha", Meta.Sha);
            w.WriteNumber("loc", Meta.Loc);
            w.WriteString("fidelity", fidelity);
            w.WriteNumber("errors", errors);
            w.Arr("stubs", stubs);
            w.WriteString("profile", P.Profile);
            w.Str("project", P.Info.ProjectPath);
        });
    }

    private void Internal(string where, Exception ex)
    {
        R.ExtractorErrors++;
        R.Diags.Add(RecordBuffer.Line("diag", w =>
        {
            w.WriteString("file", FilePath);
            w.WriteNumber("line", 1);
            w.WriteString("severity", "internal");
            w.WriteString("code", "VR001");
            w.Str("msg", J.Snip($"extractor error in {where}: {ex.GetType().Name}: {ex.Message}", 400));
        }));
    }

    // ───────────────────────────── helpers ─────────────────────────────

    private static (int line, int col) Pos(SyntaxNode n)
    {
        var p = n.GetLocation().GetLineSpan().StartLinePosition;
        return (p.Line + 1, p.Character + 1);
    }

    private static (int line, int col) Pos(SyntaxToken t)
    {
        var p = t.GetLocation().GetLineSpan().StartLinePosition;
        return (p.Line + 1, p.Character + 1);
    }

    private static (int line, int col) Pos(Location l)
    {
        var p = l.GetLineSpan().StartLinePosition;
        return (p.Line + 1, p.Character + 1);
    }

    private static (int, int) Range(SyntaxNode n)
    {
        var ls = n.GetLocation().GetLineSpan();
        return (ls.StartLinePosition.Line + 1, ls.EndLinePosition.Line + 1);
    }

    private static int EndLine(SyntaxNode n) => n.GetLocation().GetLineSpan().EndLinePosition.Line + 1;

    private static void WriteGuards(Utf8JsonWriter w, List<Guard>? guards)
    {
        w.WriteStartArray("guards");
        if (guards != null) foreach (var g in guards) g.Write(w);
        w.WriteEndArray();
    }

    // ───────────────────────────── symbol emission ─────────────────────────────

    private void EmitSymbol(ISymbol s, string? key, string kind, string name, SyntaxNode decl, Location nameLoc, string? container, bool isDef,
                            Action<Utf8JsonWriter>? extra = null, string? qualOverride = null, int? endOverride = null)
    {
        if (key == null) return;
        var (line, col) = nameLoc.IsInSource ? Pos(nameLoc) : Pos(decl);
        int end = endOverride ?? EndLine(decl);
        string qual = qualOverride ?? Fmt.Display(s, Fmt.Qual);
        string sig = s switch
        {
            ILocalSymbol l => Fmt.Display(l.Type, Fmt.Type) + " " + l.Name,
            IParameterSymbol p => Fmt.Display(p, Fmt.Sig),
            _ => Fmt.Display(s, Fmt.Sig),
        };
        string? type = Fmt.TypeOf(s);
        string? doc = s is ILocalSymbol ? null : Docs.Summary(s, Ct);
        bool isStatic = s is not (ILocalSymbol or IParameterSymbol or INamespaceSymbol) && s.IsStatic;
        bool isExtern = s.IsExtern;
        B.Record("symbol", w =>
        {
            w.WriteString("key", key);
            w.WriteString("kind", kind);
            w.Str("name", name);
            w.Str("qual", qual);
            w.Str("sig", J.Snip(sig, 400));
            w.Str("type", type);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.WriteNumber("end_line", end);
            w.WriteNumber("col", col);
            w.Str("container", container);
            w.WriteBoolean("is_def", isDef);
            w.WriteBoolean("static", isStatic);
            w.WriteBoolean("extern", isExtern);
            w.Str("doc", doc);
            if (s is not (ILocalSymbol or IParameterSymbol or INamespaceSymbol))
            {
                w.WriteString("access", Fmt.Access(s));
                if (s.IsAbstract) w.WriteBoolean("abstract", true);
                if (s.IsVirtual) w.WriteBoolean("virtual", true);
                if (s.IsOverride) w.WriteBoolean("override", true);
                if (s.IsSealed && s is not INamedTypeSymbol { TypeKind: TypeKind.Struct or TypeKind.Enum }) w.WriteBoolean("sealed", true);
                if (s.IsImplicitlyDeclared) w.WriteBoolean("implicit", true);
                if (s is IMethodSymbol { IsAsync: true }) w.WriteBoolean("async", true);
            }
            extra?.Invoke(w);
        });
        _symbols++;
    }

    /// <summary>Readable qualified name, including synthesized/anonymous containers.</summary>
    private string QualOf(ISymbol? s, int depth = 0)
    {
        if (s == null || depth > 16) return "";
        return s switch
        {
            IMethodSymbol { MethodKind: MethodKind.AnonymousFunction } m => QualOf(m.ContainingSymbol, depth + 1) + ".lambda@" +
                (m.DeclaringSyntaxReferences.FirstOrDefault()?.GetSyntax() is SyntaxNode n ? KeyCache.LineCol(n).line : KeyCache.Line(m)),
            IMethodSymbol { MethodKind: MethodKind.LocalFunction } m => QualOf(m.ContainingSymbol, depth + 1) + "." + m.Name,
            IMethodSymbol m when m.ContainingType is { IsImplicitlyDeclared: true } ct => ct.Name + "." + m.Name,
            _ => Fmt.Display(s, Fmt.Qual),
        };
    }

    private string? ContainerOf(ISymbol s)
    {
        if (s.ContainingType != null) return K.Of(s.ContainingType);
        var ns = s.ContainingNamespace;
        if (ns == null || ns.IsGlobalNamespace) return null;
        return K.Of(ns);
    }

    private void EmitParams(IMethodSymbol m, string? fnKey, SyntaxNode fallbackDecl)
    {
        if (fnKey == null) return;
        foreach (var p in m.Parameters) EmitParam(p, fallbackDecl);
    }

    private void EmitParam(IParameterSymbol p, SyntaxNode fallbackDecl)
    {
        var decl = p.DeclaringSyntaxReferences.FirstOrDefault()?.GetSyntax(Ct) ?? fallbackDecl;
        var loc = p.Locations.FirstOrDefault(l => l.IsInSource) ?? decl.GetLocation();
        if (loc.SourceTree != Meta.Tree) { loc = fallbackDecl.GetLocation(); decl = fallbackDecl; }
        string? fnKey = K.Of(p.ContainingSymbol);
        EmitSymbol(p, K.Of(p), "param", p.Name, decl, loc, fnKey, true,
            w => { w.WriteNumber("pos", p.Ordinal); if (p.RefKind != RefKind.None) w.WriteString("ref", p.RefKind.ToString().ToLowerInvariant()); },
            qualOverride: QualOf(p.ContainingSymbol) + "." + p.Name);
    }

    private void EmitLocal(ILocalSymbol l, SyntaxToken ident, SyntaxNode decl)
    {
        string? key = K.Of(l);
        EmitSymbol(l, key, "local", l.Name, decl, ident.GetLocation(), K.Of(l.ContainingSymbol), true,
            w => { if (l.IsConst) w.WriteBoolean("const", true); },
            qualOverride: QualOf(l.ContainingSymbol) + "." + l.Name);
        if (l.IsConst && l.HasConstantValue && key != null) EmitConst(key, l.ConstantValue);
    }

    private void EmitConst(string key, object? value)
    {
        string? v = value switch
        {
            null => null,
            string s => s,
            bool b => b ? "true" : "false",
            IFormattable f => f.ToString(null, System.Globalization.CultureInfo.InvariantCulture),
            _ => value.ToString(),
        };
        string vtype = value switch { null => "null", string => "string", bool => "bool", char => "char", _ => "number" };
        B.Record("const", w =>
        {
            w.WriteString("sym", key);
            w.Str("value", v == null ? null : J.Snip(v, 400));
            w.WriteString("vtype", vtype);
        });
    }

    private void Entry(string? sym, string kind, string? detail, string res)
    {
        if (sym == null) return;
        if (!_entryDedupe.Add(sym + "|" + kind + "|" + detail)) return;
        B.Record("entry", w =>
        {
            w.WriteString("sym", sym);
            w.WriteString("kind", kind);
            w.Str("detail", detail);
            w.WriteString("res", res);
            w.WriteString("file", FilePath);
        });
    }

    private void Inherit(string? sub, string? @base, string kind)
    {
        if (sub == null || @base == null) return;
        B.Record("inherit", w =>
        {
            w.WriteString("sub", sub);
            w.WriteString("base", @base);
            w.WriteString("kind", kind);
        });
    }

    private void EmitTypeInheritance(INamedTypeSymbol t)
    {
        string? key = K.Of(t);
        var bt = t.BaseType;
        if (bt != null && bt.SpecialType is not (SpecialType.System_Object or SpecialType.System_ValueType or SpecialType.System_Enum or SpecialType.System_MulticastDelegate)
            && t.TypeKind is TypeKind.Class or TypeKind.Struct)
            Inherit(key, K.Of(bt), "extends");
        foreach (var i in t.Interfaces)
            Inherit(key, K.Of(i), t.TypeKind == TypeKind.Interface ? "extends" : "implements");

        var done = new HashSet<(string, string)>();
        void Pair(string? a, string? b, string kind)
        {
            if (a == null || b == null || !done.Add((a, b))) return;
            Inherit(a, b, kind);
        }

        foreach (var m in t.GetMembers())
        {
            if (m.IsImplicitlyDeclared) continue;
            switch (m)
            {
                case IMethodSymbol ms when ms.OverriddenMethod != null:
                    Pair(K.Of(ms), K.Of(ms.OverriddenMethod), "overrides");
                    break;
                case IPropertySymbol ps when ps.OverriddenProperty != null:
                    Pair(K.Of(ps), K.Of(ps.OverriddenProperty), "overrides");
                    break;
                case IEventSymbol es when es.OverriddenEvent != null:
                    Pair(K.Of(es), K.Of(es.OverriddenEvent), "overrides");
                    break;
            }
        }

        if (t.TypeKind is TypeKind.Class or TypeKind.Struct)
        {
            var baseIfaces = t.BaseType?.AllInterfaces ?? System.Collections.Immutable.ImmutableArray<INamedTypeSymbol>.Empty;
            foreach (var iface in t.AllInterfaces)
            {
                bool viaBase = baseIfaces.Contains(iface, SymbolEqualityComparer.Default);
                foreach (var im in iface.GetMembers())
                {
                    if (im is not (IMethodSymbol or IPropertySymbol or IEventSymbol)) continue;
                    if (im is IMethodSymbol { MethodKind: not MethodKind.Ordinary }) continue; // accessors handled with their property
                    if (im.IsStatic && !im.IsAbstract && !im.IsVirtual) continue;
                    ISymbol? impl;
                    try { impl = t.FindImplementationForInterfaceMember(im); } catch { impl = null; }
                    if (impl == null || SymbolEqualityComparer.Default.Equals(impl.OriginalDefinition, im.OriginalDefinition)) continue; // default interface member
                    if (impl.IsImplicitlyDeclared && P.IsSource(impl)) continue; // compiler-synthesized (records): no symbol record
                    bool own = SymbolEqualityComparer.Default.Equals(impl.ContainingType, t);
                    if (!own && viaBase) continue; // the base type emits it
                    Pair(K.Of(impl), K.Of(im), "implements");
                    if (im is IPropertySymbol ip && impl is IPropertySymbol pp)
                    {
                        if (ip.GetMethod != null && pp.GetMethod != null) Pair(K.Of(pp.GetMethod), K.Of(ip.GetMethod), "implements");
                        if (ip.SetMethod != null && pp.SetMethod != null) Pair(K.Of(pp.SetMethod), K.Of(ip.SetMethod), "implements");
                    }
                }
            }
        }

        // Accessors of overriding properties override the base accessors.
        foreach (var ps in t.GetMembers().OfType<IPropertySymbol>())
        {
            var op = ps.OverriddenProperty;
            if (op == null) continue;
            if (ps.GetMethod != null && op.GetMethod != null) Pair(K.Of(ps.GetMethod), K.Of(op.GetMethod), "overrides");
            if (ps.SetMethod != null && op.SetMethod != null) Pair(K.Of(ps.SetMethod), K.Of(op.SetMethod), "overrides");
        }
    }

    private static string TypeKindName(INamedTypeSymbol t, SyntaxNode decl) => decl switch
    {
        RecordDeclarationSyntax => "record",
        EnumDeclarationSyntax => "enum",
        InterfaceDeclarationSyntax => "interface",
        StructDeclarationSyntax => "struct",
        DelegateDeclarationSyntax => "delegate",
        _ => t.TypeKind switch
        {
            TypeKind.Struct => "struct",
            TypeKind.Interface => "interface",
            TypeKind.Enum => "enum",
            TypeKind.Delegate => "delegate",
            _ => "class",
        },
    };

    private static IEnumerable<string> AttributeNames(SyntaxList<AttributeListSyntax> lists)
    {
        foreach (var l in lists)
            foreach (var a in l.Attributes)
            {
                string n = a.Name switch
                {
                    QualifiedNameSyntax q => q.Right.Identifier.ValueText,
                    AliasQualifiedNameSyntax aq => aq.Name.Identifier.ValueText,
                    SimpleNameSyntax s => s.Identifier.ValueText,
                    _ => a.Name.ToString(),
                };
                if (n.EndsWith("Attribute", StringComparison.Ordinal) && n.Length > 9) n = n[..^9];
                yield return n;
            }
    }

    private void MethodEntries(IMethodSymbol m, string? key, SyntaxList<AttributeListSyntax> attrs)
    {
        if (key == null) return;
        if (m.Name == "Main" && m.IsStatic && m.Parameters.Length <= 1)
        {
            bool exact = P.EntryPoint != null && SymbolEqualityComparer.Default.Equals(P.EntryPoint, m);
            Entry(key, "main", Fmt.Display(m, Fmt.Qual), exact ? "exact" : "pattern");
        }
        foreach (var a in AttributeNames(attrs))
        {
            if (ApiTables.TestAttributes.Contains(a)) Entry(key, "test", a, "pattern");
            else if (ApiTables.HandlerAttributes.Contains(a)) Entry(key, "handler", a, "pattern");
        }
    }

    private static bool HasBody(SyntaxNode decl) => decl switch
    {
        BaseMethodDeclarationSyntax b => b.Body != null || b.ExpressionBody != null,
        LocalFunctionStatementSyntax l => l.Body != null || l.ExpressionBody != null,
        _ => true,
    };

    private readonly HashSet<string> _emittedLambdas = new(StringComparer.Ordinal);

    /// <summary>Emits a lambda / anonymous method / query-clause lambda once, whichever walker sees it first.</summary>
    private void EmitLambda(IMethodSymbol m, SyntaxNode node, string? container)
    {
        string? key = K.Of(m);
        if (key == null || !_emittedLambdas.Add(key)) return;
        bool query = node is not AnonymousFunctionExpressionSyntax;
        EmitSymbol(m, key, "function", "lambda", node, node.GetLocation(), container ?? K.Of(m.ContainingSymbol), true,
            w => { w.WriteBoolean("lambda", true); if (query) w.WriteBoolean("query", true); },
            qualOverride: QualOf(m.ContainingSymbol) + ".lambda@" + Pos(node).line);
        EmitParams(m, key, node);
    }

    private void EmitMethodDecl(IMethodSymbol m, SyntaxNode decl, SyntaxToken ident, string kind)
    {
        // Partial definition parts: the implementation part emits the symbol.
        if (m.PartialImplementationPart != null && !HasBody(decl)) return;
        string? key = K.Of(m);
        string name = kind == "constructor" ? m.ContainingType?.Name ?? m.Name : m.Name;
        bool isDef = HasBody(decl) && !m.IsAbstract && !m.IsExtern;
        EmitSymbol(m, key, kind, name, decl, ident.GetLocation(), ContainerOf(m), isDef, w =>
        {
            if (m.IsExtensionMethod) w.WriteBoolean("extension", true);
            if (m.MethodKind is MethodKind.UserDefinedOperator or MethodKind.Conversion) w.WriteString("operator", m.Name);
            if (m.IsGenericMethod) w.WriteNumber("arity", m.Arity);
        });
        EmitParams(m, key, decl);
    }

    // ───────────────────────────── declaration walker ─────────────────────────────

    private sealed class DeclWalker : CSharpSyntaxWalker
    {
        private readonly FileAnalyzer F;
        private readonly Stack<string?> Fn = new();
        private int _nodes;

        public DeclWalker(FileAnalyzer f) : base(SyntaxWalkerDepth.Node) { F = f; }

        private string? CurFn => Fn.Count > 0 ? Fn.Peek() : null;

        public override void Visit(SyntaxNode? node)
        {
            if (node == null) return;
            if ((++_nodes & 511) == 0) F.Ct.ThrowIfCancellationRequested();
            base.Visit(node);
        }

        // Runs a body through the IOperation walker with the given enclosing-function key.
        private void Body(SyntaxNode node, string? fnKey)
        {
            if (fnKey == null) return;
            F.WalkOperations(node, fnKey);
        }

        private void WithFn(string? key, Action a)
        {
            Fn.Push(key);
            try { a(); }
            finally { Fn.Pop(); }
        }

        private void Guarded(string what, Action a)
        {
            try { a(); }
            catch (OperationCanceledException) { throw; }
            catch (InsufficientExecutionStackException) { throw; }
            catch (Exception ex) { F.Internal(what, ex); }
        }

        // ── namespaces & types ──

        public override void VisitNamespaceDeclaration(NamespaceDeclarationSyntax node) { EmitNamespaces(node); base.VisitNamespaceDeclaration(node); }

        public override void VisitFileScopedNamespaceDeclaration(FileScopedNamespaceDeclarationSyntax node) { EmitNamespaces(node); base.VisitFileScopedNamespaceDeclaration(node); }

        private void EmitNamespaces(BaseNamespaceDeclarationSyntax node) => Guarded("namespace", () =>
        {
            if (F.M.GetDeclaredSymbol(node, F.Ct) is not INamespaceSymbol ns) return;
            for (var n = ns; n != null && !n.IsGlobalNamespace; n = n.ContainingNamespace)
            {
                if (!F.P.IsCanonical(n, node)) continue;
                string? key = F.K.Of(n);
                if (key == null || !F.P.EmittedGlobal.TryAdd(key, 0)) continue;
                string? container = n.ContainingNamespace is { IsGlobalNamespace: false } pn ? F.K.Of(pn) : null;
                F.EmitSymbol(n, key, "namespace", n.Name, node, node.Name.GetLocation(), container, true);
            }
        });

        private void EmitType(BaseTypeDeclarationSyntax node) => Guarded("type", () =>
        {
            if (F.M.GetDeclaredSymbol(node, F.Ct) is not INamedTypeSymbol t) return;
            bool canonical = F.P.IsCanonical(t, node);
            string? key = F.K.Of(t);
            if (canonical)
            {
                bool partial = node.Modifiers.Any(m => m.IsKind(SyntaxKind.PartialKeyword));
                F.EmitSymbol(t, key, TypeKindName(t, node), t.Name, node, node.Identifier.GetLocation(), F.ContainerOf(t), true, w =>
                {
                    if (partial) w.WriteBoolean("partial", true);
                    if (t.Arity > 0) w.WriteNumber("arity", t.Arity);
                    if (t.DeclaringSyntaxReferences.Length > 1) w.WriteNumber("parts", t.DeclaringSyntaxReferences.Length);
                });
                F.EmitTypeInheritance(t);
                // Implicit parameterless constructor (target of `new T()` calls).
                foreach (var c in t.InstanceConstructors)
                    if (c.IsImplicitlyDeclared && t.TypeKind is TypeKind.Class or TypeKind.Struct && !t.IsStatic)
                        F.EmitSymbol(c, F.K.Of(c), "constructor", t.Name, node, node.Identifier.GetLocation(), key, true);
            }

            if (node is TypeDeclarationSyntax tds && tds.ParameterList != null)
            {
                // Primary constructor (records, C# 12 classes/structs) and positional record properties.
                var ctor = t.InstanceConstructors.FirstOrDefault(c => c.DeclaringSyntaxReferences.Any(r => r.SyntaxTree == tds.SyntaxTree && r.Span.Start >= tds.Span.Start && r.Span.End <= tds.Span.End && c.Parameters.Length == tds.ParameterList.Parameters.Count && !c.IsImplicitlyDeclared))
                           ?? t.InstanceConstructors.FirstOrDefault(c => c.Parameters.Length == tds.ParameterList.Parameters.Count && c.Parameters.All(p => p.Locations.Any(l => l.SourceTree == tds.SyntaxTree && tds.ParameterList.Span.Contains(l.SourceSpan))));
                if (ctor != null)
                {
                    string? ck = F.K.Of(ctor);
                    F.EmitSymbol(ctor, ck, "constructor", t.Name, tds, tds.Identifier.GetLocation(), key, true, w => w.WriteBoolean("primary", true));
                    F.EmitParams(ctor, ck, tds.ParameterList);
                    if (tds.BaseList != null)
                        foreach (var pb in tds.BaseList.Types.OfType<PrimaryConstructorBaseTypeSyntax>())
                            WithFn(ck, () => Body(pb, ck));
                }
                if (node is RecordDeclarationSyntax)
                {
                    foreach (var ps in tds.ParameterList.Parameters)
                    {
                        var prop = t.GetMembers(ps.Identifier.ValueText).OfType<IPropertySymbol>()
                            .FirstOrDefault(pp => pp.Locations.Any(l => l.SourceTree == tds.SyntaxTree && ps.Span.Contains(l.SourceSpan)));
                        if (prop != null)
                            F.EmitSymbol(prop, F.K.Of(prop), "property", prop.Name, ps, ps.Identifier.GetLocation(), key, true, w => w.WriteBoolean("positional", true));
                    }
                }
            }
        });

        public override void VisitClassDeclaration(ClassDeclarationSyntax node) { EmitType(node); base.VisitClassDeclaration(node); }
        public override void VisitStructDeclaration(StructDeclarationSyntax node) { EmitType(node); base.VisitStructDeclaration(node); }
        public override void VisitInterfaceDeclaration(InterfaceDeclarationSyntax node) { EmitType(node); base.VisitInterfaceDeclaration(node); }
        public override void VisitRecordDeclaration(RecordDeclarationSyntax node) { EmitType(node); base.VisitRecordDeclaration(node); }
        public override void VisitEnumDeclaration(EnumDeclarationSyntax node) { EmitType(node); base.VisitEnumDeclaration(node); }

        public override void VisitDelegateDeclaration(DelegateDeclarationSyntax node)
        {
            Guarded("delegate", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not INamedTypeSymbol t) return;
                F.EmitSymbol(t, F.K.Of(t), "delegate", t.Name, node, node.Identifier.GetLocation(), F.ContainerOf(t), true);
            });
            base.VisitDelegateDeclaration(node);
        }

        public override void VisitEnumMemberDeclaration(EnumMemberDeclarationSyntax node)
        {
            Guarded("enum member", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IFieldSymbol f) return;
                string? key = F.K.Of(f);
                F.EmitSymbol(f, key, "enum_const", f.Name, node, node.Identifier.GetLocation(), F.ContainerOf(f), true);
                if (key != null && f.HasConstantValue) F.EmitConst(key, f.ConstantValue);
            });
            base.VisitEnumMemberDeclaration(node);
        }

        // ── members ──

        public override void VisitMethodDeclaration(MethodDeclarationSyntax node)
        {
            string? key = null;
            Guarded("method", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IMethodSymbol m) return;
                key = F.K.Of(m);
                F.EmitMethodDecl(m, node, node.Identifier, "method");
                F.MethodEntries(m, key, node.AttributeLists);
            });
            WithFn(key, () =>
            {
                if (node.Body != null || node.ExpressionBody != null) Body(node, key);
                base.VisitMethodDeclaration(node);
            });
        }

        public override void VisitConstructorDeclaration(ConstructorDeclarationSyntax node)
        {
            string? key = null;
            Guarded("constructor", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IMethodSymbol m) return;
                key = F.K.Of(m);
                F.EmitMethodDecl(m, node, node.Identifier, "constructor");
            });
            WithFn(key, () =>
            {
                Body(node, key);
                base.VisitConstructorDeclaration(node);
            });
        }

        public override void VisitDestructorDeclaration(DestructorDeclarationSyntax node)
        {
            string? key = null;
            Guarded("destructor", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IMethodSymbol m) return;
                key = F.K.Of(m);
                F.EmitMethodDecl(m, node, node.Identifier, "method");
            });
            WithFn(key, () => { Body(node, key); base.VisitDestructorDeclaration(node); });
        }

        public override void VisitOperatorDeclaration(OperatorDeclarationSyntax node)
        {
            string? key = null;
            Guarded("operator", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IMethodSymbol m) return;
                key = F.K.Of(m);
                F.EmitMethodDecl(m, node, node.OperatorToken, "method");
            });
            WithFn(key, () => { Body(node, key); base.VisitOperatorDeclaration(node); });
        }

        public override void VisitConversionOperatorDeclaration(ConversionOperatorDeclarationSyntax node)
        {
            string? key = null;
            Guarded("conversion", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IMethodSymbol m) return;
                key = F.K.Of(m);
                F.EmitMethodDecl(m, node, node.OperatorKeyword, "method");
            });
            WithFn(key, () => { Body(node, key); base.VisitConversionOperatorDeclaration(node); });
        }

        public override void VisitPropertyDeclaration(PropertyDeclarationSyntax node)
        {
            Guarded("property", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IPropertySymbol p) return;
                bool isDef = !p.IsAbstract && !(p.ContainingType?.TypeKind == TypeKind.Interface && node.AccessorList?.Accessors.All(a => a.Body == null && a.ExpressionBody == null) != false && node.ExpressionBody == null);
                bool auto = node.ExpressionBody == null && node.AccessorList != null && node.AccessorList.Accessors.All(a => a.Body == null && a.ExpressionBody == null);
                F.EmitSymbol(p, F.K.Of(p), "property", p.Name, node, node.Identifier.GetLocation(), F.ContainerOf(p), isDef, w =>
                {
                    if (auto && isDef) w.WriteBoolean("auto", true);
                    if (p.IsReadOnly) w.WriteBoolean("readonly", true);
                });
            });
            base.VisitPropertyDeclaration(node);
        }

        public override void VisitIndexerDeclaration(IndexerDeclarationSyntax node)
        {
            Guarded("indexer", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IPropertySymbol p) return;
                string? key = F.K.Of(p);
                F.EmitSymbol(p, key, "property", "this[]", node, node.ThisKeyword.GetLocation(), F.ContainerOf(p), !p.IsAbstract, w => w.WriteBoolean("indexer", true));
                foreach (var prm in p.Parameters) F.EmitParam(prm, node);
            });
            base.VisitIndexerDeclaration(node);
        }

        public override void VisitEventDeclaration(EventDeclarationSyntax node)
        {
            Guarded("event", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IEventSymbol e) return;
                F.EmitSymbol(e, F.K.Of(e), "event", e.Name, node, node.Identifier.GetLocation(), F.ContainerOf(e), !e.IsAbstract);
            });
            base.VisitEventDeclaration(node);
        }

        public override void VisitAccessorDeclaration(AccessorDeclarationSyntax node)
        {
            if (node.Body == null && node.ExpressionBody == null)
            {
                // Auto / abstract / interface accessors: still call and override targets, so they get a symbol.
                Guarded("accessor", () =>
                {
                    if (F.M.GetDeclaredSymbol(node, F.Ct) is not IMethodSymbol acc || acc.AssociatedSymbol is IEventSymbol) return;
                    bool isDef = !acc.IsAbstract && acc.ContainingType?.TypeKind != TypeKind.Interface;
                    F.EmitSymbol(acc, F.K.Of(acc), "method", acc.Name, node, node.Keyword.GetLocation(), F.K.Of(acc.AssociatedSymbol) ?? F.ContainerOf(acc), isDef,
                        w => { w.WriteString("accessor", node.Keyword.ValueText); if (isDef) w.WriteBoolean("auto", true); });
                });
                base.VisitAccessorDeclaration(node);
                return;
            }
            string? key = null;
            Guarded("accessor", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IMethodSymbol acc) return;
                key = F.K.Of(acc);
                string? owner = F.K.Of(acc.AssociatedSymbol);
                F.EmitSymbol(acc, key, "method", acc.Name, node, node.Keyword.GetLocation(), owner ?? F.ContainerOf(acc), true, w => w.WriteString("accessor", node.Keyword.ValueText));
                foreach (var prm in acc.Parameters)
                {
                    // Indexer parameters are emitted with the indexer; only the implicit 'value' belongs to the accessor.
                    if (prm.Name == "value" && (node.IsKind(SyntaxKind.SetAccessorDeclaration) || node.IsKind(SyntaxKind.InitAccessorDeclaration) || node.IsKind(SyntaxKind.AddAccessorDeclaration) || node.IsKind(SyntaxKind.RemoveAccessorDeclaration)))
                        F.EmitSymbol(prm, F.K.Of(prm), "param", "value", node, node.Keyword.GetLocation(), key, true, w => { w.WriteNumber("pos", prm.Ordinal); w.WriteBoolean("implicit", true); }, Fmt.Display(acc, Fmt.Qual) + ".value");
                }
            });
            WithFn(key, () => { Body(node, key); base.VisitAccessorDeclaration(node); });
        }

        public override void VisitArrowExpressionClause(ArrowExpressionClauseSyntax node)
        {
            if (node.Parent is BasePropertyDeclarationSyntax prop)
            {
                string? key = null;
                Guarded("expression-bodied property", () =>
                {
                    if (F.M.GetDeclaredSymbol(prop, F.Ct) is not IPropertySymbol p || p.GetMethod == null) return;
                    key = F.K.Of(p.GetMethod);
                    F.EmitSymbol(p.GetMethod, key, "method", p.GetMethod.Name, node, node.GetLocation(), F.K.Of(p), true, w => w.WriteString("accessor", "get"));
                });
                WithFn(key, () => { Body(node, key); base.VisitArrowExpressionClause(node); });
                return;
            }
            base.VisitArrowExpressionClause(node);
        }

        public override void VisitEqualsValueClause(EqualsValueClauseSyntax node)
        {
            string? key = null;
            if (node.Parent is PropertyDeclarationSyntax pd)
                key = F.K.Of(F.M.GetDeclaredSymbol(pd, F.Ct));
            else if (node.Parent is VariableDeclaratorSyntax vd && vd.Parent?.Parent is BaseFieldDeclarationSyntax)
                key = F.K.Of(F.M.GetDeclaredSymbol(vd, F.Ct));
            if (key != null)
            {
                WithFn(key, () => { Body(node, key); base.VisitEqualsValueClause(node); });
                return;
            }
            base.VisitEqualsValueClause(node);
        }

        public override void VisitFieldDeclaration(FieldDeclarationSyntax node)
        {
            foreach (var v in node.Declaration.Variables)
            {
                Guarded("field", () =>
                {
                    if (F.M.GetDeclaredSymbol(v, F.Ct) is not IFieldSymbol f) return;
                    string? key = F.K.Of(f);
                    F.EmitSymbol(f, key, "field", f.Name, v, v.Identifier.GetLocation(), F.ContainerOf(f), true, w =>
                    {
                        if (f.IsConst) w.WriteBoolean("const", true);
                        if (f.IsReadOnly) w.WriteBoolean("readonly", true);
                        if (f.IsVolatile) w.WriteBoolean("volatile", true);
                    });
                    if (key == null) return;
                    if (f.HasConstantValue) F.EmitConst(key, f.ConstantValue);
                    else if (f.IsStatic && f.IsReadOnly && v.Initializer?.Value is LiteralExpressionSyntax lit)
                        F.EmitConst(key, F.M.GetConstantValue(lit, F.Ct) is { HasValue: true } cv ? cv.Value : lit.Token.ValueText);
                });
            }
            base.VisitFieldDeclaration(node);
        }

        public override void VisitEventFieldDeclaration(EventFieldDeclarationSyntax node)
        {
            foreach (var v in node.Declaration.Variables)
            {
                Guarded("event field", () =>
                {
                    if (F.M.GetDeclaredSymbol(v, F.Ct) is not IEventSymbol e) return;
                    F.EmitSymbol(e, F.K.Of(e), "event", e.Name, v, v.Identifier.GetLocation(), F.ContainerOf(e), true, w => w.WriteBoolean("field_like", true));
                });
            }
            base.VisitEventFieldDeclaration(node);
        }

        // ── top-level statements ──

        private string? _mainKey;
        private bool _mainEmitted;

        public override void VisitGlobalStatement(GlobalStatementSyntax node)
        {
            if (!_mainEmitted)
            {
                _mainEmitted = true;
                Guarded("top-level statements", () =>
                {
                    var cu = (CompilationUnitSyntax)node.Parent!;
                    IMethodSymbol? main = F.M.GetDeclaredSymbol(cu, F.Ct) as IMethodSymbol;
                    if (main == null && F.P.EntryPoint is { } ep && ep.DeclaringSyntaxReferences.Any(r => r.SyntaxTree == cu.SyntaxTree)) main = ep;
                    if (main == null) return;
                    _mainKey = F.K.Of(main);
                    var first = cu.Members.OfType<GlobalStatementSyntax>().First();
                    var last = cu.Members.OfType<GlobalStatementSyntax>().Last();
                    var span = Microsoft.CodeAnalysis.Text.TextSpan.FromBounds(first.SpanStart, last.Span.End);
                    int endLine = cu.SyntaxTree.GetLineSpan(span).EndLinePosition.Line + 1;
                    if (main.ContainingType is { IsImplicitlyDeclared: true } prog && F.K.Of(prog) is string pk && F.P.EmittedGlobal.TryAdd(pk + "@" + F.P.Info.Name, 0))
                        F.EmitSymbol(prog, pk, "class", prog.Name, first, first.GetLocation(), null, true,
                            w => w.WriteBoolean("top_level", true), qualOverride: prog.Name, endOverride: endLine);
                    string mainQual = (main.ContainingType?.Name ?? "Program") + "." + main.Name;
                    F.EmitSymbol(main, _mainKey, "method", main.Name, first, first.GetLocation(), F.ContainerOf(main), true,
                        w => w.WriteBoolean("top_level", true), qualOverride: mainQual, endOverride: endLine);
                    F.EmitParams(main, _mainKey, first);
                    F.Entry(_mainKey, "main", "top-level statements", "exact");
                });
            }
            WithFn(_mainKey, () => { Body(node.Statement, _mainKey); base.VisitGlobalStatement(node); });
        }

        // ── local functions, lambdas, locals ──

        public override void VisitLocalFunctionStatement(LocalFunctionStatementSyntax node)
        {
            string? key = null;
            Guarded("local function", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is not IMethodSymbol m) return;
                key = F.K.Of(m);
                F.EmitSymbol(m, key, "function", m.Name, node, node.Identifier.GetLocation(), CurFn, HasBody(node), w => w.WriteBoolean("local_function", true),
                    qualOverride: F.QualOf(m.ContainingSymbol) + "." + m.Name);
                F.EmitParams(m, key, node);
            });
            WithFn(key ?? CurFn, () => base.VisitLocalFunctionStatement(node));
        }

        private void Lambda(AnonymousFunctionExpressionSyntax node, Action visitBase)
        {
            string? key = null;
            Guarded("lambda", () =>
            {
                var si = F.M.GetSymbolInfo(node, F.Ct);
                var m = si.Symbol as IMethodSymbol ?? si.CandidateSymbols.OfType<IMethodSymbol>().FirstOrDefault();
                if (m == null)
                {
                    var (l, c) = Pos(node);
                    key = (CurFn ?? "?") + $"#lambda@{l}:{c}";
                    return;
                }
                key = F.K.Of(m);
                F.EmitLambda(m, node, CurFn);
            });
            WithFn(key ?? CurFn, visitBase);
        }

        public override void VisitParenthesizedLambdaExpression(ParenthesizedLambdaExpressionSyntax node) => Lambda(node, () => base.VisitParenthesizedLambdaExpression(node));
        public override void VisitSimpleLambdaExpression(SimpleLambdaExpressionSyntax node) => Lambda(node, () => base.VisitSimpleLambdaExpression(node));
        public override void VisitAnonymousMethodExpression(AnonymousMethodExpressionSyntax node) => Lambda(node, () => base.VisitAnonymousMethodExpression(node));

        public override void VisitVariableDeclarator(VariableDeclaratorSyntax node)
        {
            if (node.Parent is VariableDeclarationSyntax { Parent: LocalDeclarationStatementSyntax or ForStatementSyntax or UsingStatementSyntax or FixedStatementSyntax })
                Guarded("local", () =>
                {
                    if (F.M.GetDeclaredSymbol(node, F.Ct) is ILocalSymbol l) F.EmitLocal(l, node.Identifier, node);
                });
            base.VisitVariableDeclarator(node);
        }

        public override void VisitSingleVariableDesignation(SingleVariableDesignationSyntax node)
        {
            Guarded("local", () =>
            {
                if (F.M.GetDeclaredSymbol(node, F.Ct) is ILocalSymbol l) F.EmitLocal(l, node.Identifier, node);
            });
            base.VisitSingleVariableDesignation(node);
        }

        public override void VisitCatchDeclaration(CatchDeclarationSyntax node)
        {
            if (node.Identifier.ValueText.Length > 0)
                Guarded("catch local", () =>
                {
                    if (F.M.GetDeclaredSymbol(node, F.Ct) is ILocalSymbol l) F.EmitLocal(l, node.Identifier, node);
                });
            base.VisitCatchDeclaration(node);
        }

        // ── branches ──

        private void Branch(string kind, SyntaxNode at, string cond, IReadOnlyList<string> reads, (int, int)? then, (int, int)? els, Action<Utf8JsonWriter>? extra = null)
        {
            var (line, _) = Pos(at);
            int end = EndLine(at);
            string? fn = CurFn;
            F.B.Record("branch", w =>
            {
                w.Str("fn", fn);
                w.WriteString("file", F.FilePath);
                w.WriteNumber("line", line);
                w.WriteNumber("end_line", end);
                w.WriteString("kind", kind);
                w.Str("cond", cond);
                w.Arr("reads", reads);
                w.Range("then", then);
                w.Range("else", els);
                extra?.Invoke(w);
            });
        }

        public override void VisitIfStatement(IfStatementSyntax node)
        {
            Guarded("if", () =>
            {
                bool thenExits = F.G.Exits(node.Statement);
                bool elseExits = node.Else != null && F.G.Exits(node.Else.Statement);
                string kind = node.Else == null && thenExits ? "early_exit" : "if";
                string? exits = thenExits && elseExits ? "both" : thenExits ? "then" : elseExits ? "else" : null;
                Branch(kind, node, J.Snip(node.Condition.ToString()), F.G.Reads(node.Condition), Range(node.Statement),
                    node.Else != null ? Range(node.Else.Statement) : null, w => { if (exits != null) w.WriteString("exits", exits); });
            });
            base.VisitIfStatement(node);
        }

        public override void VisitSwitchStatement(SwitchStatementSyntax node)
        {
            Guarded("switch", () =>
            {
                var reads = F.G.Reads(node.Expression);
                (int, int)? body = node.Sections.Count > 0 ? (Range(node.Sections.First()).Item1, Range(node.Sections.Last()).Item2) : null;
                Branch("switch", node, J.Snip(node.Expression.ToString()), reads, body, null, w => w.WriteNumber("cases", node.Sections.Count));
                foreach (var sec in node.Sections)
                {
                    var g = F.G.CaseGuard(node, sec);
                    bool isDefault = sec.Labels.Any(l => l is DefaultSwitchLabelSyntax);
                    Branch("case", sec, g.Cond, g.Reads, Range(sec), null, w => { if (isDefault) w.WriteBoolean("default", true); w.WriteBoolean("polarity", g.Polarity); });
                }
            });
            base.VisitSwitchStatement(node);
        }

        public override void VisitSwitchExpression(SwitchExpressionSyntax node)
        {
            Guarded("switch expression", () =>
            {
                Branch("switch", node, J.Snip(node.GoverningExpression.ToString()), F.G.Reads(node.GoverningExpression), Range(node), null, w => w.WriteNumber("cases", node.Arms.Count));
                foreach (var arm in node.Arms)
                {
                    var g = F.G.ArmGuard(node, arm);
                    Branch("case", arm, g.Cond, g.Reads, Range(arm.Expression), null, w => w.WriteBoolean("polarity", g.Polarity));
                }
            });
            base.VisitSwitchExpression(node);
        }

        public override void VisitWhileStatement(WhileStatementSyntax node)
        {
            Guarded("while", () => Branch("while", node, J.Snip(node.Condition.ToString()), F.G.Reads(node.Condition), Range(node.Statement), null));
            base.VisitWhileStatement(node);
        }

        public override void VisitDoStatement(DoStatementSyntax node)
        {
            Guarded("do", () => Branch("do", node, J.Snip(node.Condition.ToString()), F.G.Reads(node.Condition), Range(node.Statement), null));
            base.VisitDoStatement(node);
        }

        public override void VisitForStatement(ForStatementSyntax node)
        {
            Guarded("for", () => Branch("for", node, node.Condition != null ? J.Snip(node.Condition.ToString()) : "true",
                node.Condition != null ? F.G.Reads(node.Condition) : Array.Empty<string>(), Range(node.Statement), null));
            base.VisitForStatement(node);
        }

        public override void VisitForEachStatement(ForEachStatementSyntax node)
        {
            Guarded("foreach", () =>
            {
                var g = F.G.ForEachGuard(node);
                Branch("for", node, g.Cond, g.Reads, Range(node.Statement), null, w => w.WriteBoolean("foreach", true));
                if (F.M.GetDeclaredSymbol(node, F.Ct) is ILocalSymbol l) F.EmitLocal(l, node.Identifier, node);
            });
            base.VisitForEachStatement(node);
        }

        public override void VisitForEachVariableStatement(ForEachVariableStatementSyntax node)
        {
            Guarded("foreach", () =>
            {
                var g = F.G.ForEachGuard(node);
                Branch("for", node, g.Cond, g.Reads, Range(node.Statement), null, w => w.WriteBoolean("foreach", true));
            });
            base.VisitForEachVariableStatement(node);
        }

        public override void VisitCatchClause(CatchClauseSyntax node)
        {
            // C# reports failures by throwing: a catch block is the decision "the try block threw this exception"
            Guarded("catch", () =>
            {
                string type = node.Declaration?.Type.ToString() ?? "Exception";
                string cond = $"catch ({type})" + (node.Filter != null ? $" when ({J.Snip(node.Filter.FilterExpression.ToString())})" : "");
                IReadOnlyList<string> reads = node.Filter != null ? F.G.Reads(node.Filter.FilterExpression) : Array.Empty<string>();
                Branch("catch", node, cond, reads, Range(node.Block), null);
            });
            base.VisitCatchClause(node);
        }

        public override void VisitConditionalExpression(ConditionalExpressionSyntax node)
        {
            Guarded("ternary", () => Branch("ternary", node, J.Snip(node.Condition.ToString()), F.G.Reads(node.Condition), Range(node.WhenTrue), Range(node.WhenFalse)));
            base.VisitConditionalExpression(node);
        }

        // ── type references ──

        public override void VisitIdentifierName(IdentifierNameSyntax node)
        {
            if (F.P.Opt.TypeOcc && F.P.SourceTypeNames.Contains(node.Identifier.ValueText) &&
                !(node.Parent is MemberAccessExpressionSyntax ma && ma.Name == node) && node.Parent is not MemberBindingExpressionSyntax)
                Guarded("type ref", () => F.TypeOcc(node, CurFn));
            base.VisitIdentifierName(node);
        }

        public override void VisitGenericName(GenericNameSyntax node)
        {
            if (F.P.Opt.TypeOcc && F.P.SourceTypeNames.Contains(node.Identifier.ValueText) &&
                !(node.Parent is MemberAccessExpressionSyntax ma && ma.Name == node))
                Guarded("type ref", () => F.TypeOcc(node, CurFn));
            base.VisitGenericName(node);
        }

        // ── event wiring fallback, configuration keys ──

        public override void VisitAssignmentExpression(AssignmentExpressionSyntax node)
        {
            if (node.IsKind(SyntaxKind.AddAssignmentExpression) || node.IsKind(SyntaxKind.SubtractAssignmentExpression))
                Guarded("subscribe (heuristic)", () => F.HeuristicSubscribe(node, CurFn));
            base.VisitAssignmentExpression(node);
        }

        public override void VisitElementAccessExpression(ElementAccessExpressionSyntax node)
        {
            Guarded("config", () => F.ConfigFromElementAccess(node, CurFn));
            base.VisitElementAccessExpression(node);
        }

        public override void VisitInvocationExpression(InvocationExpressionSyntax node)
        {
            Guarded("config", () => F.ConfigFromInvocation(node, CurFn));
            base.VisitInvocationExpression(node);
        }
    }

    // ───────────────────────────── syntax-level facts ─────────────────────────────

    private void TypeOcc(SimpleNameSyntax node, string? fn)
    {
        var si = M.GetSymbolInfo(node, Ct);
        ISymbol? s = si.Symbol ?? (si.CandidateSymbols.Length == 1 ? si.CandidateSymbols[0] : null);
        if (s is IAliasSymbol a) s = a.Target;
        if (s is IMethodSymbol { MethodKind: MethodKind.Constructor } c) s = c.ContainingType;
        if (s is not INamedTypeSymbol t || !P.IsSource(t)) return;
        string? key = K.Of(t);
        if (key == null) return;
        var (line, col) = Pos(node);
        B.Record("occ", w =>
        {
            w.WriteString("sym", key);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.WriteNumber("col", col);
            w.WriteString("role", "type");
            w.Str("in", fn);
        });
    }

    private void HeuristicSubscribe(AssignmentExpressionSyntax node, string? fn)
    {
        var op = M.GetOperation(node, Ct);
        if (op is IEventAssignmentOperation) return; // resolved: handled by the operation walker
        // Resolved compound assignments are arithmetic or delegate combination (the latter is covered by fnaddr).
        if (op is ICompoundAssignmentOperation { Target.Type.TypeKind: not TypeKind.Error }) return;
        ExpressionSyntax rhs = node.Right;
        if (rhs is ObjectCreationExpressionSyntax { ArgumentList.Arguments.Count: 1 } oce) rhs = oce.ArgumentList!.Arguments[0].Expression;
        var si = M.GetSymbolInfo(rhs, Ct);
        var m = si.Symbol as IMethodSymbol ?? si.CandidateSymbols.OfType<IMethodSymbol>().FirstOrDefault();
        string? handler;
        if (m != null && P.IsSource(m)) handler = K.Of(m);
        else if (rhs is AnonymousFunctionExpressionSyntax lam && M.GetSymbolInfo(lam, Ct).Symbol is IMethodSymbol lm) handler = K.Of(lm);
        else return;
        if (handler == null) return;
        var lsi = M.GetSymbolInfo(node.Left, Ct);
        var ev = lsi.Symbol as IEventSymbol ?? lsi.CandidateSymbols.OfType<IEventSymbol>().FirstOrDefault();
        string evKey = ev != null ? K.Of(ev) ?? "?" : "?:" + J.Snip(node.Left.ToString(), 200);
        string opText = node.IsKind(SyntaxKind.AddAssignmentExpression) ? "+=" : "-=";
        var (line, _) = Pos(node);
        B.Record("subscribe", w =>
        {
            w.WriteString("event", evKey);
            w.WriteString("handler", handler);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.Str("fn", fn);
            w.WriteString("op", opText);
            w.WriteString("res", "heuristic");
        });
        if (opText == "+=") Entry(handler, "event", IsDesigner ? evKey + " (designer)" : evKey, "heuristic");
    }

    private static string? StringLiteral(ArgumentSyntax? a) =>
        a?.Expression is LiteralExpressionSyntax lit && lit.IsKind(SyntaxKind.StringLiteralExpression) ? lit.Token.ValueText : null;

    /// <summary>Resolves "Section:" prefixes from chained GetSection("...") calls.</summary>
    private string ConfigPrefix(ExpressionSyntax receiver, int depth = 0)
    {
        if (depth > 8) return "";
        if (receiver is InvocationExpressionSyntax inv && inv.Expression is MemberAccessExpressionSyntax ma &&
            ma.Name.Identifier.ValueText is "GetSection" or "GetRequiredSection" && StringLiteral(inv.ArgumentList.Arguments.FirstOrDefault()) is string sec)
            return ConfigPrefix(ma.Expression, depth + 1) + sec + ":";
        return "";
    }

    private string? AssignTargetOf(SyntaxNode node)
    {
        SyntaxNode cur = node;
        for (int i = 0; i < 12 && cur.Parent != null; i++)
        {
            var p = cur.Parent;
            switch (p)
            {
                case AssignmentExpressionSyntax a when a.Right == cur:
                    return K.Of(M.GetSymbolInfo(a.Left, Ct).Symbol);
                case EqualsValueClauseSyntax { Parent: VariableDeclaratorSyntax vd }:
                    return K.Of(M.GetDeclaredSymbol(vd, Ct));
                case EqualsValueClauseSyntax { Parent: PropertyDeclarationSyntax pd }:
                    return K.Of(M.GetDeclaredSymbol(pd, Ct));
                case CastExpressionSyntax or ParenthesizedExpressionSyntax or PostfixUnaryExpressionSyntax or BinaryExpressionSyntax or ArgumentSyntax or ArgumentListSyntax:
                    if (p is ArgumentSyntax or ArgumentListSyntax) return null;
                    cur = p;
                    continue;
                case InvocationExpressionSyntax or MemberAccessExpressionSyntax:
                    cur = p; // e.g. config["k"].Trim()
                    continue;
                default:
                    return null;
            }
        }
        return null;
    }

    private void EmitConfig(string key, string? sym, SyntaxNode at, string via, string? fn)
    {
        var (line, _) = Pos(at);
        B.Record("config_key", w =>
        {
            w.Str("key", key);
            w.Str("sym", sym);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.WriteString("via", via);
            w.Str("fn", fn);
        });
    }

    private void ConfigFromElementAccess(ElementAccessExpressionSyntax node, string? fn)
    {
        if (node.ArgumentList.Arguments.Count != 1 || StringLiteral(node.ArgumentList.Arguments[0]) is not string k) return;
        var recvType = M.GetTypeInfo(node.Expression, Ct).Type;
        string recvText = node.Expression.ToString();
        bool sectionCall = node.Expression is InvocationExpressionSyntax { Expression: MemberAccessExpressionSyntax { Name.Identifier.ValueText: "GetSection" or "GetRequiredSection" } };
        if (!sectionCall && !ApiTables.LooksLikeConfig(recvType, recvText)) return;
        string via = recvText.EndsWith("AppSettings", StringComparison.Ordinal) ? "AppSettings[]" : recvText.EndsWith("ConnectionStrings", StringComparison.Ordinal) ? "ConnectionStrings[]" : "IConfiguration[]";
        EmitConfig(ConfigPrefix(node.Expression) + k, AssignTargetOf(node), node, via, fn);
    }

    private void ConfigFromInvocation(InvocationExpressionSyntax node, string? fn)
    {
        if (node.Expression is not MemberAccessExpressionSyntax ma) return;
        string name = ma.Name.Identifier.ValueText;
        if (!ApiTables.ConfigMethods.Contains(name)) return;
        if (StringLiteral(node.ArgumentList.Arguments.FirstOrDefault()) is not string k) return;
        var recvType = M.GetTypeInfo(ma.Expression, Ct).Type;
        if (!ApiTables.LooksLikeConfig(recvType, ma.Expression.ToString())) return;
        EmitConfig(ConfigPrefix(ma.Expression) + k, AssignTargetOf(node), node, name, fn);
    }
}
