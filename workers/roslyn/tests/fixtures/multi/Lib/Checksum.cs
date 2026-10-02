namespace Multi.Lib
{
    public static class Checksum
    {
        /// <summary>XOR checksum over a span of characters.</summary>
        public static byte Xor(string s)
        {
            byte sum = 0;
#if LIB_FEATURE
            foreach (char c in s) sum ^= (byte)c;
#else
            sum = 0xFF;
#endif
            return sum;
        }
    }
}
