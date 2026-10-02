using System.Collections.Concurrent;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace Verity.Roslyn;

internal sealed class FileMeta
{
    public required string Abs;
    public required string Rel;
    public required string Sha;
    public required int Loc;
    public required SyntaxTree Tree;
}

/// <summary>Shared, thread-safe state for analysing the files of one compilation.</summary>
internal sealed class ProjectContext
{
    public required ProjectInfo Info;
    public required CSharpCompilation Compilation;
    public required KeyCache Keys;
    public required Options Opt;
    public required HashSet<string> SourceTypeNames;
    public required ConcurrentDictionary<string, byte> EmittedGlobal;
    public required string Profile;
    public IMethodSymbol? EntryPoint;

    private readonly ConcurrentDictionary<ISymbol, (string Path, int Pos)> _canonical = new(SymbolEqualityComparer.Default);
    private readonly ConcurrentDictionary<ISymbol, bool> _hasBody = new(SymbolEqualityComparer.Default);
    private readonly ConcurrentDictionary<ISymbol, bool> _isSource = new(SymbolEqualityComparer.Default);

    /// <summary>
    /// True when <paramref name="node"/> contains the first declaration (by path, position) of a symbol
    /// that has several declarations (partial types, namespaces). Makes emission deterministic under parallelism.
    /// </summary>
    public bool IsCanonical(ISymbol s, SyntaxNode node)
    {
        var c = _canonical.GetOrAdd(s, sym =>
        {
            (string, int) best = ("￿", int.MaxValue);
            foreach (var r in sym.DeclaringSyntaxReferences)
            {
                var cand = (r.SyntaxTree.FilePath, r.Span.Start);
                int cmp = string.CompareOrdinal(cand.FilePath, best.Item1);
                if (cmp < 0 || (cmp == 0 && cand.Start < best.Item2)) best = cand;
            }
            return best;
        });
        return c.Path == node.SyntaxTree.FilePath && c.Pos >= node.Span.Start && c.Pos < Math.Max(node.Span.End, node.Span.Start + 1);
    }

    public bool IsSource(ISymbol? s)
    {
        if (s == null) return false;
        var d = s.OriginalDefinition ?? s;
        return _isSource.GetOrAdd(d, x => x.Locations.Any(l => l.IsInSource));
    }

    /// <summary>True when a method/accessor has an explicit body in source (not an auto-accessor).</summary>
    public bool HasExplicitBody(IMethodSymbol m)
    {
        return _hasBody.GetOrAdd(m.OriginalDefinition, x =>
        {
            foreach (var r in x.DeclaringSyntaxReferences)
            {
                var syn = r.GetSyntax();
                switch (syn)
                {
                    case AccessorDeclarationSyntax a when a.Body != null || a.ExpressionBody != null: return true;
                    case ArrowExpressionClauseSyntax: return true;
                    case BaseMethodDeclarationSyntax b when b.Body != null || b.ExpressionBody != null: return true;
                    case PropertyDeclarationSyntax p when p.ExpressionBody != null: return true;
                    case IndexerDeclarationSyntax i when i.ExpressionBody != null: return true;
                }
            }
            return false;
        });
    }
}
