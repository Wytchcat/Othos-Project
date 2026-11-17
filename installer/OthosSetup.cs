// OthosSetup.cs
// Version 8 – Windows Forms installer for Othos (C# 5, .NET Framework)

using System;
using System.IO;
using System.Text;
using System.Drawing;
using System.Diagnostics;
using System.Windows.Forms;
using System.Text.RegularExpressions;
using System.Collections.Generic;
using System.Threading;
using System.Runtime.InteropServices;

namespace OthosInstaller
{
    // ------------------------------------------------------------------------
    // Colour definitions for the Othos theme
    // ------------------------------------------------------------------------
    internal static class OthosColors
    {
        public static readonly Color Background = Color.FromArgb(2, 7, 5);
        public static readonly Color Panel = Color.FromArgb(5, 12, 8);
        public static readonly Color Text = Color.FromArgb(200, 230, 210);
        public static readonly Color Heading = Color.FromArgb(61, 220, 132);
        public static readonly Color Success = Color.FromArgb(61, 220, 132);
        public static readonly Color Warning = Color.FromArgb(231, 195, 104);
        public static readonly Color ButtonBack = Color.FromArgb(14, 16, 25);
        public static readonly Color ButtonBorder = Color.FromArgb(64, 53, 105);
        public static readonly Color ButtonHover = Color.FromArgb(25, 24, 43);
    }

    // ------------------------------------------------------------------------
    // Themed message box – used for every UI message
    // ------------------------------------------------------------------------
    internal static class ThemedBox
    {
        public static DialogResult Show(IWin32Window owner, string text, string title, bool yesNo)
        {
            Form f = new Form();
            f.HandleCreated += delegate(object s, EventArgs ev) { try { int use = 1; OthosSetup.DwmSetWindowAttribute(f.Handle, 20, ref use, 4); } catch { } };
            f.Text = title;
            f.FormBorderStyle = FormBorderStyle.Sizable;
f.MinimumSize = new Size(300, 200);
            f.StartPosition = FormStartPosition.CenterParent;
            f.BackColor = OthosColors.Background;
            f.ForeColor = OthosColors.Text;
            f.Font = new Font("Segoe UI", 10, FontStyle.Regular);
            
            
            f.MaximizeBox = false;
            f.MinimizeBox = false;

            Label lbl = new Label();
            lbl.Text = text;
            lbl.AutoSize = true;
            lbl.MaximumSize = new Size(460, 0);
            lbl.ForeColor = OthosColors.Text;
            lbl.BackColor = OthosColors.Background;
            lbl.Location = new Point(20, 20);
            f.Controls.Add(lbl);

            Button btnOk = new Button();
            btnOk.Text = yesNo ? "Yes" : "OK";
            btnOk.BackColor = OthosColors.ButtonBack;
            btnOk.ForeColor = OthosColors.Text;
            btnOk.FlatStyle = FlatStyle.Flat;
            btnOk.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            btnOk.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnOk.Size = new Size(100, 30);
            btnOk.Location = new Point(20, lbl.Bottom + 20);
            btnOk.DialogResult = yesNo ? DialogResult.Yes : DialogResult.OK;
            f.Controls.Add(btnOk);

            Button btnCancel = null;
            if (yesNo)
            {
                btnCancel = new Button();
                btnCancel.Text = "No";
                btnCancel.BackColor = OthosColors.ButtonBack;
                btnCancel.ForeColor = OthosColors.Text;
                btnCancel.FlatStyle = FlatStyle.Flat;
                btnCancel.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
                btnCancel.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
                btnCancel.Size = new Size(100, 30);
                btnCancel.Location = new Point(btnOk.Right + 10, btnOk.Top);
                btnCancel.DialogResult = DialogResult.No;
                f.Controls.Add(btnCancel);
                f.AcceptButton = btnOk;
                f.CancelButton = btnCancel;
            }
            else
            {
                f.AcceptButton = btnOk;
                f.CancelButton = btnOk;
            }

            if (owner != null && owner is Form)
{
    f.Icon = ((Form)owner).Icon;
}
int btnRight = (btnCancel != null ? btnCancel.Right : btnOk.Right);
f.ClientSize = new Size(Math.Max(lbl.Right, btnRight) + 20, btnOk.Bottom + 20);
btnOk.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
if (btnCancel != null) btnCancel.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
DialogResult dr = f.ShowDialog(owner);
            f.Dispose();
            return dr;
        }
    }

