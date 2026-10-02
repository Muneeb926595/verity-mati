using System;
using System.Collections.Generic;

namespace Features
{
    public readonly record struct Angle(double Deg)
    {
        public static Angle operator +(Angle a, Angle b) => new(a.Deg + b.Deg);
        public static implicit operator double(Angle a) => a.Deg;
    }

    public abstract class Shape
    {
        public abstract double Area { get; }
        public virtual string Name => "shape";
    }

    public sealed class Square : Shape
    {
        private readonly double _side;
        public Square(double side) => _side = side;
        public override double Area => _side * _side;
        public override string Name { get; } = "square";
        public double this[int i] { get { if (i < 0) throw new ArgumentOutOfRangeException(nameof(i)); return _side * i; } }
    }

    public static class Dispatch
    {
        private static readonly Action<int>[] Handlers = { OnA, OnB };
        private static readonly Dictionary<string, Func<int, int>> Table = new() { ["x"] = Twice };
        private static Func<int, int>? _hook;

        static void OnA(int v) { }
        static void OnB(int v) { }
        static int Twice(int v) => v * 2;

        public static double Run(Shape s, Angle a, Angle b, int k)
        {
            _hook = Twice;
            _hook += Twice;
            Angle c = a + b;
            double d = c;
            double area = s.Area + s.Name.Length;
            var sq = s as Square;
            double e = sq?[k] ?? 0;
            var label = k switch { 0 => "zero", > 0 when k < 10 => "small", _ => "big" };
            Handlers[k % 2](k);
            return d + area + e + (_hook?.Invoke(k) ?? 0) + Table["x"](k) + label.Length;
        }
    }
}
