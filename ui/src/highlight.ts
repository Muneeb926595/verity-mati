// A deliberately tiny C / C# highlighter for the read-only source viewer (~80–600 lines at a time).

export type TokKind = "" | "kw" | "ty" | "str" | "com" | "num" | "pp";
export interface Tok {
  k: TokKind;
  v: string;
}

const C_KW = new Set(
  (
    "auto break case const continue default do else enum extern for goto if inline register restrict return sizeof " +
    "static struct switch typedef union volatile while true false NULL"
  ).split(" "),
);
const C_TY = new Set(
  (
    "char double float int long short signed unsigned void bool _Bool size_t ssize_t uint8_t uint16_t uint32_t uint64_t " +
    "int8_t int16_t int32_t int64_t uintptr_t intptr_t"
  ).split(" "),
);
const CS_KW = new Set(
  (
    "abstract as base break case catch checked class const continue default delegate do else enum event explicit extern " +
    "false finally fixed for foreach goto if implicit in interface internal is lock namespace new null operator out " +
    "override params private protected public readonly ref return sealed sizeof stackalloc static struct switch this " +
    "throw true try typeof unchecked unsafe using virtual volatile while async await get set init record yield nameof " +
    "when where var"
  ).split(" "),
);
const CS_TY = new Set(
  "bool byte char decimal double float int long object sbyte short string uint ulong ushort void dynamic".split(" "),
);

const NUM_RE = /^(?:0[xX][0-9a-fA-F']+|0[bB][01']+|\d[\d']*\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)[uUlLfFdDmM]*/;
const ID_RE = /^[A-Za-z_@][A-Za-z0-9_]*/;

export function highlight(lines: string[], lang: string): Tok[][] {
  const cs = lang === "csharp" || lang === "cs";
  const KW = cs ? CS_KW : C_KW;
  const TY = cs ? CS_TY : C_TY;
  let inBlock = false;
  return lines.map((line) => {
    const out: Tok[] = [];
    const push = (k: TokKind, v: string) => {
      if (!v) return;
      const last = out[out.length - 1];
      if (last && last.k === k) last.v += v;
      else out.push({ k, v });
    };
    let i = 0;
    const n = line.length;
    if (inBlock) {
      const e = line.indexOf("*/");
      if (e < 0) {
        push("com", line);
        return out;
      }
      push("com", line.slice(0, e + 2));
      i = e + 2;
      inBlock = false;
    }
    const firstNonSpace = line.search(/\S/);
    while (i < n) {
      const ch = line[i];
      const nx = line[i + 1];
      if (ch === "/" && nx === "/") {
        push("com", line.slice(i));
        break;
      }
      if (ch === "/" && nx === "*") {
        const e = line.indexOf("*/", i + 2);
        if (e < 0) {
          push("com", line.slice(i));
          inBlock = true;
          break;
        }
        push("com", line.slice(i, e + 2));
        i = e + 2;
        continue;
      }
      if (ch === '"' || ch === "'") {
        let j = i + 1;
        while (j < n && line[j] !== ch) j += line[j] === "\\" ? 2 : 1;
        push("str", line.slice(i, Math.min(n, j + 1)));
        i = j + 1;
        continue;
      }
      if (ch === "#" && i === firstNonSpace) {
        const m = /^#\s*[A-Za-z]+/.exec(line.slice(i));
        if (m) {
          push("pp", m[0]);
          i += m[0].length;
          continue;
        }
      }
      if (/[0-9]/.test(ch) || (ch === "." && /[0-9]/.test(nx || ""))) {
        const prev = line[i - 1];
        if (!prev || !/[A-Za-z0-9_]/.test(prev)) {
          const m = NUM_RE.exec(line.slice(i));
          if (m) {
            push("num", m[0]);
            i += m[0].length;
            continue;
          }
        }
      }
      if (/[A-Za-z_@]/.test(ch)) {
        const m = ID_RE.exec(line.slice(i));
        if (m) {
          const w = m[0];
          push(KW.has(w) ? "kw" : TY.has(w) ? "ty" : "", w);
          i += w.length;
          continue;
        }
      }
      push("", ch);
      i++;
    }
    return out;
  });
}
