using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Operations;

namespace Verity.Roslyn;

internal sealed class Guard
{
    public required string Cond;
    public required bool Polarity;
    public required int Line;
    public required string Kind; // if | else | early_exit | loop | case | ternary
    public required IReadOnlyList<string> Reads;

    public void Write(Utf8JsonWriter w)
    {
        w.WriteStartObject();
        w.Str("cond", Cond);
        w.WriteBoolean("polarity", Polarity);
        w.WriteNumber("line", Line);
        w.WriteString("kind", Kind);
        w.Arr("reads", Reads);
        w.WriteEndObject();
    }
}

/// <summary>
/// Computes the chain of conditions that must hold for a syntax node to execute, from the
/// function entry inwards. Early-exit guards come from preceding sibling statements such as
/// <c>if (c) return;</c> (the rest of the block only runs when <c>c</c> is false).
/// </summary>
internal sealed class GuardComputer
{
    private const int MaxGuards = 32;
    private readonly SemanticModel _model;
    private readonly KeyCache _keys;
    private readonly CancellationToken _ct;
    private readonly Dictionary<SyntaxNode, List<Guard>> _stmtCache = new();
    private readonly Dictionary<SyntaxNode, IReadOnlyList<string>> _readsCache = new();
    private readonly Dictionary<SyntaxNode, int> _exitCache = new();
    private readonly Dictionary<SyntaxNode, BlockInfo> _blockCache = new();

    private sealed class BlockInfo
    {
        public readonly Dictionary<SyntaxNode, int> Index = new();
        public readonly List<(int Index, Guard G)> Exits = new();
    }

    public GuardComputer(SemanticModel model, KeyCache keys, CancellationToken ct)
    {
        _model = model;
        _keys = keys;
        _ct = ct;
    }

    public static bool IsBoundary(SyntaxNode n) =>
        (n is MemberDeclarationSyntax && n is not GlobalStatementSyntax) ||
        n is AccessorDeclarationSyntax || n is AnonymousFunctionExpressionSyntax ||
        n is LocalFunctionStatementSyntax || n is CompilationUnitSyntax;

    /// <summary>Guards controlling <paramref name="node"/>, outermost first.</summary>
    public List<Guard> For(SyntaxNode node)
    {
        var expr = new List<Guard>(); // inner → outer
        SyntaxNode cur = node;
        while (true)
        {
            if (cur is StatementSyntax st)
            {
                var sg = StmtGuards(st);
                if (expr.Count == 0) return sg;
                var all = new List<Guard>(sg.Count + expr.Count);
                all.AddRange(sg);
                for (int i = expr.Count - 1; i >= 0; i--) all.Add(expr[i]);
                return Cap(all);
            }
            var parent = cur.Parent;
            if (parent == null || IsBoundary(parent))
            {
                expr.Reverse();
                return Cap(expr);
            }
            AddExprGuard(parent, cur, expr);
            cur = parent;
        }
    }

    private static List<Guard> Cap(List<Guard> g) => g.Count <= MaxGuards ? g : g.GetRange(g.Count - MaxGuards, MaxGuards);