    internal static class ToolCheck
    {
        public static bool Works(string exe, string args)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(exe, args);
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                using (Process p = Process.Start(psi))
                {
                    if (!p.WaitForExit(15000))
                    {
                        try { p.Kill(); } catch { }
                        return false;
                    }
                    return p.ExitCode == 0;
                }
            }
            catch { return false; }
        }

        public static List<string> Missing()
        {
            List<string> m = new List<string>();
            if (!Works("py", "-3.12 --version")) m.Add("Python 3.12: https://www.python.org/downloads/ (3.12.x, not 3.13 or later; tick 'py launcher')");
            if (!Works("node", "--version")) m.Add("Node.js LTS: https://nodejs.org/");
            if (!Works("ollama", "--version")) m.Add("Ollama: https://ollama.com/download");
            return m;
        }
    }

    // ------------------------------------------------------------------------
    // Stop dialog – separate themed form
    // ------------------------------------------------------------------------
    internal class StopBox : Form
    {
        
        private Button btnTryAgain;
        private Button btnClose;
        private string linkUrl;

        public StopBox(string message)
        {
            this.Text = "Setup STOP";
            this.FormBorderStyle = FormBorderStyle.Sizable;
            this.StartPosition = FormStartPosition.CenterParent;
            this.BackColor = OthosColors.Background;
            this.ForeColor = OthosColors.Text;
            this.Font = new Font("Segoe UI", 10, FontStyle.Regular);
            this.MaximizeBox = false;
            this.MinimizeBox = false;

            Label lbl = new Label();
            lbl.Text = message;
            lbl.AutoSize = true;
            lbl.MaximumSize = new Size(460, 0);
            lbl.ForeColor = OthosColors.Text;
            lbl.BackColor = OthosColors.Background;
            lbl.Location = new Point(20, 20);
            this.Controls.Add(lbl);

            // Detect link
            linkUrl = ExtractUrl(message);
            int buttonTop = lbl.Bottom + 20;

            {
            }

            btnTryAgain = new Button();
            btnTryAgain.Text = "Try Again";
            btnTryAgain.BackColor = OthosColors.ButtonBack;
            btnTryAgain.ForeColor = OthosColors.Text;
            btnTryAgain.FlatStyle = FlatStyle.Flat;
            btnTryAgain.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            btnTryAgain.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnTryAgain.Size = new Size(100, 30);
            btnTryAgain.Location = new Point(20, buttonTop + 40);
            btnTryAgain.DialogResult = DialogResult.Yes;
            this.Controls.Add(btnTryAgain);
            this.AcceptButton = btnTryAgain;

            btnClose = new Button();
            btnClose.Text = "Close";
            btnClose.BackColor = OthosColors.ButtonBack;
            btnClose.ForeColor = OthosColors.Text;
            btnClose.FlatStyle = FlatStyle.Flat;
            btnClose.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            btnClose.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnClose.Size = new Size(100, 30);
            btnClose.Location = new Point(btnTryAgain.Right + 10, btnTryAgain.Top);
            btnClose.DialogResult = DialogResult.No;
            this.Controls.Add(btnClose);
            // Dynamically create a button for each URL in the message
            MatchCollection matches = Regex.Matches(message, @"https://[^\s""'<>)]+");
            int nextX = 20;
            int maxRight = Math.Max(lbl.Right, btnClose.Right);
            int maxBottom = btnClose.Bottom;
            List<Button> linkButtons = new List<Button>();
            foreach (Match m in matches)
            {
                string url = m.Value;
                // Trim trailing punctuation
                while (url.Length > 0 && (url.EndsWith(".") || url.EndsWith(",") || url.EndsWith(";") || url.EndsWith(":")))
                {
                    url = url.Substring(0, url.Length - 1);
                }
                // Determine button text
                string btnText = "Open link";
                string[] lines = message.Split(new char[] { '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries);
                foreach (string line in lines)
                {
    if (line.Contains(url))
    {
        int at = line.IndexOf(": " + url);
        if (at > 0)
        {
            btnText = line.Substring(0, at).Trim();
        }
        break;
    }
                }
                Button linkBtn = new Button();
                linkBtn.Text = btnText;
                linkBtn.BackColor = OthosColors.ButtonBack;
                linkBtn.ForeColor = OthosColors.Text;
                linkBtn.FlatStyle = FlatStyle.Flat;
                linkBtn.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
                linkBtn.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
                linkBtn.Size = new Size(160, 30);
                linkBtn.Location = new Point(nextX, btnTryAgain.Bottom + 10);
                linkBtn.Tag = url;
                linkBtn.Click += delegate(object sender, EventArgs e) { try { Process.Start((string)((Button)sender).Tag); } catch { } };
                linkButtons.Add(linkBtn);
                this.Controls.Add(linkBtn);
                nextX = linkBtn.Right + 10;
                if (linkBtn.Right > maxRight) maxRight = linkBtn.Right;
                if (linkBtn.Bottom > maxBottom) maxBottom = linkBtn.Bottom;
            }
            this.CancelButton = btnClose;
            this.CancelButton = btnClose;
            // Set fixed client size based on controls
            this.ClientSize = new Size(maxRight + 20, maxBottom + 20);
            btnTryAgain.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            btnClose.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            foreach (Button b in linkButtons) b.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            
        }

        private void BtnOpenLink_Click(object sender, EventArgs e)
        {
            try
            {
                Process.Start(linkUrl);
            }
            catch
            {
                // ignore failures
            }
        }

        private string ExtractUrl(string text)
        {
            Match m = Regex.Match(text, @"https://[^\s""'<>)]+", RegexOptions.IgnoreCase);
            if (!m.Success)
            {
                return null;
            }
            string url = m.Value;
            // Trim trailing punctuation
            while (url.Length > 0 && (url.EndsWith(".") || url.EndsWith(",") || url.EndsWith(";") || url.EndsWith(":")))
            {
                url = url.Substring(0, url.Length - 1);
            }
            return url;
        }
        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            try
            {
                int use = 1;
                OthosSetup.DwmSetWindowAttribute(this.Handle, 20, ref use, 4);
            }
            catch { }
        }
    }

    // ------------------------------------------------------------------------
    // "Connect your Google account? (optional)" – one question after a good install.
    // The page itself makes no network call and opens no browser; Connect (only when agent\google-connect.ps1
    // and agent\.venv are there) opens Othos's own Connect Google window, which explains the rest. Records only
    // {"answer", "dont_ask_again", "asked_on"} to agent\state\google-prompt.json.
    // ------------------------------------------------------------------------
    internal static class GooglePrompt
    {
        public const string Title = "Connect your Google account? (optional)";

        public const string Intro =
            "Othos can use your Google Calendar, Sheets, Docs, Gmail and Google Voice voicemails, so you can ask by voice: " +
            "\"What's on my calendar today?\", \"Any new email?\", \"Read me the budget sheet\".";

        public const string Summary =
            "What to know first: your permission is broad, spoken text may go to the online voice, emails can contain " +
            "tricks, Othos never opens links or attachments. Show me how for the full text.";

        public static string FullText()
        {
            StringBuilder sb = new StringBuilder();
            sb.Append(Intro).Append("\n\n");
            sb.Append("What you get\n");
            sb.Append("- Hands-free answers from your own calendar, mail, documents and spreadsheets, read by Othos.\n");
            sb.Append("- Nothing to retype: Othos reads it for you and says where the answer came from.\n");
            sb.Append("- You choose which parts to connect. Everything starts read-only.\n\n");
            sb.Append("What to know first (the risks, in plain words)\n");
            sb.Append("- Your account's permission is broad. Google can't limit \"read email\" to just some messages, so Othos " +
                      "limits itself in its own code (and skips a never-read list you set). If someone got into this PC, the " +
                      "saved sign-in could read the same things until you cancel it.\n");
            sb.Append("- Some of what Othos reads goes out when it speaks. With the online voice (Rime), anything Othos says " +
                      "aloud, including email text, is sent to that voice service. Othos asks before reading an email's full " +
                      "text, unless your voice and hearing both run on this computer.\n");
            sb.Append("- Emails and documents can contain tricks. A message might try to give Othos orders. Othos treats " +
                      "everything it reads as plain text, never as instructions, and can't send, delete or change anything " +
                      "while connected read-only.\n");
            sb.Append("- Othos never opens links or attachments. It doesn't click, download or open anything inside an email " +
                      "or document. If one has a link or an attachment, Othos only tells you it's there (\"this email has a " +
                      "link and one attachment; I haven't opened them\") and you decide whether to open it yourself. That " +
                      "closes off the most common way email is used to attack people.\n");
            sb.Append("- Google shows a warning screen. Because you make your own Google key for this, Google calls the app " +
                      "\"unverified\". That's expected; you click through it once.\n");
            sb.Append("- You can undo it any time: press Disconnect in Othos, or remove Othos at " +
                      "myaccount.google.com/permissions. Both stop access at once.\n");
            sb.Append("- Othos keeps the sign-in on this computer only, locked to your Windows account. Nothing is posted, " +
                      "sent or deleted.");
            return sb.ToString();
        }

        public static string RecordPath(string othosFolder)
        {
            return Path.Combine(Path.Combine(Path.Combine(othosFolder, "agent"), "state"), "google-prompt.json");
        }

        // Ask unless an earlier answer says not to: the page shows again only when
        // dont_ask_again is false and the answer is not "connect".
        public static bool ShouldAsk(string path)
        {
            try
            {
                if (!File.Exists(path)) return true;
                string json = File.ReadAllText(path, Encoding.UTF8);
                if (Regex.IsMatch(json, "\"dont_ask_again\"\\s*:\\s*true")) return false;
                if (Regex.IsMatch(json, "\"answer\"\\s*:\\s*\"connect\"")) return false;
                return true;
            }
            catch { return true; }
        }

        public static string RecordJson(string answer, bool dontAskAgain, DateTime when)
        {
            return "{\"answer\": \"" + answer + "\", \"dont_ask_again\": " + (dontAskAgain ? "true" : "false") +
                   ", \"asked_on\": \"" + when.ToString("yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture) + "\"}\n";
        }

        // Shows the page if it should, and records the answer. Never throws: a failure
        // here never blocks finishing the installer.
        public static void AskAndRecord(IWin32Window owner, string othosFolder)
        {
            try
            {
                string path = RecordPath(othosFolder);
                if (!ShouldAsk(path)) return;
                string answer;
                bool dontAsk;
                string script = ConnectScript(othosFolder);
                using (GooglePromptForm f = new GooglePromptForm(script))
                {
                    f.ShowDialog(owner);
                    answer = f.Answer;
                    dontAsk = f.DontAskAgain;
                }
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                File.WriteAllText(path, RecordJson(answer, dontAsk, DateTime.Now), new UTF8Encoding(false));
                if (answer == "connect" && script != null) StartConnect(script);
            }
            catch { }
        }

        // Othos's Connect Google window (agent\google-connect.ps1), when it and the agent's .venv are both there;
        // null otherwise, and Connect stays off with the "coming soon" hint.
        public static string ConnectScript(string othosFolder)
        {
            try
            {
                string agent = Path.Combine(othosFolder, "agent");
                string script = Path.Combine(agent, "google-connect.ps1");
                if (File.Exists(script) && Directory.Exists(Path.Combine(agent, ".venv"))) return script;
            }
            catch { }
            return null;
        }

        public static string ConnectArguments(string script)
        {
            return "-NoProfile -ExecutionPolicy Bypass -File \"" + script + "\"";
        }

        // Opens the Connect Google window and doesn't wait for it: the installer finishes normally.
        public static void StartConnect(string script)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo("powershell.exe", ConnectArguments(script));
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.WindowStyle = ProcessWindowStyle.Hidden;
                psi.WorkingDirectory = Path.GetDirectoryName(script);
                Process.Start(psi);
            }
            catch { }
        }

        internal static Button MakeButton(string text)
        {
            Button b = new Button();
            b.Text = text;
            b.BackColor = OthosColors.ButtonBack;
            b.ForeColor = OthosColors.Text;
            b.FlatStyle = FlatStyle.Flat;
            b.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            b.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            b.AutoSize = true;
            b.MinimumSize = new Size(100, 30);
            return b;
        }

        internal static RichTextBox MakeText(string text)
        {
            RichTextBox r = new RichTextBox();
            r.ReadOnly = true;
            r.Text = text;
            r.BackColor = OthosColors.Panel;
            r.ForeColor = OthosColors.Text;
            r.BorderStyle = BorderStyle.None;
            r.ScrollBars = RichTextBoxScrollBars.Vertical;
            r.TabStop = false;
            r.HandleCreated += delegate(object s, EventArgs ev) { try { OthosSetup.SetWindowTheme(r.Handle, "DarkMode_Explorer", null); } catch { } };
            return r;
        }

        internal static void Theme(Form f, string title, Size size, Size min)
        {
            f.HandleCreated += delegate(object s, EventArgs ev) { try { int use = 1; OthosSetup.DwmSetWindowAttribute(f.Handle, 20, ref use, 4); } catch { } };
            f.Text = title;
            f.FormBorderStyle = FormBorderStyle.Sizable;
            f.MinimumSize = min;
            f.ClientSize = size;
            f.StartPosition = FormStartPosition.CenterParent;
            f.BackColor = OthosColors.Background;
            f.ForeColor = OthosColors.Text;
            f.Font = new Font("Segoe UI", 10, FontStyle.Regular);
            f.MaximizeBox = true;
            f.MinimizeBox = false;
            f.ShowInTaskbar = false;
        }
    }

    // The question itself. Not now is the default (Enter); Escape, X and Alt+F4 mean Not now.
    internal class GooglePromptForm : Form
    {
        private CheckBox chkDontAsk;
        private bool sawFullText = false;
        private bool connectPressed = false;

        public GooglePromptForm() : this(null) { }

        // connectScript: Othos's Connect Google window, or null when it isn't there (Connect stays off)
        public GooglePromptForm(string connectScript)
        {
            GooglePrompt.Theme(this, "Othos Setup", new Size(620, 400), new Size(460, 340));

            Label head = new Label();
            head.Text = GooglePrompt.Title;
            head.AutoSize = true;
            head.ForeColor = OthosColors.Heading;
            head.Font = new Font("Segoe UI Semibold", 14, FontStyle.Regular);
            head.Location = new Point(20, 16);
            this.Controls.Add(head);

            RichTextBox body = GooglePrompt.MakeText(GooglePrompt.Intro + "\n\n" + GooglePrompt.Summary);
            body.Location = new Point(20, 60);
            body.Size = new Size(this.ClientSize.Width - 40, this.ClientSize.Height - 60 - 130);
            body.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            this.Controls.Add(body);

            Label hint = new Label();
            hint.Text = "Coming soon: Othos's Connect Google window isn't built yet";
            hint.AutoSize = true;
            hint.ForeColor = OthosColors.Warning;
            hint.Location = new Point(20, this.ClientSize.Height - 120);
            hint.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            this.Controls.Add(hint);

            chkDontAsk = new CheckBox();
            chkDontAsk.Text = "Don't ask me again";
            chkDontAsk.AutoSize = true;
            chkDontAsk.ForeColor = OthosColors.Text;
            chkDontAsk.BackColor = OthosColors.Background;
            chkDontAsk.Location = new Point(20, this.ClientSize.Height - 90);
            chkDontAsk.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            this.Controls.Add(chkDontAsk);

            Button btnNotNow = GooglePrompt.MakeButton("Not now");
            btnNotNow.DialogResult = DialogResult.Cancel;
            btnNotNow.Location = new Point(20, this.ClientSize.Height - 50);
            btnNotNow.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            this.Controls.Add(btnNotNow);

            Button btnHow = GooglePrompt.MakeButton("Show me how");
            btnHow.Location = new Point(140, this.ClientSize.Height - 50);
            btnHow.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            btnHow.Click += delegate(object s, EventArgs ev)
            {
                sawFullText = true;
                using (GoogleFullTextForm full = new GoogleFullTextForm()) { full.ShowDialog(this); }
            };
            this.Controls.Add(btnHow);

            Button btnConnect = GooglePrompt.MakeButton("Connect");
            btnConnect.Enabled = connectScript != null; // off when the Connect Google window isn't there
            btnConnect.Location = new Point(this.ClientSize.Width - 120, this.ClientSize.Height - 50);
            btnConnect.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            btnConnect.Click += delegate(object s, EventArgs ev)
            {
                connectPressed = true;
                this.DialogResult = DialogResult.OK;    // closes the page; the window opens after it's recorded
            };
            this.Controls.Add(btnConnect);
            hint.Visible = connectScript == null;

            this.AcceptButton = btnNotNow;              // Enter presses only the default button
            this.CancelButton = btnNotNow;              // Escape means Not now
            this.Shown += delegate(object s, EventArgs ev) { btnNotNow.Focus(); };
        }

        // Connect is "connect"; every other way out (Not now, Escape, X, Alt+F4) is "not now",
        // or "show_how" after reading the full text.
        public string Answer { get { return connectPressed ? "connect" : sawFullText ? "show_how" : "not_now"; } }
        public bool DontAskAgain { get { return chkDontAsk.Checked; } }
    }

    // "Show me how": the full text as its own page; Back (Enter, Escape, X) returns to the question.
    internal class GoogleFullTextForm : Form
    {
        public GoogleFullTextForm()
        {
            GooglePrompt.Theme(this, GooglePrompt.Title, new Size(700, 560), new Size(460, 340));

            RichTextBox body = GooglePrompt.MakeText(GooglePrompt.FullText());
            body.Location = new Point(20, 20);
            body.Size = new Size(this.ClientSize.Width - 40, this.ClientSize.Height - 20 - 70);
            body.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
            this.Controls.Add(body);

            Button back = GooglePrompt.MakeButton("Back");
            back.DialogResult = DialogResult.OK;
            back.Location = new Point(20, this.ClientSize.Height - 50);
            back.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            this.Controls.Add(back);
            this.AcceptButton = back;
            this.CancelButton = back;
            this.Shown += delegate(object s, EventArgs ev) { back.Focus(); };
        }
    }

    // ------------------------------------------------------------------------
    // Main installer form (wizard style)
    // ------------------------------------------------------------------------
    public class OthosSetup : Form
    {
        // UI controls
        private Panel panelMode;
        private Panel panelFolder;
        private Panel panelRun;
        private Button btnMixed;
        private Button btnOffline;
        private TextBox txtAssistantName;
        private Button btnNextFromMode;
        private Button btnBackFromFolder;
        private Button btnNextFromFolder;
        private Button btnBrowseFolder;
        private TextBox txtFolder;
        private ListView lvSteps;
        private RichTextBox rtbOutput;
        private Button btnCancelRun;

        // State
        private bool modeMixed = true;                 // default
        private string assistantName = "Othos";
        private string targetFolder = "";
        private string[] stepNames = new string[]
        {
            "Checking tools",
            "LiveKit server",
            "Python environment",
            "Call page",
            "Models",
            "Your settings",
            "This PC",
            "Done"
        };
        private int currentStepIndex = -1;              // -1 = not started
        private Process setupProcess = null;
        private bool stopShown = false;
        private string lastErrorLine = "";
        private bool copyInProgress = false;
        private bool warningFlag = false; // true if current step had a warning
        private string prevLine = "";
        private string lastLogLine = "";
        private volatile bool cancelRequested = false;
        [DllImport("dwmapi.dll")] internal static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);
        [DllImport("uxtheme.dll", CharSet = CharSet.Unicode)] internal static extern int SetWindowTheme(IntPtr hwnd, string appName, string idList);
