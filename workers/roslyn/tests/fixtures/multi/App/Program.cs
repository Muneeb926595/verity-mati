using Multi.Lib;

var line = Console.ReadLine() ?? "";
byte cs = Checksum.Xor(line);
if (cs == 0)
    return;
Report(cs);

static void Report(byte value) => Console.WriteLine($"checksum={value:X2}");