    private List<Guard> StmtGuards(StatementSyntax s)
    {
        if (_stmtCache.TryGetValue(s, out var cached)) return cached;
        _ct.ThrowIfCancellationRequested();
        var local = new List<Guard>(); // outer → inner, contributed by s's immediate context
        SyntaxNode? parent = s.Parent;
        SyntaxNode? climbFrom = parent;
        switch (parent)
        {
            case null:
                break;
            case BlockSyntax b:
                AddEarlyExits(b, b.Statements, s, local);
                break;
            case SwitchSectionSyntax sec:
                if (sec.Parent is SwitchStatementSyntax sw) local.Add(CaseGuard(sw, sec));
                AddEarlyExits(sec, sec.Statements, s, local);
                climbFrom = sec.Parent;
                break;
            case GlobalStatementSyntax gs:
                if (gs.Parent is CompilationUnitSyntax cu)
                {
                    var stmts = cu.Members.OfType<GlobalStatementSyntax>().Select(g => g.Statement).ToList();
                    AddEarlyExits(cu, stmts, s, local);
                }
                climbFrom = null;
                break;
            case IfStatementSyntax ifs when ifs.Statement == s:
                local.Add(Make(ifs.Condition, true, "if", ifs));
                break;
            case ElseClauseSyntax els when els.Parent is IfStatementSyntax pif:
                local.Add(Make(pif.Condition, false, "else", pif));
                climbFrom = pif;
                break;
            case WhileStatementSyntax ws when ws.Statement == s:
                local.Add(Make(ws.Condition, true, "loop", ws));
                break;
            case DoStatementSyntax ds when ds.Statement == s:
                local.Add(Make(ds.Condition, true, "loop", ds));
                break;
            case ForStatementSyntax fs when fs.Statement == s:
                if (fs.Condition != null) local.Add(Make(fs.Condition, true, "loop", fs));
                else local.Add(new Guard { Cond = "true", Polarity = true, Line = LineOf(fs), Kind = "loop", Reads = Array.Empty<string>() });
                break;
            case CommonForEachStatementSyntax fe when fe.Statement == s:
                local.Add(ForEachGuard(fe));
                break;
        }

        List<Guard> rest = new();
        if (climbFrom != null && !IsBoundary(climbFrom))
        {
            SyntaxNode? anc = climbFrom;
            while (anc != null && anc is not StatementSyntax && !IsBoundary(anc))
                anc = anc.Parent;
            if (anc is StatementSyntax ast) rest = StmtGuards(ast);
        }

        List<Guard> result;
        if (rest.Count == 0) result = Cap(local);
        else if (local.Count == 0) result = rest;
        else
        {
            result = new List<Guard>(rest.Count + local.Count);
            result.AddRange(rest);
            result.AddRange(local);
            result = Cap(result);
        }
        _stmtCache[s] = result;
        return result;
    }

    private void AddEarlyExits(SyntaxNode container, IReadOnlyList<StatementSyntax> stmts, StatementSyntax s, List<Guard> acc)
    {
        if (!_blockCache.TryGetValue(container, out var info))
        {
            info = new BlockInfo();
            for (int i = 0; i < stmts.Count; i++)
            {
                info.Index[stmts[i]] = i;
                if (stmts[i] is IfStatementSyntax ifs)
                {
                    foreach (var g in EarlyExitGuards(ifs)) info.Exits.Add((i, g));
                }
            }
            _blockCache[container] = info;
        }
        if (!info.Index.TryGetValue(s, out int idx)) return;
        int start = acc.Count;
        foreach (var (i, g) in info.Exits)
        {
            if (i >= idx) break;
            acc.Add(g);
        }
        // Keep only the nearest guards if a block has an extreme number of early exits.
        if (acc.Count - start > MaxGuards) acc.RemoveRange(start, acc.Count - start - MaxGuards);
    }

    /// <summary>Guards implied for code following an if statement whose branch always exits.</summary>
    public IEnumerable<Guard> EarlyExitGuards(IfStatementSyntax ifs)
    {
        // Walk an else-if chain: every link whose then-branch exits contributes "cond is false".
        IfStatementSyntax? cur = ifs;
        int depth = 0;
        while (cur != null && depth++ < 16)
        {
            bool thenExits = Exits(cur.Statement);
            var elseStmt = cur.Else?.Statement;
            if (thenExits)
            {
                yield return Make(cur.Condition, false, "early_exit", cur);
                cur = elseStmt as IfStatementSyntax;
                if (cur == null) yield break;
                continue;
            }
            if (elseStmt != null && elseStmt is not IfStatementSyntax && Exits(elseStmt))
                yield return Make(cur.Condition, true, "early_exit", cur);
            yield break;
        }
    }

    /// <summary>True when control never reaches the end of <paramref name="stmt"/> (return/throw/break/continue/goto).</summary>
    public bool Exits(StatementSyntax stmt)
    {
        if (_exitCache.TryGetValue(stmt, out int v)) return v == 1;
        bool exits;
        try
        {
            var cf = _model.AnalyzeControlFlow(stmt);
            exits = cf != null && cf.Succeeded ? !cf.EndPointIsReachable : SyntacticExits(stmt);
        }
        catch (OperationCanceledException) { throw; }
        catch { exits = SyntacticExits(stmt); }
        _exitCache[stmt] = exits ? 1 : 0;
        return exits;
    }