protected override void OnHandleCreated(EventArgs e)
{
    base.OnHandleCreated(e);
    try
    {
        int attr = 20;
        int use = 1;
        DwmSetWindowAttribute(this.Handle, attr, ref use, 4);
    }
    catch { }
}

        // Entry point
        [STAThread]
        public static void Main()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new OthosSetup());
        }

        // Constructor
        public OthosSetup()
        {
            this.Text = "Othos Setup";
            this.FormBorderStyle = FormBorderStyle.Sizable;
this.MaximizeBox = true;
this.MinimumSize = new Size(600, 450);
            this.StartPosition = FormStartPosition.CenterScreen;
            this.ClientSize = new Size(820, 660);
            this.BackColor = OthosColors.Background;
            this.ForeColor = OthosColors.Text;
            this.Font = new Font("Segoe UI", 10, FontStyle.Regular);

InitializeControls();
this.Shown += OthosSetup_Shown;
ShowPanel(panelMode);
        }

        // --------------------------------------------------------------------
        // UI initialisation
        // --------------------------------------------------------------------
        private void InitializeControls()
        {
            // -------------------------- Panel: Mode & Name -------------------
            panelMode = new Panel();
panelMode.AutoScroll = true;
            panelMode.Dock = DockStyle.Fill;
            panelMode.Size = this.ClientSize;
            panelMode.BackColor = OthosColors.Panel;
            this.Controls.Add(panelMode);

            Label lblHeader = new Label();
            lblHeader.Text = "Select mode";
            lblHeader.AutoSize = true;
            lblHeader.ForeColor = OthosColors.Heading;
            lblHeader.Font = new Font("Segoe UI Semibold", 14, FontStyle.Regular);
            lblHeader.Location = new Point(20, 20);
            panelMode.Controls.Add(lblHeader);

            btnMixed = new Button();
            btnMixed.Text = "Mixed Mode";
            btnMixed.BackColor = OthosColors.ButtonBack;
            btnMixed.ForeColor = OthosColors.Text;
            btnMixed.FlatStyle = FlatStyle.Flat;
            btnMixed.FlatAppearance.BorderColor = OthosColors.Success;
            btnMixed.FlatAppearance.BorderSize = 2;
            btnMixed.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnMixed.Size = new Size(300, 80);
            btnMixed.Location = new Point(20, 70);
            btnMixed.Click += BtnMixed_Click;
            panelMode.Controls.Add(btnMixed);

            btnOffline = new Button();
            btnOffline.Text = "Offline Mode";
            btnOffline.BackColor = OthosColors.ButtonBack;
            btnOffline.ForeColor = OthosColors.Text;
            btnOffline.FlatStyle = FlatStyle.Flat;
            btnOffline.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            btnOffline.FlatAppearance.BorderSize = 2;
            btnOffline.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnOffline.Size = new Size(300, 80);
            btnOffline.Location = new Point(340, 70);
            btnOffline.Click += BtnOffline_Click;
            panelMode.Controls.Add(btnOffline);

            Label lblDescMixed = new Label();
            lblDescMixed.Text = "Mixed: uses online services where needed.";
            lblDescMixed.AutoSize = true;
            lblDescMixed.ForeColor = OthosColors.Text;
            lblDescMixed.Location = new Point(btnMixed.Left, btnMixed.Bottom + 10);
            panelMode.Controls.Add(lblDescMixed);

            Label lblDescOffline = new Label();
            lblDescOffline.Text = "Offline: everything runs locally.";
            lblDescOffline.AutoSize = true;
            lblDescOffline.ForeColor = OthosColors.Text;
            lblDescOffline.Location = new Point(btnOffline.Left, btnOffline.Bottom + 10);
            panelMode.Controls.Add(lblDescOffline);

            Label lblName = new Label();
            lblName.Text = "Assistant name (default Othos):";
            lblName.AutoSize = true;
            lblName.ForeColor = OthosColors.Text;
            lblName.Location = new Point(20, 200);
            panelMode.Controls.Add(lblName);

            txtAssistantName = new TextBox();
            txtAssistantName.Size = new Size(300, 30);
            txtAssistantName.Location = new Point(20, 230);
            txtAssistantName.ForeColor = OthosColors.Text;
            txtAssistantName.BackColor = OthosColors.Panel;
            txtAssistantName.BorderStyle = BorderStyle.FixedSingle;
            panelMode.Controls.Add(txtAssistantName);

            btnNextFromMode = new Button();
            btnNextFromMode.Text = "Next";
            btnNextFromMode.BackColor = OthosColors.ButtonBack;
            btnNextFromMode.ForeColor = OthosColors.Text;
            btnNextFromMode.FlatStyle = FlatStyle.Flat;
            btnNextFromMode.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            btnNextFromMode.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnNextFromMode.Size = new Size(100, 30);
            btnNextFromMode.Location = new Point(this.ClientSize.Width - 120, this.ClientSize.Height - 50);
btnNextFromMode.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            btnNextFromMode.Click += BtnNextFromMode_Click;
            panelMode.Controls.Add(btnNextFromMode);

            // -------------------------- Panel: Folder -----------------------
            panelFolder = new Panel();
panelFolder.AutoScroll = true;
            panelFolder.Dock = DockStyle.Fill;
            panelFolder.Size = this.ClientSize;
            panelFolder.BackColor = OthosColors.Panel;
            panelFolder.Visible = false;
            this.Controls.Add(panelFolder);

            Label lblFolderHeader = new Label();
            lblFolderHeader.Text = "Select installation folder";
            lblFolderHeader.AutoSize = true;
            lblFolderHeader.ForeColor = OthosColors.Heading;
            lblFolderHeader.Font = new Font("Segoe UI Semibold", 14, FontStyle.Regular);
            lblFolderHeader.Location = new Point(20, 20);
            panelFolder.Controls.Add(lblFolderHeader);

            txtFolder = new TextBox();
txtFolder.Size = new Size(500, 30);
txtFolder.Location = new Point(20, 70);
txtFolder.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            txtFolder.ForeColor = OthosColors.Text;
            txtFolder.BackColor = OthosColors.Panel;
            txtFolder.BorderStyle = BorderStyle.FixedSingle;
            // default to MyDocuments\Othos
            string docs = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
            txtFolder.Text = Path.Combine(docs, "Othos");
            panelFolder.Controls.Add(txtFolder);

            btnBrowseFolder = new Button();
            btnBrowseFolder.Text = "Browse...";
            btnBrowseFolder.BackColor = OthosColors.ButtonBack;
            btnBrowseFolder.ForeColor = OthosColors.Text;
            btnBrowseFolder.FlatStyle = FlatStyle.Flat;
            btnBrowseFolder.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            btnBrowseFolder.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnBrowseFolder.Size = new Size(100, 30);
            btnBrowseFolder.Location = new Point(txtFolder.Right + 10, 70);
btnBrowseFolder.Anchor = AnchorStyles.Top | AnchorStyles.Right;
            btnBrowseFolder.Click += BtnBrowseFolder_Click;
            panelFolder.Controls.Add(btnBrowseFolder);

            btnBackFromFolder = new Button();
            btnBackFromFolder.Text = "Back";
            btnBackFromFolder.BackColor = OthosColors.ButtonBack;
            btnBackFromFolder.ForeColor = OthosColors.Text;
            btnBackFromFolder.FlatStyle = FlatStyle.Flat;
            btnBackFromFolder.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            btnBackFromFolder.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnBackFromFolder.Size = new Size(100, 30);
            btnBackFromFolder.Location = new Point(20, this.ClientSize.Height - 50);
btnBackFromFolder.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
            btnBackFromFolder.Click += BtnBackFromFolder_Click;
            panelFolder.Controls.Add(btnBackFromFolder);

            btnNextFromFolder = new Button();
            btnNextFromFolder.Text = "Next";
            btnNextFromFolder.BackColor = OthosColors.ButtonBack;
            btnNextFromFolder.ForeColor = OthosColors.Text;
            btnNextFromFolder.FlatStyle = FlatStyle.Flat;
            btnNextFromFolder.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            btnNextFromFolder.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnNextFromFolder.Size = new Size(100, 30);
            btnNextFromFolder.Location = new Point(this.ClientSize.Width - 120, this.ClientSize.Height - 50);
btnNextFromFolder.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            btnNextFromFolder.Click += BtnNextFromFolder_Click;
            panelFolder.Controls.Add(btnNextFromFolder);

            // -------------------------- Panel: Run --------------------------
            panelRun = new Panel();
panelRun.AutoScroll = true;
            panelRun.Dock = DockStyle.Fill;
            panelRun.Size = this.ClientSize;
            panelRun.BackColor = OthosColors.Panel;
            panelRun.Visible = false;
            this.Controls.Add(panelRun);

            Label lblStepsHeader = new Label();
            lblStepsHeader.Text = "Setup Steps:";
            lblStepsHeader.AutoSize = true;
            lblStepsHeader.ForeColor = OthosColors.Heading;
            lblStepsHeader.Font = new Font("Segoe UI Semibold", 14, FontStyle.Regular);
            lblStepsHeader.Location = new Point(20, 20);
            panelRun.Controls.Add(lblStepsHeader);

            lvSteps = new ListView();
            lvSteps.View = View.Details;
            lvSteps.FullRowSelect = true;
            lvSteps.HeaderStyle = ColumnHeaderStyle.Nonclickable;
            lvSteps.OwnerDraw = true;
            lvSteps.BackColor = OthosColors.Panel;
            lvSteps.ForeColor = OthosColors.Text;
            lvSteps.BorderStyle = BorderStyle.FixedSingle;
            lvSteps.Size = new Size(750, 238);
lvSteps.Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right;
            lvSteps.Location = new Point(20, 55);
lvSteps.Columns.Add("Step", 400);
lvSteps.Columns.Add("Status", 100);
            lvSteps.DrawColumnHeader += LvSteps_DrawColumnHeader;
lvSteps.Resize += LvSteps_Resize;
            lvSteps.DrawItem += delegate(object s, DrawListViewItemEventArgs ev) { ev.DrawDefault = true; };
            lvSteps.DrawSubItem += delegate(object s, DrawListViewSubItemEventArgs ev) { ev.DrawDefault = true; };
            panelRun.Controls.Add(lvSteps);

            rtbOutput = new RichTextBox();
            rtbOutput.ReadOnly = true;
            rtbOutput.BackColor = OthosColors.Panel;
            rtbOutput.ForeColor = OthosColors.Text;
            rtbOutput.BorderStyle = BorderStyle.FixedSingle;
rtbOutput.Size = new Size(750, this.ClientSize.Height - 278 - 70);
rtbOutput.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
rtbOutput.Location = new Point(20, 278);
rtbOutput.HandleCreated += delegate(object s, EventArgs ev) { try { SetWindowTheme(rtbOutput.Handle, "DarkMode_Explorer", null); } catch { } };
            panelRun.Controls.Add(rtbOutput);

            btnCancelRun = new Button();
            btnCancelRun.Text = "Cancel";
            btnCancelRun.BackColor = OthosColors.ButtonBack;
            btnCancelRun.ForeColor = OthosColors.Text;
            btnCancelRun.FlatStyle = FlatStyle.Flat;
            btnCancelRun.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
            btnCancelRun.FlatAppearance.MouseOverBackColor = OthosColors.ButtonHover;
            btnCancelRun.Size = new Size(100, 30);
            btnCancelRun.Location = new Point(this.ClientSize.Width - 120, this.ClientSize.Height - 50);
btnCancelRun.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;
            btnCancelRun.Click += BtnCancelRun_Click;
            panelRun.Controls.Add(btnCancelRun);
        }

        // --------------------------------------------------------------------
        // Helper to switch visible panel
        // --------------------------------------------------------------------
        private void OthosSetup_Shown(object sender, EventArgs e)
        {
            Rectangle wa = Screen.FromControl(this).WorkingArea;
            this.MinimumSize = new Size(Math.Min(this.MinimumSize.Width, wa.Width), Math.Min(this.MinimumSize.Height, wa.Height));
            this.Width = Math.Min(this.Width, wa.Width);
            this.Height = Math.Min(this.Height, wa.Height);
            this.Location = new Point(wa.Left + (wa.Width - this.Width) / 2, wa.Top + (wa.Height - this.Height) / 2);
        }

        private void ShowPanel(Panel p)
        {
            panelMode.Visible = false;
            panelFolder.Visible = false;
            panelRun.Visible = false;
            p.Visible = true;
            if (p == panelRun) LvSteps_Resize(lvSteps, EventArgs.Empty);
        }

        // --------------------------------------------------------------------
        // Mode button handlers (border highlights)
        // --------------------------------------------------------------------
        private void BtnMixed_Click(object sender, EventArgs e)
        {
            modeMixed = true;
            btnMixed.FlatAppearance.BorderColor = OthosColors.Success;
            btnOffline.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
        }

        private void BtnOffline_Click(object sender, EventArgs e)
        {
            modeMixed = false;
            btnOffline.FlatAppearance.BorderColor = OthosColors.Success;
            btnMixed.FlatAppearance.BorderColor = OthosColors.ButtonBorder;
        }

        // --------------------------------------------------------------------
        // Next from mode
        // --------------------------------------------------------------------
        private void BtnNextFromMode_Click(object sender, EventArgs e)
        {
            if (!String.IsNullOrWhiteSpace(txtAssistantName.Text))
            {
                assistantName = txtAssistantName.Text.Trim();
            }
            else
            {
                assistantName = "Othos";
            }
            ShowPanel(panelFolder);
        }

        // --------------------------------------------------------------------
        // Folder browsing
        // --------------------------------------------------------------------
        private void BtnBrowseFolder_Click(object sender, EventArgs e)
        {
            using (FolderBrowserDialog dlg = new FolderBrowserDialog())
            {
                dlg.Description = "Select target folder for the Othos agent.";
                dlg.ShowNewFolderButton = true;
                if (dlg.ShowDialog() == DialogResult.OK)
                {
                    txtFolder.Text = dlg.SelectedPath;
                }
            }
        }

        private void BtnBackFromFolder_Click(object sender, EventArgs e)
        {
            ShowPanel(panelMode);
        }

        private void BtnNextFromFolder_Click(object sender, EventArgs e)
        {
            if (String.IsNullOrWhiteSpace(txtFolder.Text))
            {
                ThemedBox.Show(this, "Please select a folder.", "Folder required", false);
                return;
            }
            targetFolder = txtFolder.Text.Trim();

            // Ensure folder exists
            try
            {
                Directory.CreateDirectory(targetFolder);
            }
            catch (Exception ex)
            {
                ThemedBox.Show(this, "Failed to create folder: " + ex.Message, "Error", false);
                return;
            }

            // Show run panel immediately
            ShowPanel(panelRun);
            InitializeStepList();

            // Start copy on background thread
            Thread copyThread = new Thread(new ThreadStart(CopyThreadProc));
            copyThread.IsBackground = true;
            copyThread.Start();
        }

        // --------------------------------------------------------------------
        // Copy thread procedure
        // --------------------------------------------------------------------
        private void CopyThreadProc()
        {
            copyInProgress = true;
            // Compare installer folder with target folder
            string installerFolder = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory).TrimEnd('\\');
            string targetFull = Path.GetFullPath(targetFolder).TrimEnd('\\');
            if (String.Compare(installerFolder, targetFull, StringComparison.OrdinalIgnoreCase) == 0)
            {
                AppendLog("Already in the Othos folder; nothing to copy.");
                copyInProgress = false;
                BeginInvoke(new Action(StartSetupProcess));
                return;
            }

            AppendLog("Copying files...");
            int totalCopied = 0;
            string baseDir = AppDomain.CurrentDomain.BaseDirectory;
            string[] required = new string[] { "agent", "ui" };
            foreach (string sub in required)
            {
                string src = Path.Combine(baseDir, sub);
                if (!Directory.Exists(src))
                {
                    AppendLog("Missing folder: " + sub);
                    copyInProgress = false;
                    BeginInvoke((Action)(() => ThemedBox.Show(this, "Put OthosSetup.exe in the Othos folder, next to agent and ui.", "Missing folder", false)));
                    return;
                }
                string dest = Path.Combine(targetFolder, sub);
                try
                {
                    int copied = CopyDirectory(src, dest, new string[] { ".venv", "node_modules", ".next", ".git" });
                    totalCopied += copied;
                    AppendLog(string.Format("{0} files copied for {1}.", copied, sub));
                }
                catch (Exception ex)
                {
                    AppendLog("Error copying folder '" + sub + "': " + ex.Message);
                    copyInProgress = false;
                    string msg = "Error copying folder '" + sub + "': " + ex.Message;
                    BeginInvoke((Action)(() => ThemedBox.Show(this, msg, "Error", false)));
                    return;
                }
            }
            AppendLog(string.Format("{0} total files copied.", totalCopied));
            copyInProgress = false;
            BeginInvoke(new Action(StartSetupProcess));
        }

        // --------------------------------------------------------------------
        // Recursive directory copy with exclusions, overwriting
        // --------------------------------------------------------------------
        private int CopyDirectory(string source, string destination, string[] excludeDirs)
        {
            int count = 0;
            if (!Directory.Exists(destination))
                Directory.CreateDirectory(destination);

            foreach (string file in Directory.GetFiles(source))
            {
                string destFile = Path.Combine(destination, Path.GetFileName(file));
                File.Copy(file, destFile, true);
                count++;
            }

            foreach (string dir in Directory.GetDirectories(source))
            {
                string dirName = Path.GetFileName(dir);
                bool skip = false;
                foreach (string ex in excludeDirs)
                {
                    if (dirName.Equals(ex, StringComparison.OrdinalIgnoreCase))
                    {
                        skip = true;
                        break;
                    }
                }
                if (!skip)
                {
                    count += CopyDirectory(dir, Path.Combine(destination, dirName), excludeDirs);
                }
            }
            return count;
        }

        // --------------------------------------------------------------------
        // Cancel button – stop everything
        // --------------------------------------------------------------------
        private void BtnCancelRun_Click(object sender, EventArgs e)
        {
            if (copyInProgress)
            {
                // During copy, just close the app
                Application.Exit();
                return;
            }

            if (setupProcess != null && !setupProcess.HasExited)
            {
                try
                {
                    ProcessStartInfo psiKill = new ProcessStartInfo();
                    psiKill.FileName = "cmd.exe";
                    psiKill.Arguments = string.Format("/C taskkill /PID {0} /T /F", setupProcess.Id);
                    psiKill.CreateNoWindow = true;
                    psiKill.UseShellExecute = false;
                    Process.Start(psiKill);
                }
                catch { }
            }
            Application.Exit();
        }

        // --------------------------------------------------------------------
        // Step list initialisation
        // --------------------------------------------------------------------
        private void InitializeStepList()
        {
            if (lvSteps.InvokeRequired)
            {
                lvSteps.BeginInvoke(new Action(InitializeStepList));
                return;
            }
            lvSteps.Items.Clear();
            foreach (string step in stepNames)
            {
                ListViewItem item = new ListViewItem(step);
                item.UseItemStyleForSubItems = false;
                item.SubItems.Add("");
                lvSteps.Items.Add(item);
            }
            currentStepIndex = -1;
            rtbOutput.Clear();
            stopShown = false;
    lastErrorLine = "";
    prevLine = "";
        }

        // --------------------------------------------------------------------
        // Draw column header for step list
        // --------------------------------------------------------------------
