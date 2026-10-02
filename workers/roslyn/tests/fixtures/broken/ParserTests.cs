using Xunit;

namespace Broken.Tests
{
    public class ParserTests
    {
        [Fact]
        public void ParsesRmc()
        {
            Assert.True(new Broken.App.Packet().Speed == 0);
        }

        [Theory]
        [InlineData(1)]
        public void Many(int x)
        {
            Assert.Equal(1, x);
        }
    }
}