    private static bool SyntacticExits(StatementSyntax s) => s switch
    {
        ReturnStatementSyntax or ThrowStatementSyntax or BreakStatementSyntax or ContinueStatementSyntax or GotoStatementSyntax => true,
        YieldStatementSyntax y => y.IsKind(SyntaxKind.YieldBreakStatement),
        ExpressionStatementSyntax e => e.Expression is ThrowExpressionSyntax,
        BlockSyntax b => b.Statements.Count > 0 && SyntacticExits(b.Statements.Last()),
        IfStatementSyntax i => i.Else != null && SyntacticExits(i.Statement) && SyntacticExits(i.Else.Statement),
        _ => false,
    };

    private void AddExprGuard(SyntaxNode parent, SyntaxNode cur, List<Guard> acc)
    {
        switch (parent)
        {
            case ConditionalExpressionSyntax c when cur == c.WhenTrue:
                acc.Add(Make(c.Condition, true, "ternary", c));
                break;
            case ConditionalExpressionSyntax c when cur == c.WhenFalse:
                acc.Add(Make(c.Condition, false, "ternary", c));
                break;
            case BinaryExpressionSyntax b when cur == b.Right && b.IsKind(SyntaxKind.LogicalAndExpression):
                acc.Add(Make(b.Left, true, "if", b));
                break;
            case BinaryExpressionSyntax b when cur == b.Right && b.IsKind(SyntaxKind.LogicalOrExpression):
                acc.Add(Make(b.Left, false, "if", b));
                break;
            case BinaryExpressionSyntax b when cur == b.Right && b.IsKind(SyntaxKind.CoalesceExpression):
                acc.Add(new Guard { Cond = J.Snip(b.Left.ToString()) + " == null", Polarity = true, Line = LineOf(b), Kind = "if", Reads = Reads(b.Left) });
                break;
            case AssignmentExpressionSyntax a when cur == a.Right && a.IsKind(SyntaxKind.CoalesceAssignmentExpression):
                acc.Add(new Guard { Cond = J.Snip(a.Left.ToString()) + " == null", Polarity = true, Line = LineOf(a), Kind = "if", Reads = Reads(a.Left) });
                break;
            case ConditionalAccessExpressionSyntax ca when cur == ca.WhenNotNull:
                acc.Add(new Guard { Cond = J.Snip(ca.Expression.ToString()) + " != null", Polarity = true, Line = LineOf(ca), Kind = "if", Reads = Reads(ca.Expression) });
                break;
            case SwitchExpressionArmSyntax arm when cur == arm.Expression && arm.Parent is SwitchExpressionSyntax se:
                acc.Add(ArmGuard(se, arm));
                break;
        }
    }

    public Guard CaseGuard(SwitchStatementSyntax sw, SwitchSectionSyntax sec)
    {
        string gov = J.Snip(sw.Expression.ToString(), 120);
        var reads = new List<string>(Reads(sw.Expression));
        bool isDefault = sec.Labels.Any(l => l is DefaultSwitchLabelSyntax);
        if (isDefault)
        {
            var others = sw.Sections.Where(x => x != sec).SelectMany(x => x.Labels).Where(l => l is not DefaultSwitchLabelSyntax)
                .Select(l => LabelCond(gov, l)).ToList();
            if (others.Count == 0)
                return new Guard { Cond = gov + " (default)", Polarity = true, Line = LineOf(sec), Kind = "case", Reads = reads };
            return new Guard { Cond = J.Snip(string.Join(" || ", others), 400), Polarity = false, Line = LineOf(sec), Kind = "case", Reads = reads };
        }
        foreach (var l in sec.Labels)
        {
            if (l is CaseSwitchLabelSyntax c) reads.AddRange(Reads(c.Value));
            else if (l is CasePatternSwitchLabelSyntax cp && cp.WhenClause != null) reads.AddRange(Reads(cp.WhenClause.Condition));
        }
        string cond = string.Join(" || ", sec.Labels.Select(l => LabelCond(gov, l)));
        return new Guard { Cond = J.Snip(cond, 400), Polarity = true, Line = LineOf(sec), Kind = "case", Reads = reads.Distinct().ToList() };
    }

    private static string LabelCond(string gov, SwitchLabelSyntax l) => l switch
    {
        CaseSwitchLabelSyntax c => $"{gov} == {J.Snip(c.Value.ToString(), 120)}",
        CasePatternSwitchLabelSyntax p => $"{gov} is {J.Snip(p.Pattern.ToString(), 120)}" + (p.WhenClause != null ? $" && ({J.Snip(p.WhenClause.Condition.ToString(), 120)})" : ""),
        _ => "default",
    };

