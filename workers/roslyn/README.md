# VerityRoslyn: C# fact extractor

A .NET 10 console worker (Roslyn / Microsoft.CodeAnalysis.CSharp 5.9.0). It analyses a C# repository **without building or restoring it**
and writes facts as JSON Lines that follow `docs/FACT_SCHEMA.md`. It runs fully offline: the reference assemblies
(Basic.Reference.Assemblies Net80, Net80Windows, Net100, Net472, NetStandard20) are embedded in `bin/`, and there is no restore or telemetry at runtime.

## Build / run
```sh
workers/roslyn/build.sh     # locked restore (cache: tools/nuget-packages) + publish to workers/roslyn/bin; offline once cached
tools/dotnet/dotnet workers/roslyn/bin/VerityRoslyn.dll <repo_root> [--out facts.jsonl] [--symbols "DEBUG;TRACE"] [--progress]
    [--timeout SEC] [--file-timeout SEC (120)] [--threads N (≤8)] [--tfm net8.0] [--max-file-mb 16] [--no-type-occ] [--max-diags 200]
python3 workers/roslyn/tests/verify.py [--extra-repo /path/to/any/csharp/repo]
```
Output goes to stdout unless you pass `--out`. With `--progress`, stderr gets `PROGRESS <done>/<total> <file>` lines. Exit codes: 0 ok, 1 bad arguments, 2 timed out (the output is partial but still ends with `done`), 3 fatal.
The output order is deterministic (projects in dependency order, files sorted, each file's records together).

## How it works
- **Discovery**: finds `*.cs` files and skips `bin obj .git node_modules` and hidden directories. For each `*.csproj` it reads TargetFramework(s)/TargetFrameworkVersion, `Directory.Build.props`, DefineConstants, LangVersion, Nullable, ImplicitUsings, `<Using>`, UseWindowsForms/UseWPF, PackageReference/Reference/packages.config (recorded but not restored), ProjectReference, and old-style `<Compile>` lists. Each file belongs to its nearest project. Files outside any project form a `(loose)` group whose framework is inferred: `System.Windows.Forms`, WPF namespaces or `.xaml` files mean Windows desktop.
- **One `CSharpCompilation` per project**. Projects are built in ProjectReference order, and references use `CompilationReference`, so calls across projects resolve to source. The worker picks the newest .NET TFM, otherwise .NET Framework, otherwise .NET Standard. Pack mapping: net9+ → Net100, net5–8/netcoreapp → Net80, `-windows`/WinForms/WPF → core pack + Net80Windows, net4x → Net472, netstandard → NetStandard20. It also adds SDK implicit usings and TFM preprocessor symbols (`NET8_0_OR_GREATER`, …). `--symbols` replaces the default `DEBUG;TRACE`.
- **Per file** (in parallel on 256 MB-stack threads): diagnostics → fidelity; a syntax walk for declarations, inheritance, branches, type references, entries and config keys; and an IOperation walk of every body (methods, constructors, accessors, expression bodies, field/property initialisers, top-level statements, primary-constructor base calls, lambdas, local functions).

## Record notes (additions to the schema; the ingester ignores unknown fields)
- Keys are DocumentationCommentIds **without** the `~ReturnType` suffix that Roslyn 5.x now appends (it is kept only for conversion operators). Other forms: `…#param:x`, `…#local:x@line`, `…#lambda@line:col` (also LINQ query lambdas), `…#fn:name` (local function), `…#ret`. Top-level statements: `M:Program.<Main>$(System.String[])`. Anonymous-type members get no key. Parameters of indexer accessors use the indexer's key.
- `project` record (one per compilation): tfm, refs pack, packages, defines. `done` record: `projects, extractor_errors, skipped, timed_out, records{type: count}`.
- `file.fidelity`: F2 = no compile errors in the file, F3 = errors (for example missing packages; `stubs` lists the unresolved names), F4 = analysis timed out, so facts are partial.
- `call`: `recv` = receiver symbol; `ext: true` = target is outside the source; `implicit`; `name` for unresolved calls. Delegate and event invocations have `to: null`, `res: "flow"` and `via` = the holder (field, local, param, event, or the collection for `table[i](…)`); resolve them through `fnaddr` and `subscribe`. Reading or writing a property adds a call to its accessor (`via` = property) when the accessor has a body or dispatches (virtual/abstract/interface). User-defined operators and conversions are calls too. `res: "signature"` means overload resolution failed on a single candidate.
- `flow`: `pos` (argument position), `api` (an external method the value passes through; kind `api` when it reaches a variable), `elem` (element or indexer write). Object creation yields `<ctor>#ret`. For `out`/`ref` parameters the value flows back with `return_to`.
- `branch`: `exits` (then/else/both) and `foreach`. `case` records carry `polarity`/`default`. `early_exit` = an `if` without `else` whose then-branch never falls through (decided by Roslyn control-flow analysis).
- `guards` are ordered outermost to innermost and capped at 32 (the innermost are kept). Kinds: if, else, early_exit (preceding sibling `if` that returns/throws/breaks/continues, including else-if chains), case (switch sections and switch-expression arms; `default` = the other labels with polarity false), loop, ternary, plus `&&`/`||`/`??`/`?.` short-circuits (kind if).
- `subscribe.res`: `exact`, or `heuristic` when the event's type is unresolved (the event key is then `?:<lhs text>`). `entry` kinds: main, event, timer/thread (a delegate passed to Timer, Thread, Task.Run, ThreadPool, Parallel; Timer.Elapsed/Tick; BackgroundWorker.DoWork), handler (CancelKeyPress, ASP.NET attributes), test (Fact/Theory/Test/TestMethod…). Subscriptions in `*.Designer.cs` get `(designer)` in `detail`.
- `io` uses the table in `src/ApiTables.cs` (res `apipack`, and `heuristic` for unresolved receivers such as `SerialPort` without its package). `config_key` handles `cfg["k"]`, `GetValue/GetSection/GetConnectionString("k")`, chained `GetSection("a")["b"]` → `a:b`, and `ConfigurationManager.AppSettings`.

## Known limitations
- No restore and no source generators: package types become error types (F3, unresolved calls, name-based I/O). XAML-generated `InitializeComponent`, Razor, and generator-produced members are missing, and XAML `Click="…"` handlers are not read.
- One TFM per project, so `#if` branches that are only active for the other target frameworks are not analysed.
- Value flow is intraprocedural and field-based (`P:Type.Prop`, not per instance), with no alias analysis. External calls are modelled as passing all their inputs through, and deconstruction assignments get write occurrences but no flows.
- Guards follow control structure only: there is no path sensitivity across loops, `goto` or exceptions, catch/finally are not guards, and helper methods that always throw are not recognised as exits.
- thread/timer entries and `fnaddr` need a method group or lambda at the site. Delegates passed indirectly are found only through flow records.
- Memory scales with the repository because compilations of referenced projects are kept (Newtonsoft.Json with 197k lines peaks at 560 MB).
