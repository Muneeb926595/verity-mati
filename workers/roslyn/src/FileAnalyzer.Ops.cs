using System.Collections.Immutable;
using System.Runtime.CompilerServices;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Operations;

namespace Verity.Roslyn;

/// <summary>IOperation walk of executable bodies: occurrences, calls, value flow, delegates, events, I/O.</summary>
internal sealed partial class FileAnalyzer
{
    private readonly Stack<string> _fn = new();

    private string? CurFn => _fn.Count > 0 ? _fn.Peek() : null;

    private readonly record struct Src(string Key, string? Kind, string? Api);

    /// <summary>Walks the operation tree of a body node with <paramref name="fnKey"/> as the enclosing function.</summary>
    public void WalkOperations(SyntaxNode node, string fnKey)
    {
        IOperation? op;
        try { op = M.GetOperation(node, Ct); }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex) { Internal("GetOperation", ex); return; }
        if (op == null) return;
        int depth = _fn.Count;
        _fn.Push(fnKey);
        try { Visit(op, 0); }
        catch (OperationCanceledException) { throw; }
        catch (InsufficientExecutionStackException ex) { Internal("operation walk (nesting too deep)", ex); }
        catch (Exception ex) { Internal("operation walk", ex); }
        finally { while (_fn.Count > depth) _fn.Pop(); }
    }

    private void Visit(IOperation op, int depth)
    {
        if ((++_opCount & 1023) == 0) Ct.ThrowIfCancellationRequested();
        if (depth > 256 && (depth & 63) == 0) RuntimeHelpers.EnsureSufficientExecutionStack();
        switch (op)
        {
            case INameOfOperation:
                return;
            case IAnonymousFunctionOperation af:
            {
                Safe(() => EmitLambda(af.Symbol, af.Syntax, CurFn));
                string? k = K.Of(af.Symbol);
                if (k != null) _fn.Push(k);
                try { VisitChildren(op, depth); }
                finally { if (k != null) _fn.Pop(); }
                return;
            }
            case ILocalFunctionOperation lf:
            {
                string? k = K.Of(lf.Symbol);
                if (k != null) _fn.Push(k);
                try { VisitChildren(op, depth); }
                finally { if (k != null) _fn.Pop(); }
                return;
            }
            case IInvocationOperation inv: Safe(() => OnInvocation(inv)); break;
            case IObjectCreationOperation oc: Safe(() => OnCreation(oc)); break;
            case IEventAssignmentOperation ea: Safe(() => OnEventAssign(ea)); break;
            case IDeconstructionAssignmentOperation: break;
            case IAssignmentOperation asg: Safe(() => OnAssign(asg)); break;
            case IVariableDeclaratorOperation vd: Safe(() => OnDeclarator(vd)); break;
            case IFieldInitializerOperation fi: Safe(() => OnMemberInit(fi.InitializedFields.Cast<ISymbol>(), fi.Value, fi)); break;
            case IPropertyInitializerOperation pi: Safe(() => OnMemberInit(pi.InitializedProperties.Cast<ISymbol>(), pi.Value, pi)); break;
            case IReturnOperation ret: Safe(() => OnReturn(ret)); break;
            case ILocalReferenceOperation or IParameterReferenceOperation or IFieldReferenceOperation or IPropertyReferenceOperation or IEventReferenceOperation:
                Safe(() => OnRef(op));
                break;
            case IMethodReferenceOperation mr: Safe(() => OnMethodRef(mr)); break;
            case IForEachLoopOperation fe: Safe(() => OnForEach(fe)); break;
            case IInvalidOperation bad: Safe(() => OnInvalid(bad)); break;
            case IBinaryOperation { OperatorMethod: { } bm } bo: Safe(() => OnOperator(bm, bo, bo.LeftOperand, bo.RightOperand)); break;
            case IUnaryOperation { OperatorMethod: { } um } uo: Safe(() => OnOperator(um, uo, uo.Operand, null)); break;
            case IConversionOperation { OperatorMethod: { } cm } co: Safe(() => OnOperator(cm, co, co.Operand, null)); break;
        }
        if (op is IIncrementOrDecrementOperation { OperatorMethod: { } im } io2) Safe(() => OnOperator(im, io2, io2.Target, null));
        if (op is ICompoundAssignmentOperation { OperatorMethod: { } am } ca2) Safe(() => OnOperator(am, ca2, ca2.Target, ca2.Value));
        VisitChildren(op, depth);
    }

    private void VisitChildren(IOperation op, int depth)
    {
        foreach (var c in op.ChildOperations) Visit(c, depth + 1);
    }

    private void Safe(Action a)
    {
        try { a(); }
        catch (OperationCanceledException) { throw; }
        catch (InsufficientExecutionStackException) { throw; }
        catch (Exception ex) { Internal("operation", ex); }
    }

    // ───────────────────────────── utilities ─────────────────────────────

    private static IOperation? Unwrap(IOperation? op)
    {
        for (int i = 0; i < 32 && op != null; i++)
        {
            switch (op)
            {
                case IConversionOperation c: op = c.Operand; continue;
                case IParenthesizedOperation p: op = p.Operand; continue;
                case IDeclarationExpressionOperation d: op = d.Expression; continue;
            }
            break;
        }
        return op;
    }

    /// <summary>For <c>x?.M()</c> the instance is a placeholder; resolve it to the accessed expression.</summary>
    private static IOperation? ResolveConditionalInstance(IOperation? instance, IOperation from)
    {
        instance = Unwrap(instance);
        if (instance is not IConditionalAccessInstanceOperation) return instance;
        for (var p = from.Parent; p != null; p = p.Parent)
            if (p is IConditionalAccessOperation ca) return Unwrap(ca.Operation);
        return null;
    }

    private string? RefKey(IOperation? op, int depth = 0) => depth > 8 ? null : Unwrap(op) switch
    {
        ILocalReferenceOperation l => K.Of(l.Local),
        IParameterReferenceOperation p => K.Of(p.Parameter),
        IFieldReferenceOperation f => K.Of(f.Field),
        // Elements of arrays / indexed collections are attributed to the collection (joins with fnaddr "table").
        IPropertyReferenceOperation { Property.IsIndexer: true } ix => RefKey(ResolveConditionalInstance(ix.Instance, ix), depth + 1) ?? (P.IsSource(ix.Property) ? K.Of(ix.Property) : null),
        IPropertyReferenceOperation pr => K.Of(pr.Property),
        IArrayElementReferenceOperation ae => RefKey(ae.ArrayReference, depth + 1),
        IEventReferenceOperation e => K.Of(e.Event),
        _ => null,
    };

    private string? ReceiverKey(IOperation op)
    {
        IOperation? inst = op switch
        {
            IMemberReferenceOperation m => m.Instance,
            IInvocationOperation i => i.Instance,
            _ => null,
        };
        inst = ResolveConditionalInstance(inst, op);
        return RefKey(inst);
    }

    /// <summary>Symbol receiving a value: variables, fields, properties; element writes map to the collection.</summary>
    private string? TargetKey(IOperation? t, out bool elem)
    {
        elem = false;
        t = Unwrap(t);
        switch (t)
        {
            case ILocalReferenceOperation l: return K.Of(l.Local);
            case IParameterReferenceOperation p: return K.Of(p.Parameter);
            case IFieldReferenceOperation f: return K.Of(f.Field);
            case IEventReferenceOperation e: return K.Of(e.Event);
            case IPropertyReferenceOperation pr:
                if (pr.Property.IsIndexer)
                {
                    elem = true;
                    return RefKey(pr);
                }
                return K.Of(pr.Property);
            case IArrayElementReferenceOperation ae:
                elem = true;
                return RefKey(ae.ArrayReference);
            default:
                return null;
        }
    }

    private static string RoleOf(IOperation r)
    {
        IOperation cur = r;
        IOperation? parent = r.Parent;
        while (parent is IDeclarationExpressionOperation or ITupleOperation or IConversionOperation { IsImplicit: true })
        {
            if (parent is IConversionOperation && parent.Parent is not ITupleOperation) break;
            cur = parent;
            parent = parent.Parent;
        }
        switch (parent)
        {
            case ISimpleAssignmentOperation a when a.Target == cur: return "write";
            case IDeconstructionAssignmentOperation d when d.Target == cur: return "write";
            case ICompoundAssignmentOperation c when c.Target == cur: return "rw";
            case ICoalesceAssignmentOperation c when c.Target == cur: return "rw";
            case IIncrementOrDecrementOperation: return "rw";
            case IEventAssignmentOperation e when e.EventReference == cur: return "write";
            case IArgumentOperation arg:
                return arg.Parameter?.RefKind switch
                {
                    RefKind.Out => "write",
                    RefKind.Ref => "rw",
                    _ => "read",
                };
            case IAddressOfOperation: return "addr";
            case IInvocationOperation inv when inv.Instance == cur && inv.TargetMethod.MethodKind == MethodKind.DelegateInvoke: return "call";
            case IConditionalAccessOperation ca when ca.Operation == cur &&
                ca.WhenNotNull is IInvocationOperation { TargetMethod.MethodKind: MethodKind.DelegateInvoke }: return "call";
            case IForEachLoopOperation fe when fe.LoopControlVariable == cur: return "write";
        }
        if (r is ILocalReferenceOperation { IsDeclaration: true }) return "write";
        return "read";
    }

    /// <summary>Source position of the referenced name (member name rather than the receiver).</summary>
    private static (int line, int col) NamePos(SyntaxNode syn)
    {
        return syn switch
        {
            MemberAccessExpressionSyntax ma => Pos(ma.Name),
            MemberBindingExpressionSyntax mb => Pos(mb.Name),
            InvocationExpressionSyntax { Expression: MemberAccessExpressionSyntax ma } => Pos(ma.Name),
            InvocationExpressionSyntax { Expression: MemberBindingExpressionSyntax mb } => Pos(mb.Name),
            ObjectCreationExpressionSyntax oc => Pos(oc.Type),
            AssignmentExpressionSyntax a => NamePos(a.Left),
            _ => Pos(syn),
        };
    }

    private static List<string> ArgTexts(SyntaxNode syn, ImmutableArray<IArgumentOperation> args)
    {
        BaseArgumentListSyntax? list = syn switch
        {
            InvocationExpressionSyntax i => i.ArgumentList,
            ObjectCreationExpressionSyntax o => o.ArgumentList,
            ImplicitObjectCreationExpressionSyntax io => io.ArgumentList,
            ConstructorInitializerSyntax ci => ci.ArgumentList,
            PrimaryConstructorBaseTypeSyntax pb => pb.ArgumentList,
            ElementAccessExpressionSyntax ea => ea.ArgumentList,
            _ => null,
        };
        var res = new List<string>();
        if (list != null)
        {
            foreach (var a in list.Arguments) res.Add(J.Snip(a.ToString(), 200));
            return res;
        }
        foreach (var a in args)
            if (!a.IsImplicit && a.ArgumentKind != ArgumentKind.DefaultValue) res.Add(J.Snip(a.Syntax.ToString(), 200));
        return res;
    }

    /// <summary>
    /// Symbols a value is computed from. Source-method calls contribute <c>callee#ret</c> (return_to);
    /// external API calls are looked through, their inputs flowing with kind "api".
    /// </summary>
    private List<Src> Sources(IOperation? v)
    {
        var acc = new List<Src>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        Collect(v, acc, seen, null, 0);
        return acc;
    }

    private void Collect(IOperation? op, List<Src> acc, HashSet<string> seen, string? api, int depth)
    {
        if (op == null || depth > 200) return;
        void Add(string? key, string? kind)
        {
            if (key != null && seen.Add(key)) acc.Add(new Src(key, kind ?? (api != null ? "api" : null), api));
        }
        switch (op)
        {
            case ILocalReferenceOperation l: Add(K.Of(l.Local), null); return;
            case IParameterReferenceOperation p: Add(K.Of(p.Parameter), null); return;
            case IFieldReferenceOperation f:
                Add(K.Of(f.Field), null);
                Collect(f.Instance, acc, seen, api, depth + 1);
                return;
            case IPropertyReferenceOperation pr:
                Add(K.Of(pr.Property), null);
                Collect(pr.Instance, acc, seen, api, depth + 1);
                foreach (var a in pr.Arguments) Collect(a.Value, acc, seen, api, depth + 1);
                return;
            case IEventReferenceOperation e:
                Add(K.Of(e.Event), null);
                return;
            case IInstanceReferenceOperation:
            case IConditionalAccessInstanceOperation:
            case ILiteralOperation:
            case INameOfOperation:
            case ITypeOfOperation:
            case ISizeOfOperation:
            case IDefaultValueOperation:
            case IAnonymousFunctionOperation:
            case IDelegateCreationOperation:
            case IMethodReferenceOperation:
            case ILocalFunctionOperation:
                return;
            case IInvocationOperation inv:
            {
                var t = inv.TargetMethod;
                if (t.MethodKind == MethodKind.DelegateInvoke) return;
                if (P.IsSource(t))
                {
                    if (K.Of(t) is string k) Add(k + "#ret", "return_to");
                    return;
                }
                string a2 = api ?? K.Of(t) ?? t.Name;
                Collect(inv.Instance, acc, seen, a2, depth + 1);
                foreach (var a in inv.Arguments)
                    if (a.Parameter?.RefKind != RefKind.Out) Collect(a.Value, acc, seen, a2, depth + 1);
                return;
            }
            case IBinaryOperation { OperatorMethod: { } bm } when P.IsSource(bm):
            case IUnaryOperation { OperatorMethod: { } um } when P.IsSource(um):
            case IConversionOperation { OperatorMethod: { } cm } when P.IsSource(cm):
            {
                var om = op switch { IBinaryOperation b => b.OperatorMethod, IUnaryOperation u => u.OperatorMethod, _ => ((IConversionOperation)op).OperatorMethod };
                if (K.Of(om) is string ok) Add(ok + "#ret", "return_to");
                return;
            }
            case IObjectCreationOperation oc:
            {
                if (oc.Constructor != null && P.IsSource(oc.Constructor))
                {
                    if (K.Of(oc.Constructor) is string k) Add(k + "#ret", "return_to");
                    return;
                }
                string a2 = api ?? K.Of(oc.Constructor) ?? "new";
                foreach (var a in oc.Arguments) Collect(a.Value, acc, seen, a2, depth + 1);
                return;
            }
        }
        foreach (var c in op.ChildOperations) Collect(c, acc, seen, api, depth + 1);
    }

    /// <summary>Methods/lambdas whose address is taken in a value; <c>table</c> when stored in an array/collection.</summary>
    private List<(string Fn, bool Table)> DelegatesIn(IOperation? v)
    {
        var res = new List<(string, bool)>();
        void Walk(IOperation? o, bool table, int depth)
        {
            o = Unwrap(o);
            if (o == null || depth > 16) return;
            switch (o)
            {
                case IDelegateCreationOperation dc: Walk(dc.Target, table, depth + 1); break;
                case IMethodReferenceOperation mr: if (K.Of(mr.Method) is string k1) res.Add((k1, table)); break;
                case IAnonymousFunctionOperation af: if (K.Of(af.Symbol) is string k2) res.Add((k2, table)); break;
                case IArrayCreationOperation ac when ac.Initializer != null:
                    foreach (var e in ac.Initializer.ElementValues) Walk(e, true, depth + 1);
                    break;
                case IArrayInitializerOperation ai:
                    foreach (var e in ai.ElementValues) Walk(e, true, depth + 1);
                    break;
                case IObjectCreationOperation oc when oc.Initializer != null:
                    foreach (var init in oc.Initializer.Initializers)
                    {
                        if (init is IInvocationOperation add) foreach (var a in add.Arguments) Walk(a.Value, true, depth + 1);
                        else if (init is ISimpleAssignmentOperation sa) Walk(sa.Value, true, depth + 1);
                    }
                    break;
                case ICollectionExpressionOperation ce:
                    foreach (var e in ce.Elements) Walk(e, true, depth + 1);
                    break;
                case IConditionalOperation c:
                    Walk(c.WhenTrue, table, depth + 1);
                    Walk(c.WhenFalse, table, depth + 1);
                    break;
                case ICoalesceOperation co:
                    Walk(co.Value, table, depth + 1);
                    Walk(co.WhenNull, table, depth + 1);
                    break;
            }
        }
        Walk(v, false, 0);
        return res;
    }

    // ───────────────────────────── emitters ─────────────────────────────

    private void EmitCall(string? to, string kind, string res, SyntaxNode syn, List<string> args, List<Guard> guards,
                          string? via = null, string? recv = null, bool ext = false, bool isImplicit = false, string? name = null)
    {
        string? from = CurFn;
        var (line, col) = NamePos(syn);
        B.Record("call", w =>
        {
            w.Str("from", from);
            w.Str("to", to);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.WriteNumber("col", col);
            w.WriteString("kind", kind);
            w.WriteString("res", res);
            if (via != null) w.WriteString("via", via);
            if (recv != null) w.WriteString("recv", recv);
            w.Arr("args", args);
            WriteGuards(w, guards);
            if (ext) w.WriteBoolean("ext", true);
            if (isImplicit) w.WriteBoolean("implicit", true);
            if (name != null) w.Str("name", name);
        });
    }

    private void EmitOcc(string key, SyntaxNode syn, string role, string? via, List<Guard>? guards)
    {
        var (line, col) = NamePos(syn);
        string? fn = CurFn;
        B.Record("occ", w =>
        {
            w.WriteString("sym", key);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.WriteNumber("col", col);
            w.WriteString("role", role);
            w.Str("in", fn);
            if (via != null) w.WriteString("via", via);
            if (guards != null) WriteGuards(w, guards);
        });
    }

    private void EmitOccAt(string key, (int line, int col) pos, string role, List<Guard>? guards)
    {
        string? fn = CurFn;
        B.Record("occ", w =>
        {
            w.WriteString("sym", key);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", pos.line);
            w.WriteNumber("col", pos.col);
            w.WriteString("role", role);
            w.Str("in", fn);
            if (guards != null) WriteGuards(w, guards);
        });
    }

    private void EmitFlow(string src, string dst, string kind, SyntaxNode at, string? api = null, int? pos = null, bool elem = false)
    {
        if (src == dst) return;
        var (line, _) = Pos(at);
        string? fn = CurFn;
        string expr = J.Snip(at.ToString(), 200);
        B.Record("flow", w =>
        {
            w.WriteString("src", src);
            w.WriteString("dst", dst);
            w.WriteString("kind", kind);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.Str("fn", fn);
            w.Str("expr", expr);
            if (api != null) w.WriteString("api", api);
            if (pos != null) w.WriteNumber("pos", pos.Value);
            if (elem) w.WriteBoolean("elem", true);
        });
    }

    private void EmitFnAddr(string fn, string dst, string kind, SyntaxNode at)
    {
        var (line, _) = Pos(at);
        string? inFn = CurFn;
        B.Record("fnaddr", w =>
        {
            w.WriteString("fn", fn);
            w.WriteString("dst", dst);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.WriteString("kind", kind);
            w.Str("in", inFn);
        });
    }

    private void EmitIo(string? sym, string dir, string api, SyntaxNode at, string res, string? handler = null)
    {
        var (line, _) = Pos(at);
        string? fn = CurFn;
        B.Record("io", w =>
        {
            w.Str("sym", sym);
            w.WriteString("dir", dir);
            w.WriteString("api", api);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.Str("fn", fn);
            w.WriteString("res", res);
            if (handler != null) w.WriteString("handler", handler);
        });
    }

    // ───────────────────────────── handlers ─────────────────────────────

    private void OnRef(IOperation op)
    {
        if (op.IsImplicit) return;
        ISymbol? sym = op switch
        {
            ILocalReferenceOperation l => l.Local,
            IParameterReferenceOperation p => p.Parameter,
            IFieldReferenceOperation f => f.Field,
            IPropertyReferenceOperation pr => pr.Property,
            IEventReferenceOperation e => e.Event,
            _ => null,
        };
        string? key = K.Of(sym);
        if (key == null) return;
        string role = RoleOf(op);
        if (op is ILocalReferenceOperation { IsDeclaration: true } && role == "read") return;
        List<Guard>? guards = role is "write" or "rw" ? G.For(op.Syntax) : null;
        EmitOcc(key, op.Syntax, role, ReceiverKey(op), guards);
        if (op is IPropertyReferenceOperation prop) AccessorCalls(prop, role);
    }

    /// <summary>Property reads/writes execute accessor bodies: emit call edges for non-auto or dispatching accessors.</summary>
    private void AccessorCalls(IPropertyReferenceOperation pr, string role)
    {
        var prop = pr.Property.OriginalDefinition;
        if (!P.IsSource(prop)) return;
        bool iface = prop.ContainingType?.TypeKind == TypeKind.Interface && !prop.IsStatic;
        bool dispatch = iface || prop.IsAbstract || prop.IsVirtual || prop.IsOverride;
        var accessors = new List<IMethodSymbol>(2);
        if (role is "read" or "rw" && prop.GetMethod != null) accessors.Add(prop.GetMethod);
        if (role is "write" or "rw" && prop.SetMethod != null) accessors.Add(prop.SetMethod);
        foreach (var acc in accessors)
        {
            if (!dispatch && !P.HasExplicitBody(acc)) continue;
            bool isBase = pr.Instance is IInstanceReferenceOperation && pr.Syntax is MemberAccessExpressionSyntax { Expression: BaseExpressionSyntax };
            string kind = isBase ? "direct" : iface ? "interface" : dispatch ? "virtual" : "direct";
            string res = kind == "direct" ? "exact" : "cha";
            var args = new List<string>();
            if (acc.MethodKind == MethodKind.PropertySet && pr.Parent is ISimpleAssignmentOperation sa) args.Add(J.Snip(sa.Value.Syntax.ToString(), 200));
            EmitCall(K.Of(acc), kind, res, pr.Syntax, args, G.For(pr.Syntax), via: K.Of(prop), recv: ReceiverKey(pr));
        }
    }

    private void OnMethodRef(IMethodReferenceOperation mr)
    {
        if (K.Of(mr.Method) is string k) EmitOcc(k, mr.Syntax, "addr", ReceiverKey(mr), null);
    }

    private void OnInvocation(IInvocationOperation inv)
    {
        var target = inv.TargetMethod;
        bool source = P.IsSource(target);
        if (inv.IsImplicit && !source) return; // implicit object..ctor, collection-initializer Add, etc.
        var syn = inv.Syntax;
        var args = ArgTexts(syn, inv.Arguments);
        var guards = G.For(syn);

        if (target.MethodKind == MethodKind.DelegateInvoke)
        {
            var holder = ResolveConditionalInstance(inv.Instance, inv);
            string? via = RefKey(holder);
            bool isEvent = Unwrap(holder) is IEventReferenceOperation;
            EmitCall(null, isEvent ? "event" : "delegate", "flow", syn, args, guards, via: via,
                     name: holder != null ? J.Snip(holder.Syntax.ToString(), 120) : null);
            if (via != null) ArgFlows(inv.Arguments, via, syn);
            return;
        }

        string? to = K.Of(target);
        string kind, res;
        if (target.MethodKind == MethodKind.Constructor) { kind = "ctor"; res = "exact"; }
        else if (!target.IsStatic && target.ContainingType?.TypeKind == TypeKind.Interface) { kind = "interface"; res = "cha"; }
        else if (inv.IsVirtual) { kind = "virtual"; res = "cha"; }
        else { kind = "direct"; res = "exact"; }
        EmitCall(to, kind, res, syn, args, guards, recv: ReceiverKey(inv), ext: !source, isImplicit: inv.IsImplicit);

        if (to == null) return;
        if (source)
        {
            EmitOcc(to, syn, "call", ReceiverKey(inv), null);
            ArgFlows(inv.Arguments, to, syn);
        }
        else
        {
            ExternalOutFlows(inv.Arguments, inv.Instance, to, syn);
        }
        FnAddrArgs(inv.Arguments, to, syn);
        StartEntries(target, inv.Arguments);

        var io = ApiTables.MatchIo(target, inv.Instance?.Type);
        if (io != null) EmitIo(to, io.Value.Dir, io.Value.Api, syn, "apipack");
    }

    /// <summary>User-defined operators and conversions declared in source are calls too.</summary>
    private void OnOperator(IMethodSymbol m, IOperation op, IOperation? a, IOperation? b)
    {
        if (!P.IsSource(m) || K.Of(m) is not string to) return;
        var args = new List<string>();
        if (a != null) args.Add(J.Snip(a.Syntax.ToString(), 200));
        if (b != null) args.Add(J.Snip(b.Syntax.ToString(), 200));
        EmitCall(to, "direct", "exact", op.Syntax, args, G.For(op.Syntax), isImplicit: op.IsImplicit, name: m.Name);
        EmitOcc(to, op.Syntax, "call", null, null);
        int i = 0;
        foreach (var operand in new[] { a, b })
        {
            if (operand == null || i >= m.Parameters.Length) { i++; continue; }
            var prm = m.Parameters[i++];
            foreach (var src in Sources(operand))
                EmitFlow(src.Key, to + "#param:" + prm.Name, "arg", op.Syntax, api: src.Api, pos: prm.Ordinal);
        }
    }

    private void OnCreation(IObjectCreationOperation oc)
    {
        var ctor = oc.Constructor;
        bool source = ctor != null && P.IsSource(ctor);
        var syn = oc.Syntax;
        string? typeKey = oc.Type != null ? K.Of(oc.Type) : null;
        if (typeKey != null)
        {
            var (line, _) = Pos(syn);
            string? fn = CurFn;
            B.Record("instantiate", w =>
            {
                w.WriteString("type", typeKey);
                w.WriteString("file", FilePath);
                w.WriteNumber("line", line);
                w.Str("fn", fn);
                if (ctor != null && K.Of(ctor) is string ck) w.WriteString("ctor", ck);
            });
        }
        if (oc.IsImplicit && !source) return;
        string? to = ctor != null ? K.Of(ctor) : null;
        EmitCall(to, "ctor", ctor != null ? "exact" : "unresolved", syn, ArgTexts(syn, oc.Arguments), G.For(syn), ext: ctor != null && !source,
                 name: ctor == null && oc.Type != null ? Fmt.Display(oc.Type, Fmt.Type) : null);
        if (to == null || ctor == null) return;
        if (source)
        {
            EmitOcc(to, syn, "call", null, null);
            ArgFlows(oc.Arguments, to, syn);
        }
        else ExternalOutFlows(oc.Arguments, null, to, syn);
        FnAddrArgs(oc.Arguments, to, syn);
        StartEntries(ctor, oc.Arguments);
    }

    /// <summary>argument → parameter bindings (kind arg); ref/out parameters flow back (kind return_to).</summary>
    private void ArgFlows(ImmutableArray<IArgumentOperation> args, string calleeKey, SyntaxNode site)
    {
        foreach (var a in args)
        {
            if (a.ArgumentKind == ArgumentKind.DefaultValue) continue;
            var p = a.Parameter;
            if (p == null) continue;
            string dst = calleeKey + "#param:" + p.Name;
            SyntaxNode at = a.IsImplicit ? site : a.Syntax;
            if (p.RefKind is RefKind.Out or RefKind.Ref)
            {
                if (TargetKey(a.Value, out _) is string back) EmitFlow(dst, back, "return_to", at, pos: p.Ordinal);
                if (p.RefKind == RefKind.Out) continue;
            }
            foreach (var s in Sources(a.Value))
                EmitFlow(s.Key, dst, "arg", at, api: s.Api, pos: p.Ordinal);
        }
    }

    /// <summary>For external APIs with out/ref parameters (TryParse…): inputs flow into the out variables (kind api).</summary>
    private void ExternalOutFlows(ImmutableArray<IArgumentOperation> args, IOperation? instance, string calleeKey, SyntaxNode site)
    {
        List<string>? outs = null;
        foreach (var a in args)
            if (a.Parameter?.RefKind is RefKind.Out or RefKind.Ref && TargetKey(a.Value, out _) is string t)
                (outs ??= new List<string>()).Add(t);
        if (outs == null) return;
        var srcs = new List<Src>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        Collect(instance, srcs, seen, calleeKey, 0);
        foreach (var a in args)
            if (a.Parameter?.RefKind is not (RefKind.Out))
                Collect(a.Value, srcs, seen, calleeKey, 0);
        foreach (var o in outs)
            foreach (var s in srcs)
                if (s.Key != o) EmitFlow(s.Key, o, "api", site, api: calleeKey);
    }

    private void FnAddrArgs(ImmutableArray<IArgumentOperation> args, string calleeKey, SyntaxNode site)
    {
        foreach (var a in args)
        {
            if (a.Parameter == null) continue;
            foreach (var (fn, table) in DelegatesIn(a.Value))
                EmitFnAddr(fn, calleeKey + "#param:" + a.Parameter.Name, table ? "table" : "arg", a.IsImplicit ? site : a.Syntax);
        }
    }

    private void StartEntries(IMethodSymbol target, ImmutableArray<IArgumentOperation> args)
    {
        string? kind = ApiTables.StartKind(target);
        if (kind == null) return;
        string api = ApiTables.FullName(target.ContainingType) + "." + (target.MethodKind == MethodKind.Constructor ? ".ctor" : target.Name);
        foreach (var a in args)
            foreach (var (fn, _) in DelegatesIn(a.Value))
                Entry(fn, kind, api, "pattern");
    }

    private void OnEventAssign(IEventAssignmentOperation ea)
    {
        var evRef = Unwrap(ea.EventReference) as IEventReferenceOperation;
        IEventSymbol? ev = evRef?.Event;
        string evKey = ev != null ? K.Of(ev) ?? "?" : "?:" + J.Snip(ea.EventReference?.Syntax.ToString() ?? "", 200);
        var fns = DelegatesIn(ea.HandlerValue);
        string? handler = fns.Count > 0 ? fns[0].Fn : null;
        string? handlerVia = handler == null ? TargetKey(ea.HandlerValue, out _) : null;
        string op = ea.Adds ? "+=" : "-=";
        var (line, _) = Pos(ea.Syntax);
        string? fn = CurFn;
        string? recv = evRef != null ? ReceiverKey(evRef) : null;
        B.Record("subscribe", w =>
        {
            w.WriteString("event", evKey);
            w.Str("handler", handler);
            w.WriteString("file", FilePath);
            w.WriteNumber("line", line);
            w.Str("fn", fn);
            w.WriteString("op", op);
            w.WriteString("res", "exact");
            if (recv != null) w.WriteString("recv", recv);
            if (handlerVia != null) w.WriteString("handler_via", handlerVia);
        });
        if (ea.Adds && handler != null)
            Entry(handler, ApiTables.EventEntryKind(ev), IsDesigner ? evKey + " (designer)" : evKey, "exact");
        if (ev != null && ea.Adds && ApiTables.MatchIoEvent(ev) is { } io)
            EmitIo(evKey, io.Dir, io.Api, ea.Syntax, "apipack", handler);
    }

    private void OnAssign(IAssignmentOperation a)
    {
        string baseKind = a is ISimpleAssignmentOperation ? "assign" : "compound";
        string? dst = TargetKey(a.Target, out bool elem);
        if (dst == null) return;
        foreach (var s in Sources(a.Value))
            EmitFlow(s.Key, dst, s.Kind ?? baseKind, a.Syntax, api: s.Api, elem: elem);
        foreach (var (fn, table) in DelegatesIn(a.Value))
            EmitFnAddr(fn, dst, table ? "table" : "assign", a.Syntax);
    }

    private void OnDeclarator(IVariableDeclaratorOperation vd)
    {
        var init = vd.Initializer?.Value ?? (vd.Parent as IVariableDeclarationOperation)?.Initializer?.Value;
        if (init == null) return;
        string? dst = K.Of(vd.Symbol);
        if (dst == null) return;
        var ident = vd.Syntax is VariableDeclaratorSyntax v ? Pos(v.Identifier) : Pos(vd.Syntax);
        EmitOccAt(dst, ident, "write", G.For(vd.Syntax));
        foreach (var s in Sources(init))
            EmitFlow(s.Key, dst, s.Kind ?? "init", vd.Syntax, api: s.Api);
        foreach (var (fn, table) in DelegatesIn(init))
            EmitFnAddr(fn, dst, table ? "table" : "init", vd.Syntax);
    }

    private void OnMemberInit(IEnumerable<ISymbol> members, IOperation value, IOperation at)
    {
        foreach (var m in members)
        {
            string? dst = K.Of(m);
            if (dst == null) continue;
            var decl = m.DeclaringSyntaxReferences.FirstOrDefault(r => r.SyntaxTree == Meta.Tree)?.GetSyntax(Ct);
            var pos = decl switch
            {
                VariableDeclaratorSyntax v => Pos(v.Identifier),
                PropertyDeclarationSyntax p => Pos(p.Identifier),
                _ => Pos(at.Syntax),
            };
            EmitOccAt(dst, pos, "write", null);
            foreach (var s in Sources(value))
                EmitFlow(s.Key, dst, s.Kind ?? "init", at.Syntax.Parent ?? at.Syntax, api: s.Api);
            foreach (var (fn, table) in DelegatesIn(value))
                EmitFnAddr(fn, dst, table ? "table" : "init", at.Syntax);
        }
    }

    private void OnReturn(IReturnOperation ret)
    {
        if (ret.ReturnedValue == null || CurFn is not string fn) return;
        string dst = fn + "#ret";
        foreach (var s in Sources(ret.ReturnedValue))
            EmitFlow(s.Key, dst, "ret", ret.Syntax, api: s.Api);
        foreach (var (f, table) in DelegatesIn(ret.ReturnedValue))
            EmitFnAddr(f, dst, table ? "table" : "assign", ret.Syntax);
    }

    private void OnForEach(IForEachLoopOperation fe)
    {
        foreach (var l in fe.Locals)
        {
            if (K.Of(l) is not string dst) continue;
            if (fe.Syntax is ForEachStatementSyntax fs) EmitOccAt(dst, Pos(fs.Identifier), "write", G.For(fs));
            foreach (var s in Sources(fe.Collection))
                EmitFlow(s.Key, dst, s.Kind ?? "init", fe.Syntax is CommonForEachStatementSyntax cf ? cf.Expression : fe.Syntax, api: s.Api);
        }
    }

    private void OnInvalid(IInvalidOperation bad)
    {
        switch (bad.Syntax)
        {
            case InvocationExpressionSyntax ies:
            {
                foreach (var c in bad.ChildOperations)
                    if (c is IInvocationOperation ci && ci.Syntax == ies) return;
                var si = M.GetSymbolInfo(ies, Ct);
                var cand = si.Symbol as IMethodSymbol ?? (si.CandidateSymbols.Length == 1 ? si.CandidateSymbols[0] as IMethodSymbol : null);
                string? to = cand != null ? K.Of(cand) : null;
                var args = ies.ArgumentList.Arguments.Select(a => J.Snip(a.ToString(), 200)).ToList();
                EmitCall(to, "direct", to != null ? "signature" : "unresolved", ies, args, G.For(ies), recv: null,
                         ext: cand != null && !P.IsSource(cand), name: J.Snip(ies.Expression.ToString(), 160));
                if (ies.Expression is MemberAccessExpressionSyntax ma)
                {
                    var rt = M.GetTypeInfo(ma.Expression, Ct).Type;
                    string typeName = rt != null && rt.Name.Length > 0 ? rt.Name : ma.Expression.ToString().Split('.').Last();
                    string member = ma.Name.Identifier.ValueText;
                    var io = cand != null ? ApiTables.MatchIo(cand, rt) : null;
                    if (io != null) EmitIo(to, io.Value.Dir, io.Value.Api, ies, "apipack");
                    else if (ApiTables.MatchIoByName(typeName, member) is { } h) EmitIo("?:" + h.Api, h.Dir, h.Api, ies, "heuristic");
                }
                break;
            }
            case BaseObjectCreationExpressionSyntax oce:
            {
                foreach (var c in bad.ChildOperations)
                    if (c is IObjectCreationOperation co && co.Syntax == oce) return;
                var t = M.GetTypeInfo(oce, Ct).Type;
                string? typeKey = t != null ? K.Of(t) : null;
                var (line, _) = Pos(oce);
                string? fn = CurFn;
                if (typeKey != null)
                    B.Record("instantiate", w =>
                    {
                        w.WriteString("type", typeKey);
                        w.WriteString("file", FilePath);
                        w.WriteNumber("line", line);
                        w.Str("fn", fn);
                        w.WriteString("res", "unresolved");
                    });
                var args = oce.ArgumentList?.Arguments.Select(a => J.Snip(a.ToString(), 200)).ToList() ?? new List<string>();
                EmitCall(null, "ctor", "unresolved", oce, args, G.For(oce), name: t != null ? Fmt.Display(t, Fmt.Type) : J.Snip(oce.ToString(), 120));
                break;
            }
        }
    }
}