    public Guard ArmGuard(SwitchExpressionSyntax se, SwitchExpressionArmSyntax arm)
    {
        string gov = J.Snip(se.GoverningExpression.ToString(), 120);
        var reads = new List<string>(Reads(se.GoverningExpression));
        if (arm.Pattern is DiscardPatternSyntax && arm.WhenClause == null)
        {
            var others = se.Arms.Where(a => a != arm).Select(a => ArmCond(gov, a)).ToList();
            return new Guard { Cond = others.Count == 0 ? gov + " (default)" : J.Snip(string.Join(" || ", others), 400), Polarity = others.Count == 0, Line = LineOf(arm), Kind = "case", Reads = reads };
        }
        if (arm.WhenClause != null) reads.AddRange(Reads(arm.WhenClause.Condition));
        return new Guard { Cond = J.Snip(ArmCond(gov, arm), 400), Polarity = true, Line = LineOf(arm), Kind = "case", Reads = reads.Distinct().ToList() };
    }

    public static string ArmCond(string gov, SwitchExpressionArmSyntax a) =>
        $"{gov} is {J.Snip(a.Pattern.ToString(), 120)}" + (a.WhenClause != null ? $" && ({J.Snip(a.WhenClause.Condition.ToString(), 120)})" : "");

    public Guard ForEachGuard(CommonForEachStatementSyntax fe)
    {
        string var = fe switch
        {
            ForEachStatementSyntax f => f.Identifier.Text,
            ForEachVariableStatementSyntax v => v.Variable.ToString(),
            _ => "item",
        };
        return new Guard { Cond = $"{var} in {J.Snip(fe.Expression.ToString(), 200)}", Polarity = true, Line = LineOf(fe), Kind = "loop", Reads = Reads(fe.Expression) };
    }

    public Guard Make(ExpressionSyntax cond, bool polarity, string kind, SyntaxNode at) =>
        new() { Cond = J.Snip(cond.ToString()), Polarity = polarity, Line = LineOf(at), Kind = kind, Reads = Reads(cond) };

    public static int LineOf(SyntaxNode n) => n.GetLocation().GetLineSpan().StartLinePosition.Line + 1;

    /// <summary>Keys of data symbols (locals, params, fields, properties, events) read in an expression.</summary>
    public IReadOnlyList<string> Reads(SyntaxNode expr)
    {
        if (_readsCache.TryGetValue(expr, out var r)) return r;
        var keys = new List<string>();
        try
        {
            var op = _model.GetOperation(expr, _ct);
            if (op != null) CollectReads(op, keys, 0);
            else
            {
                foreach (var id in expr.DescendantNodesAndSelf().OfType<IdentifierNameSyntax>())
                {
                    var s = _model.GetSymbolInfo(id, _ct).Symbol;
                    if (s is ILocalSymbol or IParameterSymbol or IFieldSymbol or IPropertySymbol or IEventSymbol)
                        if (_keys.Of(s) is string k) keys.Add(k);
                }
            }
        }
        catch (OperationCanceledException) { throw; }
        catch { }
        IReadOnlyList<string> res = keys.Count == 0 ? Array.Empty<string>() : keys.Distinct().ToList();
        _readsCache[expr] = res;
        return res;
    }

    private void CollectReads(IOperation op, List<string> keys, int depth)
    {
        if (depth > 200) return;
        switch (op)
        {
            case INameOfOperation:
            case IAnonymousFunctionOperation:
                return;
            case ILocalReferenceOperation l: Add(keys, l.Local); break;
            case IParameterReferenceOperation p: Add(keys, p.Parameter); break;
            case IFieldReferenceOperation f: Add(keys, f.Field); break;
            case IPropertyReferenceOperation pr: Add(keys, pr.Property); break;
            case IEventReferenceOperation e: Add(keys, e.Event); break;
        }
        foreach (var c in op.ChildOperations) CollectReads(c, keys, depth + 1);
    }

    private void Add(List<string> keys, ISymbol s)
    {
        if (_keys.Of(s) is string k) keys.Add(k);
    }
}