private void LvSteps_Resize(object sender, EventArgs e)
{
    if (lvSteps.Columns.Count >= 2)
    {
        int total = lvSteps.Width - 20;
        lvSteps.Columns[0].Width = (int)(total * 0.7);
        lvSteps.Columns[1].Width = -2;
    }
}
private void LvSteps_DrawColumnHeader(object sender, DrawListViewColumnHeaderEventArgs e)
{
    using (SolidBrush backBrush = new SolidBrush(Color.FromArgb(14, 16, 25)))
    using (SolidBrush textBrush = new SolidBrush(OthosColors.Text))
    {
        e.Graphics.FillRectangle(backBrush, e.Bounds);
        e.Graphics.DrawString(e.Header.Text, new Font("Segoe UI", 10, FontStyle.Regular), textBrush, e.Bounds);
        // fill any empty space to the right of the last column
        if (e.ColumnIndex == lvSteps.Columns.Count - 1)
        {
            Rectangle r = e.Bounds;
            r.X = e.Bounds.Right;
            r.Width = lvSteps.Width - e.Bounds.Right;
            e.Graphics.FillRectangle(backBrush, r);
        }
    }
}

        // --------------------------------------------------------------------
        // Start the PowerShell setup script
        // --------------------------------------------------------------------
        private void StartSetupProcess()
        {
            // Check for optional Git
            if (!ToolCheck.Works("git", "--version"))
            {
                AppendLog("!! Git not found (optional: only needed to send changes back)");
            }
            List<string> missing = ToolCheck.Missing();
            if (missing.Count > 0)
            {
                AppendLog("STOP: missing programs: " + missing.Count);
                currentStepIndex = Array.IndexOf(stepNames, "Checking tools");
                UpdateStepStatus(currentStepIndex, "STOP", false);
                stopShown = true;
                ShowStopBox("These programs are missing:\n\n" + string.Join("\n", missing.ToArray()) + "\n\nInstall them, then press Try Again.");
                return;
            }
            string scriptPath = Path.Combine(targetFolder, "agent", "setup.ps1");
            if (!File.Exists(scriptPath))
            {
                ThemedBox.Show(this, "setup.ps1 not found in the selected folder.", "Error", false);
                return;
            }

            string modeArg = modeMixed ? "mixed" : "offline";

            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = "powershell.exe";
            psi.Arguments = string.Format("-NoProfile -ExecutionPolicy Bypass -Command \"[Console]::OutputEncoding=[Text.Encoding]::UTF8; & '{0}' -Mode {1}\"", scriptPath, modeArg);
            psi.WorkingDirectory = Path.GetDirectoryName(scriptPath);
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.RedirectStandardInput = true;
            psi.StandardOutputEncoding = Encoding.UTF8;
            psi.StandardErrorEncoding = Encoding.UTF8;

            // Attach handlers BEFORE start

            try
            {
                setupProcess = new Process();
                setupProcess.StartInfo = psi;
                setupProcess.OutputDataReceived += Process_OutputDataReceived;
                setupProcess.ErrorDataReceived += Process_ErrorDataReceived;
                setupProcess.Exited += Process_Exited;
                setupProcess.EnableRaisingEvents = true;
                setupProcess.Start();

                // Write an empty line and close stdin
                if (setupProcess.StandardInput != null)
                {
                    setupProcess.StandardInput.WriteLine();
                    setupProcess.StandardInput.Close();
                }

                setupProcess.BeginOutputReadLine();
                setupProcess.BeginErrorReadLine();
            }
            catch (Exception ex)
            {
                ThemedBox.Show(this, "Failed to start setup: " + ex.Message, "Error", false);
            }
        }

        // --------------------------------------------------------------------
        // Process output handling – marshaled to UI thread
        // --------------------------------------------------------------------
        private void Process_OutputDataReceived(object sender, DataReceivedEventArgs e)
        {
    if (e.Data == null) return;
    string line = e.Data;
    this.BeginInvoke((Action)(() => { HandleProcessLine(line); }));
        }

        private void Process_ErrorDataReceived(object sender, DataReceivedEventArgs e)
        {
    if (e.Data == null) return;
    string line = e.Data;
    string trimmed = line.Trim();
    if (trimmed.Length != 0)
    {
        bool keep = true;
        if (trimmed.StartsWith("At ")) keep = false;
        else if (trimmed.StartsWith("+")) keep = false;
        else if (trimmed.StartsWith("CategoryInfo")) keep = false;
        else if (trimmed.StartsWith("FullyQualifiedErrorId")) keep = false;
        else if (trimmed.StartsWith("npm warn")) keep = false;
        else if (trimmed.StartsWith("npm notice")) keep = false;
        else if (trimmed.StartsWith("WARNING:")) keep = false;
        if (keep)
        {
            lastErrorLine = trimmed;
        }
    }
    this.BeginInvoke((Action)(() => { HandleProcessLine(line); }));
        }

        private void HandleProcessLine(string line)
        {
    // Remove ANSI escape codes
    string cleaned = Regex.Replace(line, "\x1b\\[[0-9;?]*[A-Za-z]", "");
    cleaned = Regex.Replace(cleaned, "\\[\\??[0-9;]*[A-Za-z]", "");
    string trimmed = cleaned.Trim();

    // Skip empty lines
    if (trimmed.Length == 0)
    {
        prevLine = "";
        return;
    }

    // Detect PowerShell error line that follows an error message
    if (trimmed.StartsWith("At ") && trimmed.Contains(".ps1:"))
    {
        lastErrorLine = prevLine;
    }

    // Detect progress bar lines from ollama pull
    bool replace = false;
    if (trimmed.StartsWith("pulling ") && lastLogLine.StartsWith("pulling "))
    {
        replace = true;
    }

    AppendLog(trimmed, replace);
    lastLogLine = trimmed;

    // Step start detection (lines like "== StepName")
    if (trimmed.StartsWith("== "))
    {
        // Finish previous step
        if (currentStepIndex >= 0 && currentStepIndex < stepNames.Length)
        {
            string prevStatus = warningFlag ? "OK !!" : "OK";
            UpdateStepStatus(currentStepIndex, prevStatus, !warningFlag, warningFlag);
            warningFlag = false;
        }

        string stepName = trimmed.Substring(3).Trim();
        for (int i = 0; i < stepNames.Length; i++)
        {
            if (stepName.StartsWith(stepNames[i], StringComparison.OrdinalIgnoreCase))
            {
                currentStepIndex = i;
                UpdateStepStatus(i, "", false);
                break;
            }
        }
        // Update prevLine before returning
        prevLine = trimmed;
        return;
    }

    // STOP detection
    if (trimmed.StartsWith("STOP:"))
    {
        if (currentStepIndex >= 0 && currentStepIndex < stepNames.Length)
        {
            UpdateStepStatus(currentStepIndex, "STOP", false);
        }
        if (!stopShown)
        {
            stopShown = true;
            ShowStopBox(trimmed.Substring(5).Trim());
        }
        // Update prevLine before returning
        prevLine = trimmed;
        return;
    }

    // Warning detection
    if (trimmed.StartsWith("!!"))
    {
        if (currentStepIndex >= 0 && currentStepIndex < stepNames.Length)
        {
            warningFlag = true;
            UpdateStepStatus(currentStepIndex, "!!", false, true);
        }
        // Update prevLine before returning
        prevLine = trimmed;
        return;
    }

    // Update prevLine for normal lines
    prevLine = trimmed;
        }

        // --------------------------------------------------------------------
        // Append log – marshaled to UI thread if needed
        // --------------------------------------------------------------------
        private void AppendLog(string line)
        {
    if (rtbOutput.InvokeRequired)
    {
        rtbOutput.BeginInvoke(new Action<string>(AppendLog), line);
        return;
    }
    rtbOutput.AppendText(line + Environment.NewLine);
    rtbOutput.SelectionStart = rtbOutput.Text.Length;
    rtbOutput.ScrollToCaret();
        }

        private void AppendLog(string line, bool replaceLast)
        {
    if (rtbOutput.InvokeRequired)
    {
        rtbOutput.BeginInvoke(new Action<string, bool>(AppendLog), line, replaceLast);
        return;
    }
    if (replaceLast)
    {
        int idx = rtbOutput.Lines.Length - 1;
        if (idx >= 0 && rtbOutput.Lines[idx].Length == 0) idx--;
        if (idx >= 0)
        {
            int start = rtbOutput.GetFirstCharIndexFromLine(idx);
            rtbOutput.Select(start, rtbOutput.Lines[idx].Length);
            rtbOutput.SelectedText = line;
        }
        else
        {
            rtbOutput.AppendText(line + Environment.NewLine);
        }
    }
    else
    {
        rtbOutput.AppendText(line + Environment.NewLine);
    }
    rtbOutput.SelectionStart = rtbOutput.Text.Length;
    rtbOutput.ScrollToCaret();
        }

        // --------------------------------------------------------------------
        // Update step status (thread‑safe)
        // --------------------------------------------------------------------
        private void UpdateStepStatus(int index, string statusText, bool isOk)
        {
            UpdateStepStatus(index, statusText, isOk, false);
        }

        private void UpdateStepStatus(int index, string statusText, bool isOk, bool isWarning)
        {
            if (index < 0 || index >= lvSteps.Items.Count) return;
            if (lvSteps.InvokeRequired)
            {
                lvSteps.BeginInvoke(new Action<int, string, bool, bool>(UpdateStepStatus), index, statusText, isOk, isWarning);
                return;
            }
            ListViewItem item = lvSteps.Items[index];
            item.SubItems[1].Text = statusText;
            if (isOk)
            {
                item.SubItems[1].ForeColor = OthosColors.Success;
            }
            else if (isWarning)
            {
                item.SubItems[1].ForeColor = OthosColors.Warning;
            }
            else if (statusText == "STOP")
            {
                item.SubItems[1].ForeColor = OthosColors.Warning;
            }
            else
            {
                item.SubItems[1].ForeColor = OthosColors.Text;
            }
        }

        // --------------------------------------------------------------------
        // Show STOP dialog using StopBox form
        // --------------------------------------------------------------------
        private void ShowStopBox(string message)
        {
            StopBox sb = new StopBox(message);
            DialogResult dr = sb.ShowDialog(this);
            if (dr == DialogResult.Yes)
            {
                // Try Again
                CleanupSetupProcess();
                InitializeStepList();
                // Restart setup without copying again
                BeginInvoke(new Action(StartSetupProcess));
            }
            else
            {
                // Close – do nothing further
            }
        }

        // --------------------------------------------------------------------
        // Cleanup any existing process (used for Try Again)
        // --------------------------------------------------------------------
        private void CleanupSetupProcess()
        {
            if (setupProcess != null)
            {
                try
                {
                    setupProcess.OutputDataReceived -= Process_OutputDataReceived;
                    setupProcess.ErrorDataReceived -= Process_ErrorDataReceived;
                    setupProcess.Exited -= Process_Exited;
                    if (!setupProcess.HasExited)
                    {
                        setupProcess.Kill();
                    }
                }
                catch { }
                finally
                {
                    setupProcess.Dispose();
                    setupProcess = null;
                }
            }
        }

        // --------------------------------------------------------------------
        // Process exited handling – marshaled to UI thread
        // --------------------------------------------------------------------
        private void Process_Exited(object sender, EventArgs e)
        {
            // Ensure this is the current process
            if (sender != setupProcess) return;
            this.BeginInvoke(new Action(HandleProcessExited));
        }

        private void HandleProcessExited()
        {
            int exitCode = -1;
            if (setupProcess != null)
            {
                try { exitCode = setupProcess.ExitCode; } catch { }
            }
            int doneIdx = stepNames.Length - 1;

            if (exitCode == 0)
            {
                if (assistantName != "Othos") WriteAssistantName();
                UpdateStepStatus(doneIdx, "OK", true);
                GooglePrompt.AskAndRecord(this, targetFolder);   // optional; never blocks finishing
        ThemedBox.Show(this, "Setup is done. Fill in these three files in the agent folder: .env (your keys), personal.json (your name and town) and profile.md. They open in your editor if one was found. Then start Othos with start-othos.bat.", "Done", false);
                return;
            }

            if (currentStepIndex >= 0 && currentStepIndex < doneIdx)
            {
                UpdateStepStatus(currentStepIndex, "STOP", false);
            }
            UpdateStepStatus(doneIdx, "STOP", false);
            if (!stopShown)
            {
    string msg = "Setup stopped";
    if (currentStepIndex >= 0 && currentStepIndex < stepNames.Length - 1)
    {
        msg = msg + " at the step " + stepNames[currentStepIndex] + ".";
    }
    else
    {
        msg = msg + ".";
    }
    if (!string.IsNullOrEmpty(lastErrorLine))
    {
        msg = msg + "\n\nThe error was: " + lastErrorLine + "\n\nThe full log is in the window.";
    }
    ThemedBox.Show(this, msg, "Setup stopped", false);
            }
        }

        private void WriteAssistantName()
        {
            try
            {
                string jsonPath = Path.Combine(targetFolder, "agent", "personal.json");
                if (!File.Exists(jsonPath)) return;
                string json = File.ReadAllText(jsonPath, Encoding.UTF8);
                Regex regex = new Regex("(\"assistant\"\\s*:\\s*\\{\\s*\"name\"\\s*:\\s*\")[^\"]*(\")");
                string escapedName = assistantName.Replace("\\", "\\\\").Replace("\"", "\\\"");
                string replacement = "${1}" + escapedName.Replace("$", "$$") + "${2}";
                File.WriteAllText(jsonPath, regex.Replace(json, replacement, 1), new UTF8Encoding(false));
            }
            catch (Exception ex)
            {
                AppendLog("Failed to write assistant name: " + ex.Message);
            }
        }
    }
}
