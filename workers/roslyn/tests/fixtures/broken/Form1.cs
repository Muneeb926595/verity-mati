using System;
using System.Windows.Forms;

namespace Broken.App
{
    public partial class Form1 : Form
    {
        public Form1()
        {
            InitializeComponent();
        }

        private void button1_Click(object sender, EventArgs e)
        {
            MessageBox.Show("clicked");
        }

        private void timer1_Tick(object sender, EventArgs e)
        {
            Text = DateTime.Now.ToString();
        }
    }
}
