namespace Broken.App
{
    partial class Form1
    {
        private System.ComponentModel.IContainer components = null;
        private System.Windows.Forms.Button button1;
        private System.Windows.Forms.Timer timer1;

        private void InitializeComponent()
        {
            this.components = new System.ComponentModel.Container();
            this.button1 = new System.Windows.Forms.Button();
            this.timer1 = new System.Windows.Forms.Timer(this.components);
            this.button1.Click += new System.EventHandler(this.button1_Click);
            this.timer1.Tick += new System.EventHandler(this.timer1_Tick);
            this.Controls.Add(this.button1);
        }
    }
}
