# UNIC OMR Scanner: user guide

What every screen, button and option does. In the app, open it with **Help**
in the top bar (or go to `/help`): the search box there keeps only the
sections that contain all the words you type, for example `double mark`,
`regrade`, `index points` or `roll number`.

## Where to find what

| I want to… | Go to |
|---|---|
| Install, start or stop the program, open it from another PC or phone | 1. Getting started |
| Change the program's name or icon | 1. Getting started → Name and icon (branding) |
| Read a few sheets now | 2. Scan |
| Read thousands of sheets, pause, resume after a crash, rescan errors | 2. Jobs |
| Check the sheets the program was unsure about | 2. Review queue (and the flag table there) |
| Get a CSV / Excel file of the answers | 2. Export |
| Look at one sheet, correct a bubble, verify it | 3. Results screen |
| Re-read a sheet with other settings, or a sheet read upside down, mirrored or a row off | 3. Results screen → Regrade, Place the page by hand |
| See who scanned, reviewed, changed, regraded or verified a sheet; check and verify changed sheets in bulk | 3. Results screen → Who did what, Check changes |
| Hand flagged sheets out to reviewers, see your own sheets, send a sheet back to review | 3. Results screen → Check changes (Hand out review work, Send back); Results › Assigned to |
| Make or change a template (blocks, bubbles, roll number, barcode) | 4. Templates and the template editor |
| Choose an alignment method (timing tracks, index points, corner markers) | 4. → Alignment |
| Tune how dark a mark must be, double marks, light pencil | 5. → Review thresholds |
| Answer keys, marks, negative marking | 5. → Scoring |
| Pink or red printed sheets | 5. → Colours |
| Read big batches faster | 5. → Fast pass |

## 1. Getting started: install, launcher, accounts, remote access, branding

UNIC OMR Scanner reads scanned answer sheets and shows the results in your web browser. The program runs a small server on one PC (the "station"). You and your colleagues then use it from a browser on that PC, on other PCs in the office, or from anywhere through remote access.

This section covers installing the program, the launcher window, the command-line options, signing in and user accounts, the top bar and tabs, and changing the program's name and icon.

> Older versions were called **OMRChecker** or **OMR Station**. The program file (`OMRChecker.exe`), the install folder and the data folder still use the name `OMRChecker`, so your existing results stay where they are.

### Installing

You can install the program in two ways. Both give you the same program.

| Way | What you get | Best for |
| --- | --- | --- |
| **Online installer** (`OMRChecker-setup-<version>.exe`) | A normal installed program: a Start menu entry, an optional desktop icon, and an uninstaller in Windows Settings. | Most office PCs. |
| **Portable zip** (`OMRChecker-portable-win64.zip`) | A folder you unzip anywhere (Desktop, USB drive, D:\\). Nothing is installed. | PCs where you have no admin rights, or when you want to carry the program and its data together. |
| **Single exe** (`OMRChecker-onefile.exe`) | One file. It unpacks itself to a temporary folder every time it starts (and for every worker process). | Quick tries only. It is slower for large batches; use the portable folder for bulk work. |

Supported Windows: Windows 7 SP1 (64-bit) and later. Windows 7 needs updates KB2533623 and KB2999226, which every fully updated Windows 7 already has.

#### How to install with the online installer

1. Run `OMRChecker-setup-<version>.exe`.
2. Windows asks how to install:
   - **For all users** (needs an administrator password): the program goes to `C:\Program Files\OMRChecker`.
   - **Only for me** (no admin rights needed): the program goes to your own programs folder.
3. Choose the folder. Use an empty or new folder. The installer refuses a folder that already holds other files, because uninstalling removes the whole program folder.
4. Tick **Create a desktop shortcut** if you want one.
5. The installer downloads three files (the program, the reading models and the tools), checks each one and unpacks it. This needs an internet connection.
6. At the end, leave **Launch UNIC OMR Scanner** ticked to start it straight away.

**No internet on that PC?** Copy the three zip files (`OMRChecker-app.zip`, `OMRChecker-ocr-models.zip`, `OMRChecker-tools.zip`) from the release into the same folder as the setup file. The installer uses them instead of downloading. This is also the safest route on older Windows 7 PCs, which may fail to download from GitHub.

**Upgrading:** run the newer setup file. It replaces the program in place. Your data is not touched.

**Uninstalling:** use Windows Settings (Apps) or Control Panel. Your scans, templates and settings stay in the data folder (see below).

#### How to use the portable zip

1. Unzip `OMRChecker-portable-win64.zip` to a folder where you can write files, for example your Desktop or `D:\OMR`.
2. Open the folder and double-click `OMRChecker.exe`.
3. Keep the whole folder together. Do not move only the exe.

### Where your data lives

All templates, scan results, the review queue, user accounts, export profiles and settings are kept in one data folder called `omr_data`.

| How you run the program | Data folder |
| --- | --- |
| Installed with the online installer | `%LOCALAPPDATA%\OMRChecker\omr_data` (for example `C:\Users\<you>\AppData\Local\OMRChecker\omr_data`). It is never inside the program folder, so upgrading or uninstalling leaves it alone. |
| Portable folder or single exe | `omr_data` next to `OMRChecker.exe`. |
| Portable, but the folder is read-only (for example copied into `C:\Program Files`) | Falls back to `%LOCALAPPDATA%\OMRChecker\omr_data`. |
| Started with `--data-dir <folder>` | That folder. |

The launcher window shows the data folder in use at the bottom ("Data: ..."), so you can always check.

Tips:

- **Back up** the data folder to keep your results. Stop the program first.
- **Moving to a new PC:** copy the whole `omr_data` folder to the same place on the new PC (or start the program there with `--data-dir`).
- **Antivirus:** on Windows, excluding `omr_data` and the program folder from real-time scanning makes large batches much faster.
- Files inside the data folder that you may meet: `accounts.sqlite3` (user accounts), `remote_access.json` (Cloudflare settings), `templates`, `scans`, `jobs`, `exports`.

### The launcher window

When you start `OMRChecker.exe`, the program loads the reading engine, starts the server, opens the app in your default browser, and shows a small control window titled **UNIC OMR Scanner 1.0.0** (the name follows your branding). The browser app is where you do your work; the launcher window controls the server.

**Closing the launcher window stops the program.** Keep it open (you can minimise it) while anyone is using the station.

#### Server

| Control | Default | What it does |
| --- | --- | --- |
| **Port** | 8765 (or the port in use) | The network port the server listens on. The app's address is `http://127.0.0.1:<port>/`. Change it only if another program already uses 8765, or your IT department asks for a specific port. Must be a number from 1 to 65535. If the port you enter is busy, the server picks a free one and says so ("port 8765 was busy; using ..."). |
| **Other devices on this network can connect** | Off | Off: only this PC can open the app. On: other PCs, tablets and phones on the same office network can open it using this PC's network name or IP address, for example `http://192.168.1.20:8765/`. Turn it on when several people work on one station. Windows Firewall may ask you to allow access the first time. Turn on sign-in (see *Accounts*) before you do this on a shared network. |
| Status line | | Shows **Starting the server …**, **Running** (green), **Stopped**, or an error message in red. |
| Address box and **Copy** | | The address of the app while the server runs. **Copy** puts it on the clipboard so you can paste it into a browser or a message. |
| **Start server** / **Restart server** | | Starts the server with the port and network choice above. While it is running the button reads **Restart server**; use it after changing the port or the network tick box. Port and network changes only take effect when you press this button. |
| **Stop server** | | Stops the server. Anyone using the app loses the connection. Stopping the server also stops remote access. Jobs that were running continue from where they stopped the next time the server starts. |
| **Open GUI** | | Opens the app in your default browser. Use it if you closed the browser tab. Only available while the server runs. |

#### Remote access (Cloudflare)

Remote access lets people outside your office network open the station through a secure `https://` address, without changing your router or firewall. It uses a Cloudflare Tunnel; the needed tool (`cloudflared`) ships with the program.

**Before you share a remote address, turn on sign-in** (create the administrator account). Without it, anyone who has the address can use the station.

| Control | What it does |
| --- | --- |
| Mode line | Says which kind of tunnel will be used: **Quick tunnel: a new random trycloudflare.com address each start**, **Your address: https://...** (your own hostname), or **Your Cloudflare tunnel (address set in the Cloudflare dashboard)**. |
| Status line | **Not running**, **Connecting to Cloudflare …**, **Online: anyone with the address can reach the sign-in page** (green), or the error in red. |
| Address box and **Copy** | The public address while the tunnel is online. **Copy** puts it on the clipboard. |
| **Start remote** | Opens the tunnel. Only available while the server runs. |
| **Stop remote** | Closes the tunnel. The local server keeps running. |
| **Cloudflare settings…** | Opens the settings dialog below. |

**Cloudflare settings dialog**

Leave everything empty for a **quick tunnel**: no Cloudflare account needed, but you get a new random address (`https://<words>.trycloudflare.com`) every time you start remote access. Good for occasional use.

For a **fixed address on your own domain**, fill in **one** of the two methods:

| Field | What to enter |
| --- | --- |
| **Tunnel token (A)** | Method A. In the Cloudflare dashboard: Zero Trust > Networks > Tunnels > your tunnel > install command; copy the token. In the dashboard, point the tunnel's public hostname at `http://localhost:<port>` (the port shown in the launcher). |
| **API token (B)** | Method B. A Cloudflare API token with the permissions **Account > Cloudflare Tunnel: Edit** and **Zone > DNS: Edit**. The station then creates the tunnel (named `omrchecker-<hostname>`), points it at the current port and adds the DNS record by itself. |
| **Public hostname** | For example `omr.example.com`. Required for method B (the domain must be in that Cloudflare account). With method A it is optional and is only used to show the address in the window. |

Buttons: **Use quick tunnel** empties all fields, **Cancel** closes without saving, **Save** checks the hostname and saves. The settings are saved in the data folder (`remote_access.json`). **Press Stop remote and Start remote again to apply new settings.** Tokens are hidden while you type.

If both tokens are filled in, the API token (method B) is used.

Common problems:

- **"cloudflared was not found"**: the `cloudflared` folder next to the exe is missing. Reinstall or re-unzip the program.
- **On Windows 7, remote access fails to start**: current Cloudflare tools need Windows 10 or newer. Run the tunnel from a newer PC on the same network, or ask IT for an older `cloudflared.exe`.
- **"Enter the public hostname"** or **"... is not a valid hostname"**: method B needs a full name such as `omr.example.com`, without `https://`.
- **"No domain of ... is in this Cloudflare account"**: the API token cannot see that domain, or the domain is in a different account.
- **Phones and the live camera**: a phone's browser only lets the page use the camera over `https://`, so use the remote address (or another HTTPS setup) for the camera page on phones.

#### Footer

- **Data: ...** shows the data folder in use.
- **Quit** stops remote access and the server and closes the program. Closing the window with its X does the same.

#### How to let colleagues in the office use your station

1. In the launcher, tick **Other devices on this network can connect**.
2. Press **Restart server** (or **Start server**).
3. Allow the program through Windows Firewall if asked.
4. In the app, turn on sign-in (top bar **User** > **Create the administrator account…**) and add accounts for your colleagues.
5. Give colleagues the address with this PC's IP address or name, for example `http://192.168.1.20:8765/`. (The address box shows `127.0.0.1`, which only works on this PC; find this PC's IP address with `ipconfig` or ask IT.)

#### How to open the station from home

1. Turn on sign-in and make sure every user has a good password.
2. In the launcher, press **Start remote**.
3. Wait for **Online** and press **Copy** next to the address.
4. Send the address to the people who need it. They sign in as usual.
5. Press **Stop remote** when you are done. With a quick tunnel the address changes next time.

### Command-line options

You can start `OMRChecker.exe` from a Command Prompt with options. Double-clicking it is the same as running it with no options.

#### Server and window options

| Option | Default | What it does |
| --- | --- | --- |
| `--port <number>` | 8765, or any free port | The port to listen on. |
| `--host <address>` | `127.0.0.1` (this PC only) | `0.0.0.0` lets other devices on the network connect (same as the tick box in the launcher). |
| `--data-dir <folder>` | see *Where your data lives* | Use a different data folder. |
| `--workers <number>` | all processor cores | How many sheets are read at the same time in bulk jobs. Lower it if the PC becomes unusable during a job. |
| `--api-key <secret>` | none (or the `OMR_API_KEY` environment variable) | Requires programs to send this key (see *API key*). |
| `--no-browser` | off | Do not open the browser at start. |
| `--no-window` | off | No launcher window; runs as a plain console server. Stop it with Ctrl+C. |
| `--remote` | off | Also start remote access at launch (uses the saved Cloudflare settings). The address is printed. Useful together with `--no-window`. |
| `--log-level <level>` | `warning` | How much the server writes to the console (`info`, `debug` for more). For troubleshooting only. |
| `--selftest` | | Checks the installation without starting the app: reads a few test sheets, decodes test barcodes, starts worker processes and reports which optional engines (OCR, learned models, barcode readers) loaded. Exit code 0 means OK. Run it on a new PC, or when something does not work. |
| `--version` | | Prints the program name and version. |

Examples:

```
OMRChecker.exe
OMRChecker.exe --host 0.0.0.0 --port 8000
OMRChecker.exe --api-key MySecret123
OMRChecker.exe --no-window --remote
OMRChecker.exe --selftest
```

#### Headless bulk mode

Bulk mode reads every image and PDF in a folder with one template, writes the results to files and exits. No window or browser is used. It is handy for very large batches, overnight runs or scheduled tasks.

| Option | Default | What it does |
| --- | --- | --- |
| `--bulk <folder>` | | The folder of scanned sheets (images and PDFs) to read. Turns on bulk mode. |
| `--template <file>` | required | The `template.json` to read the sheets with. Bulk mode stops with a message if it is missing. You can find a template's files in the data folder under `templates`. |
| `--out <folder>` | `<scan folder>_omr_results` next to the scan folder | Where to write `results.csv` and `results.jsonl`. |
| `--workers <number>` | all processor cores | Sheets read at the same time. |
| `--config <file>` | `config.json` next to the template | Reading settings to use instead. Use this, for example, for a faster "fast pass" configuration (see below). |
| `--evaluation <file>` | none | An `evaluation.json` answer key, to score the sheets. |
| `--bubble-model <file>` | none | A learned bubble reader model (`.onnx`), if you have one. |
| `--icr-model <file>` | none | A learned handwriting reader model (`.onnx`), if you have one. |
| `--save-images` | off | Also save the aligned and marked images of each sheet. Uses much more disk space and time. |
| `--resume` | off | Continue a stopped run in the same `--out` folder. Sheets already read are kept; the rest, and the sheets that ended in an error, are read. |
| `--sheet-timeout <seconds>` | 300 | A sheet still being read after this long is skipped and written as an error row, so one bad file cannot stall the run. 0 means no limit. Works with 2 or more workers. |

**What you get:** `results.csv` (one row per sheet: file, status, score, the fields needing review, then one column per answer) and `results.jsonl` (the full details). Rows are written as soon as each sheet is read, so a stopped run loses nothing. Rows with status `needs_review` or `error`, or a non-empty `needs_review` column, should be checked by hand. To use the review screens, scan the same folder as a job in the app instead.

**Fast pass (test mode):** a configuration file with `"speed_params": {"fast_pass": true}` makes each sheet first be read at half size; a sheet is kept from that quick read only when every bubble is clearly empty or clearly filled and nothing needs review, otherwise it is read again normally. It saves time on clean timing-mark sheets. It is off by default; switch it on in the template editor (**Options › Fast pass**), or for a command-line run pass a config.json that turns it on with `--config`. Templates that use OCR, handwriting, image zones, border fitting or other advanced steps are always read at full size.

##### How to run a bulk read from the command line

1. Open a Command Prompt in the program folder (in File Explorer, type `cmd` in the address bar and press Enter).
2. Run, for example:
   ```
   OMRChecker.exe --bulk D:\scans\batch-01 --template D:\exam\template.json --out D:\results\batch-01 --workers 8
   ```
3. Wait for it to finish. The results folder is printed at the start ("Results -> ...").
4. If the run was stopped (power cut, closed window), run exactly the same command again with `--resume` added.

#### Running from source (for IT staff)

The station can also run from a copy of the program's source code on Windows, Linux or macOS, using Python 3.8 or newer.

1. Open a terminal in the source folder.
2. Create and activate a virtual environment:
   ```
   python -m venv .venv
   .venv\Scripts\activate          (Windows)
   source .venv/bin/activate       (Linux / macOS)
   ```
3. Install the requirements:
   ```
   pip install -r requirements.api.txt
   ```
4. Start the server and app:
   ```
   python -m src.api --host 0.0.0.0 --port 8000 --data-dir ./omr_data --workers 8
   ```
   Then open `http://localhost:8000/`. Note that this way the default port is **8000**, not 8765, and the data folder is `./omr_data` in the current folder.

   Or start the same launcher window as the exe (needs Tk):
   ```
   python packaging/omr_desktop.py
   ```
   It accepts every option listed above, including `--bulk` and `--selftest`.

For printed-text reading (OCR zones) install Tesseract separately; without it, OCR zones go to the review queue and everything else works. When run from source, the API key is set with the `OMR_API_KEY` environment variable.

### Signing in, accounts and roles

A new station has **no sign-in**: anyone who can open the address can use it. That is fine on a single PC. As soon as other people or other devices can reach the station, turn sign-in on.

#### Turning sign-in on

1. Click **User** in the top bar.
2. Click **Create the administrator account…**.
3. The form **Create the administrator account** appears. Fill in **User name**, **Full name** (optional), **Password** and **Password again**, then click **Create account**.
4. You are signed in at once. From now on everyone has to sign in to use the station.

(**Not now** closes the form without creating the account.)

Rules:

- User names: 2 to 40 characters; letters, digits, `.`, `_`, `-` or `@`. Not case-sensitive.
- Passwords: at least 8 characters.
- Ten wrong passwords in a row lock that user name for five minutes ("Too many failed attempts; try again in a few minutes").
- You stay signed in for 30 days on that browser, unless you sign out.

To turn sign-in off again you must stop the program and delete `accounts.sqlite3` from the data folder. That deletes every account.

#### Signing in

The **Sign in** page shows the program name and icon. Enter **User name** and **Password** and click **Sign in**. If your session ends (password changed elsewhere, account disabled, or 30 days passed), the page asks you to sign in again ("Your session ended. Sign in again.").

#### Creating your own account

Unless registration is closed, the sign-in page has **New here? Create an account**. Fill in the form and click **Create account**. Depending on the station's setting you are either signed in at once, or you see "Account created. An administrator has to approve it before you can sign in." Until then, signing in says "This account is waiting for an administrator's approval".

#### Roles

| Role | What they can do |
| --- | --- |
| **admin** | Everything, plus managing users and registration, and browsing and scanning any folder on the server. |
| **reviewer** | Scan uploaded files, scan the server folders an admin allows for them, review, correct, verify and export results, and work with templates. Cannot manage users. |

Every correction is recorded under the signed-in user's name. The station always keeps at least one active administrator: the last one cannot be demoted, disabled or deleted.

#### The User button and account menu

The **User** button is at the right of the top bar.

**When sign-in is off**, it opens the **User** dialog:

| Option | What it does |
| --- | --- |
| **Your name (recorded with every correction)** | Type your name and click **Save**. Every correction you make in the Review queue and Results is recorded with this name, so you can see later who changed what. It is remembered by this browser. The button then reads "User: <name>". Without a name, corrections are recorded as `local`. |
| **Create the administrator account…** | Turns on sign-in (see above). |

**When sign-in is on**, the button shows your name (👤 *name*) and opens your account menu, which says "Signed in as *name* · *role*":

| Button | What it does |
| --- | --- |
| **Change password** | Enter **Current password**, **New password (8+ characters)** and **New password again**, then **Change**. Your other sessions (other browsers, phones) are signed out. |
| **Manage users** | Admins only. Opens the **Users** dialog below. |
| **Sign out** | Signs you out of this browser. |

#### Managing users (admins)

The **Users** dialog has:

**Registration** — how new people get accounts:

| Choice | Meaning |
| --- | --- |
| **Anyone can register; an admin approves** (default) | People create their own account; it waits until an admin clicks **Approve**. |
| **Anyone can register and sign in at once** | No approval. Only use this on a network you trust. |
| **Closed: only admins add accounts** | The "Create an account" link disappears from the sign-in page. |

The change is saved as soon as you pick it.

**User table**, one row per account:

| Column | What it shows / does |
| --- | --- |
| **User** | The user name, and the full name below it. |
| **Role** | Change between **reviewer** and **admin**. Saved at once. |
| **Status** | **active**, or **waiting for approval** (the row is highlighted). |
| **Last sign-in** | Date and time, or "never". |
| **Server folders** | For admins: "all". For reviewers: a button showing how many folders they may scan ("none" means they can only upload files). Click it to edit the list. |
| Buttons | **Approve** (for waiting accounts), **Disable** (the user can no longer sign in and is signed out everywhere; click **Approve** to let them back in), **Reset password** (asks for a new password of 8+ characters), **Delete** (asks first; the person's corrections stay in the audit log). You cannot disable or delete your own account here. |

**Add a user**: type a user name and a password (8+), choose **reviewer** or **admin**, and click **Add**. The account is active at once, even when registration is closed.

**Server folders for a user**: this decides which folders on the station PC (or network drives) a reviewer may browse and scan in the Jobs tab. They can use those folders and everything inside them, and nothing else. Paste a full folder path and click **Add**, or click **Browse…** to pick one, remove a folder with ✕, then **Save**. With no folders, the user can only upload files from their own computer.

#### How to set up accounts for a team

1. Turn on sign-in by creating the administrator account.
2. Open **User** > **Manage users**.
3. Choose the **Registration** setting you want (closed is simplest for a fixed team).
4. Under **Add a user**, add each colleague as **reviewer**; make one or two trusted people **admin** as well, so you are never locked out.
5. For colleagues who start jobs from folders on the station PC, click their **Server folders** button and add the scan folders.
6. Tell each colleague their user name and starting password, and ask them to use **Change password** after their first sign-in.

### API key

The API key is for **programs** that talk to the station (scanning scripts, the Python/Java/Go client libraries, other systems), not for people. People use accounts.

- The station requires a key only when it was started with `--api-key <secret>` (or the `OMR_API_KEY` environment variable).
- The **API key** button in the top bar lets you type the key into this browser. Leave it empty if the server has none. It is remembered by this browser.
- If the station needs a key and the browser does not have it, the app asks "This server requires an API key:" the first time it is needed.
- With sign-in turned on, a signed-in person does not need the key; programs can keep using the key.

### The top bar

From left to right:

| Item | What it is |
| --- | --- |
| Icon and name | The program's icon and name (from branding). |
| **Scan**, **Review queue**, **Results**, **Jobs**, **Templates** | The five tabs (see below). The **Review queue** tab shows a badge with the number of items waiting for review. |
| **User** | Your name for corrections, or your account menu when sign-in is on. |
| **API key** | Enter the API key for this browser (see above). |
| **API docs** | Opens the technical reference of the station's programming interface in a new tab. For IT staff and developers. |

The address in the browser follows the tab, for example `http://127.0.0.1:8765/#results`, so you can bookmark a tab or send a colleague a link straight to it.

### The tabs at a glance

| Tab | What it is for |
| --- | --- |
| **Scan** | Quick reading of a few sheets: pick a template, drop images or PDFs, and see each sheet's answers right away. Also has **📷 Live camera** to read sheets from a camera, and **Export CSV** / **Clear list** for the sheets in this list. |
| **Review queue** | Works through single items the engine was unsure about (a faint mark, a double mark, an unreadable code), one at a time or in a grid, with keyboard shortcuts. |
| **Results** | Browse whole graded sheets with their overlay, correct bubbles and values, verify sheets, regrade, check accuracy and export. |
| **Jobs** | Start and watch large batches (bulk jobs) from uploaded files or from a folder on the server; pause, resume, rescan errors. |
| **Templates** | Create, upload, generate and edit templates: the description of where every bubble, field and zone is on your sheet, how to align the page, how to score it. |

A typical day: set up a template once in **Templates**, start a batch in **Jobs**, clear the **Review queue**, check and correct in **Results**, and export. Each tab has its own section later in this guide.

### Name and icon (branding)

The program's name and icon come from one folder, `branding`, inside the program folder (next to `OMRChecker.exe` in the portable folder, or in the source folder).

| File | What it holds |
| --- | --- |
| `branding.json` | `name`: the name shown in the browser tab, the app's top bar, the sign-in page, the launcher window, the installer and its shortcuts (default "UNIC OMR Scanner"). `publisher`: the publisher shown by the installer (default "UNIC"). `icon`: the icon file name (default `icon.png`). |
| `icon.png` | The icon. Use a square picture, 256 × 256 pixels or larger. |

Example `branding.json`:

```json
{
  "name": "City College OMR",
  "publisher": "City College Exam Office",
  "icon": "icon.png"
}
```

If the file is missing or broken, the default name and icon are used. Empty values keep the default.

#### How to change the name or icon

1. Stop the program (**Quit** in the launcher).
2. Edit `branding\branding.json` and/or replace `branding\icon.png`.
3. Run `python packaging\apply_branding.py` from the source folder. It writes `icon.ico` (the Windows icon used for the exe, the installer and shortcuts) and `branding.iss` (the name used by the installer). (`apply_branding.py --draw` redraws the default icon.)
4. Start the program again.

What changes when:

| Change appears in | When |
| --- | --- |
| Browser tab title and icon, the app's top bar, the sign-in page, the launcher window title and icon | After a **restart** of the program. No rebuild is needed, whether you run from source or an already built portable folder (edit the `branding` folder next to `OMRChecker.exe`). Reload the browser page afterwards. |
| The icon of the `OMRChecker.exe` file itself, the installer, its name in Windows Settings and the Start menu / desktop shortcuts | Only after the program and installer are **rebuilt** (and reinstalled). |

The program file (`OMRChecker.exe`), the install folder (`OMRChecker`) and the data folder (`%LOCALAPPDATA%\OMRChecker`) always keep the name **OMRChecker**, whatever name you choose. This is on purpose: renaming the program never moves or hides anyone's results.

## 2. Scan, Jobs, Review queue and Export

These four parts of the app take you from a pile of scanned sheets to a finished spreadsheet:

1. **Scan** reads a few sheets right away and shows the result for each, which is handy for trying out a template.
2. **Jobs** reads large batches (hundreds or thousands of sheets) in the background.
3. **Review queue** shows the single answers the scanner was not sure about, one at a time, so a person can confirm or correct them quickly.
4. **Export** writes the results to Excel, CSV, PDF or a database.

Every sheet read on the Scan tab or in a job is stored on the server. You can open it again on the **Results** tab, which is covered in its own section.

---

### The Scan tab

The Scan tab is for quick reads: a handful of sheets, a test of a new template, or a single late sheet. Each file is sent to the server and read straight away. The tab reads up to three files at the same time.

#### Controls

| Option | Default | What it does |
|---|---|---|
| **Template** | first template in the list | The template used to read the sheets you drop. Pick this first: the app refuses files until a template is chosen ("Pick a template first"). A sheet keeps the template that was selected when you dropped it, so changing the template later does not affect sheets already in the list. |
| **Drop sheet images or PDFs here** / *or click to choose files* | — | Drag files onto the box, or click it (or press Enter or Space when it has focus) to open a file chooser. Images (PNG, JPEG, TIFF, BMP, WebP) and PDFs are accepted. You can choose many files at once. |
| **📷 Live camera** | — | Opens the live camera page in a new browser tab, already set up for this server and the chosen template. It reads sheets from a camera with the reader built into the browser. Phones only allow camera access over HTTPS, so this works on a phone only through a secure (https) address such as the remote access link. |
| **PDF options** (fold-out) | template default | Settings for PDF files only; see below. |
| **Export CSV** | — | Downloads `scan_results.csv` with one row per sheet read in this list: file, scan id, status, score, the names of the items that need review, and every answer column. Only the sheets in the list on screen are included. For a full export with all options, use **Export…** on the Jobs or Results tab. |
| **Clear list** | — | Empties the list on screen. Sheets still waiting or being read stay. Nothing is deleted on the server; the results remain available on the Results tab. |

#### PDF options

The same two settings appear on the Scan tab and on the New bulk job panel. Leave them on the template default unless a PDF reads badly.

| Option | Default | What it does |
|---|---|---|
| **PDF render DPI** (`pdf_params.pdf_dpi`) | *template default* | Resolution PDF pages are turned into images at. Choices: *template default*, *auto (the PDF's own resolution)*, 150, 200, 300, 400, 600. 200–300 suits most scans. Too low and small bubbles blur together; too high makes reading slower and uses more memory for no gain. |
| **PDF pages** (`pdf_params.pdf_page`) | *template default (1)* | Which pages of each PDF to read: `1`, `2-4`, `3-` (page 3 to the end) or `all`. Each page read becomes its own sheet result. If your PDFs hold many sheets each, set this to `all`, or only the first page will be read. |

#### What you see after reading

- **The counters** above the list show the total number of sheets and how many are in each state.
- **The sheet list** shows a coloured status chip, the file name and, when the template has an answer key, the score. The states are:
  - *queued*: waiting to be sent;
  - *reading*: being read now;
  - *ok*: read with confidence;
  - *needs review*: at least one answer needs a person to look at it;
  - *error*: the sheet could not be read (for example the page could not be aligned). Select it to see the reason.
- A multi-page PDF adds one line per page.
- Click a sheet, or use the **Up/Down arrow keys** (when no text box has focus), to show its details on the right.

The details panel shows:

- the file name, its status, its score, and how long the server took to read it;
- **Review N item(s)**: opens the Review queue with just this sheet's items;
- **Open in Results**: opens the sheet on the Results tab, where you can see the full overlay and correct it;
- **JSON**: the raw result, in a new browser tab (mostly for technical staff);
- the marked sheet image, and a table of every field and zone with its **Type**, **Value** (*(blank)* when nothing was marked), **Confidence** (0 to 1; the cell shades from red for low through amber to green for high) and **Flags**. Rows that need review are highlighted.

Sheets read on the Scan tab are kept together in a job called **Single uploads** (one per template), shown on the Jobs tab with the source "uploads". That means you can review, browse and export them like any other job. The Scan tab keeps all images of these sheets.

#### The live camera page and exam names

On the live camera page, sheets the browser cannot read confidently can be sent to the server's review queue with **Send to server for review**. The page has an **Exam / olympiad** box: sheets sent with the same exam name and template are collected into one job of that name, so you can review and export each exam separately. With no name they go into the job "Camera / phone uploads". **Source label for the exam** sets how that job's source is described on the Jobs tab (default "camera / single uploads").

#### How to try a template on a few sheets

1. Open the **Scan** tab and choose the **Template**.
2. Drop three or four typical sheets on the box, including a messy one.
3. Click each sheet in the list and compare the values in the table with the paper.
4. If answers are wrong or many items are flagged, click **Open in Results** to see where the bubbles were placed, then adjust the template on the Templates tab and try again.

---

### The Jobs tab

Use a job for anything more than a few dozen sheets. A job runs on the server in the background: you can close the browser, and the job keeps going. Jobs run one after another; the running job spreads its sheets over several reader processes at once.

#### New bulk job

| Option | Default | What it does |
|---|---|---|
| **Template** | first template | The template every sheet in the job is read with. |
| **Name** | *optional* (empty) | A name shown in the Jobs list, the Review and Results filters and exports, for example "Physics mock 2026 day 1". Without one, the job is shown by the first 8 characters of its id. Naming jobs makes them much easier to find later. |
| **Keep images** | *Only sheets that need review* | Which sheet images the server stores. *Only sheets that need review* stores the aligned image only for flagged sheets. *All sheets* stores every image: the most disk space, but sheets can be shown even if the original scans are later moved or deleted. *None: draw review crops from the original files* stores nothing and is the fastest; review crops and the Results view are rebuilt from your original scan files. With *None*, do not move or delete the scan folder until review and checking are finished (if it does move, use **Path remap** on the Results tab). |
| **Worker processes** | *auto* (one per CPU core) | How many sheets are read at the same time. Leave it on auto in most cases. Lower it to keep the computer responsive for other work. More workers than CPU cores is allowed, and the app warns you ("… more workers than cores only helps when reading from a slow disk or network share, and uses more memory"). |
| **Read ahead (files)** | 64 | How many files are loaded into memory ahead of the readers so they never wait for the disk. About 0.2 MB per JPEG scan. Raise it for slow network shares. Set **0** to turn it off on machines with little memory. |
| **PDF options** | template default | **PDF render DPI** and **PDF pages**, as on the Scan tab. |

Then choose where the sheets come from. Use one of the two boxes.

**Upload files**: copies the sheets from your computer to the server. Use it when the server is another machine, or for a few hundred sheets.
- The file chooser takes images and PDFs; other files are ignored.
- Tick **pick a folder** to choose a whole folder instead of single files. Files in subfolders are included, and the subfolder name becomes part of the file name.
- Files are sent in groups of 25, and the line next to **Start job** shows "Uploading N/M…". Keep the page open until the upload has finished and the job has started.
- Each file may be at most 100 MB (the server's upload limit).

**…or a folder on the server**: reads the sheets where they already are, so nothing is copied. This is the fastest way for large batches. The folder must be on the machine running the app (for the portable app, that is this PC) or on a network drive it can reach. Examples: `D:\scans\batch-01`, `\\server\scans\batch-01`, `/srv/scans/batch-01`.

| Option | Default | What it does |
|---|---|---|
| Folder box | empty | Type or paste the folder path. As you type, the app checks it and shows "✓ 1,234 images, 2 PDFs" or "✗" with the reason (folder not found, not allowed, and so on). |
| **Browse…** | — | Opens **Choose a folder on the server**: a folder browser of the server's drives and folders. **Drives** goes back to the list of drives, **← Back** to the previous folder, **↑ Up** to the parent folder. Each folder shows how many images and PDFs are directly inside it. **Select this folder** puts the path in the box. |
| **Recent folders…** | — | The last 10 folders jobs were started from. Pick one to fill the box. |
| **Include subfolders** | ticked | Also reads the sheets in every folder inside this one. Untick to read only the files directly in the folder. |

An administrator can limit which server folders may be read. In that case the browser shows only the allowed folders, and with none allowed for your account it says so: upload the files instead, or ask an administrator.

Press **Start job**. A message confirms "Job … started with N files", and warnings (such as the worker warning) appear for a few seconds.

#### The Jobs list

The list shows the 50 newest jobs. While any job is queued or running and the tab is open, it refreshes itself every 1.5 seconds; **Refresh** updates it by hand.

| Column | What it shows |
|---|---|
| **Job** | The job's name (or short id), and underneath where the sheets came from: the server folder, "upload", or for single uploads and camera sheets the source label ("uploads", "camera / single uploads"). |
| **Template** | The template the job reads with. |
| **State** | See the states below. |
| **Progress** | A bar and "N / M files · P%". A multi-page PDF counts as one file here. |
| **OK** | Sheets read with no doubts. |
| **Review** | Sheets that have at least one item waiting for review. |
| **Error** | Sheets that could not be read. |
| **Sheets/s** | Reading speed: sheets per second. It updates every second, also while the readers are still starting. |
| **Time** | Time spent reading so far, with the start-up of the readers included and pauses left out. A resumed job adds to its earlier time. |
| **ETA** | Estimated time left, shown only while the job is running. |
| (buttons) | The actions below. Only the ones that make sense for the job's state are shown. |

#### Job states

| State | Meaning |
|---|---|
| *uploading* | Files are still being uploaded from your browser. |
| *queued* | Waiting for the job ahead of it to finish. |
| *running* | Being read now. |
| *paused* | Stopped on request. The sheets read so far are kept; **Resume** reads the rest. |
| *completed* | Every file has been read. |
| *interrupted* | The server stopped (closed, crashed or the PC restarted) while the job was running. Normally interrupted jobs restart by themselves when the server starts again; if this has been turned off on the server, press **Resume**. |
| *failed* | The job stopped because of a problem, for example the reader processes kept crashing. **Resume** continues it. |
| *cancelled* | Stopped for good with **Cancel**. Sheets already read keep their results. |

#### Job actions

| Button | Shown when | What it does |
|---|---|---|
| **CSV** | always | Downloads a quick CSV of the job: file name, page, scan id, status, score, needs review, reviewed, error and every answer column. It always uses the corrected values. |
| **Export…** | always | Opens the Export dialog limited to this job (see Export below). |
| **Results** | the job has sheets | Opens the Results tab filtered to this job. |
| **Review** | sheets need review | Opens the Review queue filtered to this job. |
| **Pause** | queued or running | Lets the sheets being read now finish, then stops. "Pausing: sheets being read finish first." |
| **Resume** | paused, interrupted or failed | Reads the remaining sheets. Sheets that already have results are not read again. |
| **Rescan errors** | the job has error sheets and is completed, paused, interrupted or failed | Reads the sheets that ended in an error again, for example after fixing the template or putting back a missing file. Sheets someone already reviewed are kept. For a paused, interrupted or failed job it also reads the rest. Not available for the Single uploads and camera jobs. |
| **Cancel** | uploading, queued, running, paused or interrupted | Asks first. The remaining sheets will not be read; the ones already read keep their results. A cancelled job cannot be resumed, so to stop for now and continue later use **Pause**. |
| **Delete** | finished jobs | Deletes the job and all its sheet results: values, corrections, review items and stored images. You must type `delete` to confirm. It cannot be undone. Scans in a server folder are not touched; uploaded copies are deleted. The deletion is recorded in the audit log with your name. |

#### When something goes wrong while reading

The app is built so that one bad sheet does not stop a job of thousands:

- **A sheet that crashes the reader, or hangs.** If a reader process crashes, or is still on one sheet after 300 seconds, the readers are restarted. The sheets they had are read again one at a time, and the one that fails on its own is stored as an error ("Reading this sheet took longer than 300 s; it was skipped", or "…crashed the reader"). The rest of the job carries on. Use **Rescan errors** later to try those sheets again. The time limit is a server setting (`OMR_SHEET_TIMEOUT_S`, 0 = no limit; the desktop exe in bulk mode has `--sheet-timeout`).
- **Readers that keep crashing.** After 20 restarts in a row the job stops as *failed* ("The reader processes kept crashing; Resume continues the job").
- **The PC or server stops.** Each job remembers its list of files. When the server starts again, unfinished jobs are queued again and continue where they left off, skipping sheets that already have results. A sheet read again after a crash replaces its earlier result, so you do not get duplicates. (A server administrator can turn the automatic restart off with `OMR_RESUME_JOBS=0`; the jobs then show as *interrupted* until you press **Resume**.)

#### How to read a large batch from a folder

1. Copy or scan the sheets into one folder on the server PC or a network share.
2. On the **Jobs** tab choose the **Template** and type a **Name**.
3. In **…or a folder on the server**, click **Browse…**, find the folder and click **Select this folder**. Check that the line underneath shows the number of images you expect.
4. Leave **Keep images** on *Only sheets that need review* and **Worker processes** on *auto*.
5. Click **Start job** and watch the **Progress**, **Sheets/s** and **ETA** columns.
6. When it is done, click **Review** to clear the flagged items, then **Export…**.

#### How to stop for the day and continue tomorrow

1. Click **Pause** on the running job. The state becomes *paused* once the sheets being read have finished.
2. Close the app if you like.
3. Next day, start the app, open **Jobs** and click **Resume**.

---

### The Review queue tab

The Review queue lists every single thing the scanner was unsure about across all your sheets. An **item** is one of these:

- one bubble field on one sheet (for example question 17 on sheet 0042);
- a zone (barcode, QR code, printed or handwritten text box);
- a joined value such as a roll number made of several bubble columns (a *custom label*);
- a cross-field check whose sources disagree;
- a whole-sheet problem, such as a sheet whose orientation is unclear or a blurry photo.

For each item you see a close-up crop of the sheet and the value the scanner read. You either confirm it or type the right value. The number on the **Review queue** tab shows how many items are pending across all jobs.

The queue loads by itself when you open the tab. New items from a running job are added to the end of the list every few seconds, and shown with a green "N new items" mark.

#### Filters

| Option | Default | What it does |
|---|---|---|
| **Template** | all | Only items from sheets read with this template. Choosing a template refreshes the field list. |
| **Field / zone** | *All (N)* | Only one field, zone or joined value, for example `Roll_no`. The numbers show how many items are pending for each. Useful for going through one question across all sheets. |
| **Kind** | *All* | *Bubble fields*, *Zones*, *Cross-field checks*, *Custom labels* (joined values such as roll numbers), *Whole sheet*. |
| **Job** | *All* | Only items from one job. Each entry shows the job name, its date and the number of pending items. The queue reloads when you change it. |
| **Flag** | *All* | Only items read with this flag (see the flag table below), with the number pending for each. The queue reloads when you change it. |
| **Scan** | *any* | Only one sheet, by its scan id. This is filled in for you when you click **Review N item(s)** on the Scan tab. |
| **Order** | *Highest risk first* | *Highest risk first* shows whole-sheet problems first, then failed cross-field checks, then joined values (roll numbers and the like), then zones, then single bubble fields. *Oldest first* goes in reading order. |
| **Wide context** | off | Shows a larger area around the field in the crop, so you can see neighbouring rows. Turn on when you need to judge whether the page was aligned correctly. |
| **Grid** | off | Shows many crops at once as a grid; see below. |
| **by** | *field* | For the grid: *field* shows the same field from many sheets; *flag* shows every item with the same flag (the one chosen in the **Flag** filter, or the current item's first flag). |
| **Reload queue** | — | Loads the queue again with the current filters. Skipped items come back. Use it after changing **Template**, **Field / zone**, **Kind** or **Scan**, which do not reload on their own. |
| **Accept all as read…** | — | Accepts every pending item under the current filters as read; see below. |

The progress line at the right shows "Reviewed N this session · M pending · K skipped", and when colleagues are reviewing at the same time, how many items you stepped over because they were "done by" them.

#### The review card

For the current item the card shows:

- the **crop** of the sheet (small crops are enlarged so they are easy to read), or "No image stored for this sheet";
- the field or zone **name** and its **type**;
- the sheet's file name and a short scan id (click it to open the raw result);
- **conf**: the confidence from 0 to 1, shaded red (low) to green (high);
- the **flags** the item was read with;
- **Read as:** the value the scanner read (*(blank)* when nothing), and if you have already saved it, **you saved:**;
- the reasons, when a rule failed (for example "gap at position 2");
- for joined values, **Columns:** listing each column. Columns with a double mark or a problem are highlighted, and empty columns are dimmed. Type a space for a blank column;
- the **value box**, with the read value already selected so you can simply type over it;
- for bubble fields, one **option button** per bubble with a bar showing how much it is filled and its shortcut (Alt+1, Alt+2, …). Click to choose; Shift+click to add or remove one of several marks;
- for cross-field checks and zones with a fallback, buttons with the value each source read (for example the barcode and the bubbled digits). Click one to use it;
- **Save (Enter)**, **Skip (Tab)** and **Back**;
- **Reader details** (fold-out): technical details of how the item was read.

What **Save** does:

- If you keep the value as read, the item is recorded as accepted.
- If you change it, the new value is recorded as a correction, with your name and the time. The scanner's original reading is kept in the sheet's history.
- The score, joined values and the template's rules are worked out again for that sheet.
- An empty value means "blank".
- For bubble fields the value must be made of the sheet's options. Lower-case letters are turned into capitals for you. Several marks are put in the sheet's order (typing `CA` saves `AC`). An impossible value is refused with a message listing the allowed options.
- Saving happens in the background, so you can keep going. If a save fails, a message names the item and it stays pending.

**Skip** leaves the item pending and moves on. Skipped items stay in the queue for later (and for colleagues). They come back when you press **Reload queue**.

**Working together.** Several people can work through the same queue. Items a colleague has already decided are stepped over automatically. If you are looking at one when it happens, a notice says "Done by NAME (value …). Saving here changes their decision." with a **Next** button. An item whose sheet was deleted or regraded meanwhile says it is no longer pending.

#### Keyboard shortcuts

| Key | What it does |
|---|---|
| **Enter** | Save: accept the value as read, or save your correction. |
| Typing | Replaces the value. Leave the box empty for blank. |
| **Tab** | Skip to the next item. |
| **Shift+Tab** | Go back to the previous item. |
| **Esc** | Reset the box to the value as read (or to what you saved). |
| **Alt+1** … **Alt+9** | Choose option 1 to 9. Pressing the chosen option again clears it. |
| **Alt+Shift+1** … **Alt+Shift+9** | Add or remove that option as one of several marks. |
| **Up/Down arrows** | (On the Scan tab) move through the sheet list. |

#### Grid: accept many at once

Tick **Grid** to see up to 30 crops of the same field (or the same flag, with **by** *flag*) from different sheets, each with the value read underneath (∅ means blank). All crops start ticked.

1. Look over the grid. Click a crop (or its tick box) to untick any that read wrong.
2. Click **Accept ticked**. The ticked items are accepted as read.
3. The unticked ones open one by one in the normal card so you can correct them. **Open** on a tile jumps straight to that item.

This is the fastest way through a common flag such as `weak_mark`, where most items are in fact right.

#### Accept all as read

**Accept all as read…** accepts every pending item under the current filters with the value the scanner read. A dialog shows how many items it will accept.

- Nothing is deleted. Each item is recorded as "accepted in bulk" with your name and the time, so it can be traced and changed later on the Results tab.
- Items that arrive after you opened the dialog stay in the queue.
- It runs in the background, with a progress box in the corner that has a **Stop** button. Only one bulk accept can run at a time.

Use it carefully. A good use is a flag you have checked a sample of and trust, for example: filter **Flag** = `weak_mark` and **Job** = the job, look through a grid or two, then accept the rest. Accepting all items without filters means nobody has checked them.

#### How to clear the queue for one job

1. On the **Jobs** tab click **Review** on the job (or choose it in the **Job** filter).
2. Leave **Order** on *Highest risk first*, so whole-sheet problems come first. A sheet that is misaligned produces wrong answers everywhere, so it is better fixed on the Results tab than item by item.
3. For each item, look at the crop and press **Enter** if the value is right, or type the right value and press **Enter**.
4. When the same flag keeps coming up, tick **Grid** to accept the correct ones in one go.
5. When the card says "Queue finished. Nice work.", the job is ready to export.

#### What each review flag means

A flag says why an item looks doubtful. Which flags send an item to the queue is set per template (the **review flags** list in the template's options, `review_params.review_flags`). The flags below marked "always" are sent to review whatever that list says. Flags not sent to review are still recorded and can be seen on the Results tab and exported.

**Bubble fields**

| Flag | To review by default | What it means | What to check on the sheet |
|---|---|---|---|
| `multi_marked` | yes | More than one bubble was marked in a question that takes one answer. | Did the student really mark two? Is one an erasure, a stray stroke or a crossed-out answer? Type the single intended answer, or keep both letters if the double answer should stand. |
| `empty` | no | Nothing was marked. | Normally fine: blank answers are common. If added to the review list, check for very light pencil marks. |
| `ambiguous_threshold` | yes | The bubbles in this row have no clear gap between dark and light, so the scanner could not tell marked from unmarked with confidence. | Look at every bubble in the row: are they all faintly marked, all erased, or is the area dirty or shaded? |
| `low_confidence` | yes | The weakest bubble in the field is close to the line between marked and unmarked. | Is a bubble half-filled, very light, or partly erased? |
| `weak_mark` | yes | A bubble read as marked has little ink inside its circle: a partial fill, a tick or a dot, or the bubble position is slightly off. | Is it a real answer (tick, small dot) or a stray touch? If many sheets show it in the same place, the template position may be off (see Results). |
| `possible_missed_mark` | yes | A bubble read as unmarked has a lot of ink inside its circle. | Is there a mark the scanner missed (light pen, a colour the scanner drops out), or is it a printed letter or a smudge? |
| `model_disagrees` | yes | The trained bubble model and the normal reader disagree (only when a bubble model is set up). | Look closely at the marked and nearly marked bubbles; one of the two readers is wrong. |
| `kept_darker_mark` | no | Several bubbles were marked and only the clearly darkest was kept (the "keep darker mark" rule). | If added to review: check that the lighter mark really is an erasure. |
| `faint_second_mark` | no (the field is also `multi_marked`, which is) | A second, lightly filled bubble turned the answer into a double mark (the "strict double marks" rule). | Is the lighter bubble a real second answer or a poor erasure? |
| `border_slide` | yes | The printed box around the block would have moved the bubbles too far (close to a whole bubble), so that correction was not used. | Is the block's bubble grid sitting correctly on the printed bubbles? Open the sheet on the Results tab with the overlay on. |
| `rectify_failed` | no | The printed box around the block could not be fitted (not found, too distorted, or a worse fit), so the page alignment was kept for this block. | If added to review: check the answers of the block line up with the printed bubbles. |

**Joined values (roll numbers and other grouped columns)**

| Flag | To review | What it means | What to check |
|---|---|---|---|
| `group_issue` | always | One or more columns of the joined value have a problem (for example a column that looks blank but shows ink). | The highlighted columns under **Columns:**. Type the whole value, using a space for a blank column. |
| `multi_marked` | always | One column has two marks. | Which digit or letter was meant in the highlighted column. |

**Zones (barcode, QR code, printed text, handwriting)**

| Flag | To review | What it means | What to check |
|---|---|---|---|
| `not_found` | always | No code or text was found in the zone. | Is the barcode or QR code there, inside the zone, and not torn, folded or covered? Type the value printed on the sheet. |
| `multiple_symbols` | always | More than one code was found in the zone. | Which code belongs to this field. |
| `low_confidence` | always | The value was read, but with low confidence. | Compare the value with the printed or written text. |
| `pattern_mismatch` | always | The value does not have the expected form (for example 6 digits). | Is it a misread, or a genuine wrong code? |
| `engine_unavailable` | always | The reading engine needed for this zone is not installed or could not start. | Type the value by hand. Tell whoever set up the app, because every sheet will show this. |
| `no_icr_model` | always | A handwriting zone has no handwriting model set up. | Type the value by hand; ask whoever set up the template. |
| `read_error` | always | Reading the zone failed with an error. | Type the value by hand. If it happens on many sheets, report it. |
| `engine_disagree` | always (when the template asks for it) | Two text-reading engines read different text. | Which reading is right. |
| `low_char_confidence` | always | At least one character was read with low confidence. | Each character, especially look-alikes (0/O, 1/7, 5/S). |
| `decoded_by_fallback` | no | The code was read by a backup barcode reader. | Usually fine; only recorded. |
| `not_read` | no | The zone was not read on this sheet. | Only recorded. |

**Rules and cross-field checks**

| Flag | To review | What it means | What to check |
|---|---|---|---|
| `validation_failed` | depends on the rule (usually yes) | The value broke a rule set in the template, for example a range, a pattern or "no gaps". The reason is shown on the card. | Is the reading wrong, or did the student fill the value in wrongly? |
| `cross_check_failed` | always | Two reads of the same value disagree, for example the barcode and the bubbled roll number. Buttons show each source's value. | Which source is right on the paper. Click it, or type the value. |
| `fallback_used` | only when the check is set to review it | The first source was missing, so a later one supplied the value. | That the backup value is right. |
| `all_sources_missing` | yes, unless the check turns it off | None of the sources gave a value. | Type the value from the sheet. |

**Whole sheet** (Kind = *Whole sheet*; these come first in *Highest risk first* order)

| Flag | To review | What it means | What to check |
|---|---|---|---|
| `orientation_ambiguous` | always | The sheet fits almost equally well two ways round (for example upright and upside down). | Open the sheet on the Results tab: if the answers are clearly from the wrong way round, regrade or place the page by hand. |
| `registration_suspect` | always (when it appears) | The page may be aligned one row or one column off, or the bubbles do not sit on the printed bubbles. | On the Results tab, with the overlay on, check the circles sit on the printed bubbles. A sheet one row off reads every answer wrong. |
| `alignment_residual` | always (only when the template sets a limit) | Part of the page did not line up well with the template. | Look for folds, curled edges or a stretched photo in that part of the sheet. |
| `index_point_missing` | always | A required index point was not found on the sheet. | Is that corner of the sheet cut off, folded or covered? |
| `poor_image` | always | The image is too blurred, too low in contrast, or the bubbles are too small in the picture to read reliably. | Rescan or retake the photo if you can. Otherwise check every answer on the Results tab. |
| `too_few_marks` | always (only when the template sets a minimum) | Fewer bubbles were marked than expected. Often a pen the same colour as the print was dropped out with it. | Are there marks the scanner did not see? Check the colour settings of the template. |

Accepting a whole-sheet item only records that you checked the sheet. If answers are wrong because of the sheet problem, fix them on the Results tab.

---

### Export

The Export dialog writes results to a file or a database. It always writes the **corrected** values.

You can open it from:

- **Export…** on a job in the Jobs list: that job's sheets;
- **Export…** on the Results tab: the sheets matching the Results filters (template, job, view, field, flag, file name).

The top line, **Sheets:**, shows which sheets will be exported, for example "job 3f2a91c0 · template exam-2026", or "all results".

#### Format

| Format | What you get | Options |
|---|---|---|
| **Excel (.xlsx)** | An Excel workbook. Text fields are stored as text cells, so leading zeros (roll number `0123`) survive. Over 1,048,575 rows continue on extra sheets. | **colour flagged (amber) and corrected (green) cells**, on by default. |
| **CSV** | A text file that opens in Excel (saved as UTF-8 with the marker Excel needs for non-English letters). | **Leading zeros**: *Excel-safe ="0123"* (default; Excel shows 0123 and keeps the zero), *Text marker '0123*, or *Plain 0123* (for other programs; Excel will drop the zero). **Delimiter**: *comma* (default), *semicolon* (for Excel set to languages that use a comma as the decimal sign), *tab*. |
| **PDF** | A printable report. | **Layout**: *Results table*; *One page per sheet (image + overlay + values)*; or *Both*. One page per sheet is slow and large: the dialog warns above 200 sheets, and such exports are refused above 500 sheets (the server may allow up to 2,000). Narrow the filter first. |
| **SQLite file** | A single database file you can open with database tools. | **Table** name (default `omr_results`). |
| **Database (SQLAlchemy URL)** | Writes straight into a database server such as PostgreSQL. | **Table** name, and **URL** of the database, for example `postgresql+psycopg://user:password@host/db`. A server administrator can switch this format off. |

For SQLite and database exports, rows are matched on the scan id. Exporting again updates the existing rows instead of adding duplicates, so you can export after each review round.

The chosen format is remembered in your browser for next time.

#### Fields (answer columns)

The **Fields** table lists every answer column of the template, in template order:

| Column | What it does |
|---|---|
| tick box | Untick to leave the field out of the export. |
| **Field** | The field's name in the template. |
| **Column name** | The heading written in the export. Change it to suit your spreadsheet, for example `Roll_no` → `Roll number`. |
| **Type** | *text* (default), *int* (whole number), *decimal*, *date*, *bool* (yes/no). For *date*, a small box appears for the format the date is bubbled in, default `%d%m%Y` (day, month, year with no separators, e.g. `07032009`). |
| **↑ / ↓** | Move the column earlier or later. |

Type notes:

- Keep roll numbers, phone numbers and codes as *text*. Turning a value with a leading zero into *int* or *decimal* would lose the zero, so the export is refused unless **allow lossy number casts** is ticked.
- A value that cannot be converted (for example `AB` as *int*) is left empty, and the number of such values is shown as a warning.
- For *bool*, 1, Y, YES, TRUE, T and X count as true, and 0, N, NO, FALSE, F and blank count as false.

#### Sheet columns and per-field columns

**Sheet columns**: one column per sheet. Default ticked: `file_name`, `page`, `scan_id`, `status`, `score`.

| Column | Contents |
|---|---|
| `file_name` | The scan's file name. |
| `file_id` | The sheet's name in the app; for a page of a PDF it includes the page, e.g. `batch.pdf#page3`. |
| `page` | The page number within a PDF. |
| `scan_id` | The app's unique id for this sheet result. |
| `job_id` | The job the sheet was read in. |
| `template_id` | The template it was read with. |
| `status` | *ok*, *needs_review* or *error*. |
| `score` | The score, when the template has an answer key. |
| `source_path` | Where the original scan file is. |
| `verified_by` | Who verified the sheet on the Results tab. |
| `error` | Why the sheet could not be read. |
| `created_at` | When it was read. |

**Per field**:

| Option | Default | What it adds |
|---|---|---|
| **review status** | on | A `review_status` column: *verified*, *error*, *needs_review* (items still pending), *reviewed*, or *auto* (nothing needed checking). |
| **corrected (sheet)** | on | A `corrected` column: true when anything on the sheet was changed by a person. |
| **corrected (per field)** | off | A `<column>_corrected` column after each field. |
| **confidence** | off | A `<column>_confidence` column after each field. |
| **flags** | off | A `<column>_flags` column after each field, listing its flags. |
| **allow lossy number casts (drops leading zeros)** | off | Allows *int*/*decimal* types on values with leading zeros. Leave off unless you are sure. |

#### Preview, export and download

- **Preview** shows the number of sheets and the first rows exactly as they will be written, with any warnings (amber) or errors (red). Hover over a column heading to see its field and type.
- **Save to folder** (optional): a folder on the computer running the scanner. Type it or click **Browse…**. The export is then written straight into that folder, so a large export needs no download afterwards. If a file of that name is already there, the new one is called "*name* (2)". Deleting the export from the list later keeps the file in your folder. Leave the box empty to download from the browser as before. The app remembers the folder.
- **Export** runs the export on the server with a progress bar ("running · N / M rows"). When it is ready, click **Download *file name* (N rows)**, or, with a folder chosen, the dialog shows where the file was saved. The rows are written to the file as they are read, a few thousand at a time, so even very large jobs export without filling memory. A PDF is the exception: it is assembled in memory, which is why it has row and sheet limits.
- **Following an export.** An export runs on the server, not in the window: closing the Export window, going back or switching tabs does not stop it. The window has a **Cancel export** button while it runs. Every export, running or finished, is listed under **Jobs › Exports** with its progress, how long it took and who ran it. There you can **Download** it, **Cancel** one still running (the part written so far is removed) or **Delete** a finished one (a file saved into your own folder stays). An export that was running when the server was closed shows as **interrupted**; run it again.

#### Export profiles

A profile saves your choices (which fields, column names, order, types, sheet columns, per-field options and format settings) under a name, on the server, so everyone can reuse it.

- **Save profile…** asks for a name and saves the current settings (an existing name is overwritten).
- **— saved profiles —** loads a saved profile into the dialog.
- **Profile JSON** (fold-out) shows the profile as text. **Show current** fills it in, and **Apply JSON** loads text you pasted or edited. It also allows a few settings not shown in the dialog, such as a date output format (`outputFormat`), custom true/false values for *bool* fields, or `strictCast` (fail the export instead of leaving unconvertible values empty).

The database URL is not saved in the profile, only the table name.

#### How to export a finished job to Excel

1. On the **Jobs** tab click **Export…** on the job.
2. Choose **Format** *Excel (.xlsx)* and keep **colour flagged … and corrected … cells** ticked.
3. Rename columns under **Column name** and reorder them with ↑ / ↓ as your office needs them. Untick fields you do not want.
4. Click **Preview** and check the first rows and the warnings.
5. Click **Save profile…** and give it a name such as "Board results", so next time you only pick it from **— saved profiles —**.
6. Click **Export**, then **Download**.

#### How to send results to a database after every review round

1. Open **Export…** for the job and choose **Database (SQLAlchemy URL)** (or **SQLite file**).
2. Enter the **Table** name and the database **URL**.
3. Click **Export**. After the next review round, export again: rows are updated, not duplicated.

## 3. Results screen

The **Results** tab is where you look at graded sheets one whole sheet at a time. It is different from the **Review queue**: the queue shows single flagged items from many sheets, but Results shows one complete sheet with every bubble, field and zone drawn over the image. On this screen you can:

- find any sheet with filters,
- see exactly how the engine read it,
- correct it by clicking bubbles or typing values,
- mark it **verified**,
- re-read it with other settings (**Regrade…**),
- rescue sheets that could not be aligned,
- measure how accurate the reading is.

The screen has three parts: the **filter panel and sheet list** on the left, the **toolbar and canvas** in the middle, and the **side panel** with the sheet's details and values on the right. A line of keyboard hints runs along the bottom.

Every change you make is saved at once. Each change is recorded with your name (the name on the **User** button in the top bar) and the time.

### The filter panel and sheet list

The left panel decides which sheets are listed. When you change a filter, the list reloads from the first page and the first matching sheet opens.

| Option | Default | What it does |
|---|---|---|
| **Template** | All templates | Lists only sheets read with this template. Changing it also refreshes the counts and the **Flagged field** and **Flag** lists. |
| **Job** | All jobs | Lists only sheets from one job. Each job shows its name and how many files it has processed. When a job is chosen, sheets appear in the order the job read them; otherwise the newest sheets come first. Picking a job also lets you edit that job's own path rules in **Path remap**. |
| **Show** | All sheets | Picks which kind of sheet to list (see the table below). Each choice shows how many sheets it matches in brackets. |
| **Flagged field** | Any field | Lists only sheets where this field (or zone, or check) was flagged when it was read. The list shows each name with the number of sheets in brackets, most frequent first. |
| **Flag** | Any flag | Lists only sheets that carry this flag, for example `multi_marked` or `low_confidence`. Counts are shown in brackets. You can combine it with **Flagged field**, for example "q17 with `multi_marked`". |
| **File name** | empty | Lists sheets whose file name contains this text. The list updates shortly after you stop typing. Upper and lower case don't matter. |
| **‹ / ›** (pager) | page 1 | The list shows 100 sheets at a time. The text between the arrows says which ones you see, for example "101–200 of 5,320". It reads "No sheets match" when nothing fits the filters. |

#### Show choices

| Show | Lists |
|---|---|
| **All sheets** | Every sheet that matches the other filters. |
| **Flagged** | Sheets with at least one flagged field that nobody has verified yet. A sheet leaves this view as soon as it is verified. |
| **Not flagged** | Sheets with no flags, plus every verified sheet. |
| **Not reviewed** | Sheets that are not verified and still have something waiting for a person. |
| **Reviewed** | Verified sheets, plus flagged sheets whose flagged items have all been accepted or corrected. |
| **Corrected** | Sheets where a person changed at least one value. |
| **Errors** | Sheets the engine could not read at all, usually because the page could not be aligned. See [Failed sheets](#failed-sheets-sheets-that-could-not-be-aligned). |
| **Duplicates (same key)** | Sheets that share their primary key (for example a roll number) with another sheet of the same job. See [Duplicates](#duplicates-primary-key). |

#### Sheet list badges

Each row shows the file name. Hover over it to see the full name. Click a row to open the sheet. Small badges show its state:

| Badge | Meaning |
|---|---|
| **⚑3** (orange) | 3 fields were flagged when read. Hidden once the sheet is verified. |
| **✎** | A person corrected this sheet. |
| **✓** | Verified. Hover over it to see how many fields were flagged when read. |
| **error** | The sheet could not be read. |
| Small dot | The sheet needs review and is not verified. |

The open sheet is highlighted, and the list scrolls to keep it in view.

### The toolbar

| Control | Shortcut | What it does |
|---|---|---|
| **◀** | K, ↑ or ← | Opens the previous sheet in the list. At the top of a page it goes back to the previous page. |
| **▶** | J, ↓ or → | Opens the next sheet. At the bottom of a page it loads the next page. "Last sheet" / "First sheet" appears when there is nowhere to go. |
| **Verify ✓** / **Unverify** | V or Enter | Says that every value on this sheet is right (see [Verifying a sheet](#verifying-a-sheet)). On a verified sheet the button reads **Unverify** and takes the verification back. |
| **then next** | – | On by default. After you verify, the next sheet opens by itself. Untick it to stay on the sheet. The app remembers this choice. |
| **−** | - | Zoom out. |
| **Fit** | F | Fits the whole page into the window. |
| **+** | + or = | Zoom in. |
| **overlay** | O | On by default. Shows or hides the drawn bubbles, outlines and values. Turn it off to see the plain image. With the overlay off you cannot click bubbles. |
| **View** | – | What the canvas shows: **Aligned**, **Aligned, full colour** or **Original as background** (see [View modes](#view-modes)). The app remembers this choice. |
| **side by side** | S | Shows two views next to each other (see below). |
| **left** | – | Only when **side by side** is on: what the left pane shows (**Original**, **Full colour** or **Aligned**). The right pane shows the **View** choice. Default: **Original**. |
| **borders** | B | Off by default. Draws the block borders the engine found or fitted. In the Original view it also draws the page outline (see [Colours on the canvas](#colours-on-the-canvas)). |
| **Regrade…** | R | Opens the Regrade dialog, which re-reads this sheet with other settings (see [Regrade](#the-regrade-dialog)). |
| **Accuracy** | – | Shows how often the engine was right on verified sheets (see [Accuracy](#the-accuracy-dialog)). |
| **Path remap** | – | Tells the app where moved scan folders now live (see [Path remap](#the-path-remap-dialog)). |
| **Export…** | – | Opens the export dialog. The current Results filters (template, job, Show, field, flag, file name) are already filled in as its scope. Export always writes the corrected values. |
| **Delete…** | – | Deletes this sheet's result after asking you first (see [Deleting a sheet](#deleting-a-sheet)). |

#### View modes

All views use the exact geometry recorded when the sheet was read. Nothing is detected again, so outlines sit where the engine looked. Clicking a bubble corrects it in every view.

| View | What you see | When to use it |
|---|---|---|
| **Aligned** (default) | The straightened page the engine actually read, after colour removal and conversion to grey. | Normal checking and correcting. |
| **Aligned, full colour** | The same straightened page in its original colours. | Telling pencil from pen, or a student's mark from coloured print, when colour removal makes them look alike. |
| **Original as background** | The scan in colour, after the turn, mirror and straighten the template or a regrade gives it, and turned the way the reader found the sheet. So a sheet fed upside down shows upright here too, as it does in the aligned views. Every outline is mapped back onto it. | Checking whether the page was placed correctly, spotting a sheet read upside down or shifted by a row, and for the [failed-sheet tools](#failed-sheets-sheets-that-could-not-be-aligned). |

**side by side** (S) splits the canvas into two panes, each labelled with its view name. Zooming, scrolling and hovering move both panes together, and the bubble under the mouse is highlighted in both. A common setup is **left: Original** with **View: Aligned**, so you see the raw scan and the read page together.

The full-colour and original views load a moment after the sheet. Until then the pane says "Loading the … view…". If a view cannot be made, the pane and the side panel say why. Sheets read by older versions, before the page transform was recorded, show an approximate original with a note saying so.

### The canvas

The middle area shows the sheet with its overlay.

- **Zoom:** use the mouse wheel. Zoom follows the mouse pointer.
- **Pan:** drag with the mouse.
- **Status line:** the line under the canvas shows the zoom level. When you point at a bubble, it also shows the field, the bubble value, how full the bubble is (for example "fill 62%"), and whether a click would mark or unmark it. For a column of a grouped value it also says why that column needs a look.
- **Hover:** whatever is under the mouse is outlined in teal.

#### Clicking to correct

- **Click a bubble** to mark or unmark it. The server works out the field's new value from the marked bubbles, always in the order they are printed (so a double mark reads `AB`, never `BA`). It then re-runs the template's checks and updates the score and the sheet's status.
- **Click a field away from its bubbles, or click a zone** (barcode, QR, OCR and so on) to select it. Its row opens in the side panel with the value box ready for typing.

#### Colours on the canvas

| What you see | Meaning |
|---|---|
| Blue-filled bubble with a blue outline | Marked: counted as filled in. |
| Thin grey outline | Empty bubble. |
| Orange outline around a field (or orange bubble outlines) | The field is flagged and needs a look. A dashed orange box means it is still waiting in the review queue. |
| Pink (magenta) outline and label | A column of a grouped value (for example one digit of a roll number) that is multi-marked or unclear. |
| Purple circle around a bubble | A person changed this bubble: it differs from what the machine read. |
| Purple outline around a field or zone | The field or zone has been corrected. |
| Red, thicker outline | The selected field or zone. |
| Teal outline | Under the mouse pointer. |
| Zone outlines | Barcode: dark orange · QR code: purple · OCR: green · ICR (handwriting): blue. Orange when flagged, purple when corrected. |
| Label above a field, for example `q7: B` | The field's name and current value. `∅` means blank. Labels always show for flagged, grouped and selected fields, and for all fields once you zoom in close. |

With **borders** (B) on, each block border is drawn with its status after the block name:

| Border | Status | Meaning |
|---|---|---|
| Solid green | found | The printed border was found and used. |
| Dashed blue | fitted | The border was fitted to the page and used. |
| Dashed red | failed | The border could not be found, so the block was read from its template position. |
| Dashed grey | skipped | No border search was done for this block. |

In the **Original** view, **borders** also draws the **page outline**: a dashed blue frame that shows where the engine thought the page was, with a dot and the label **top-left** on the page's top-left corner. If that dot sits at the bottom of the scan, the sheet was read upside down.

### The side panel

The right panel shows everything about the open sheet, from top to bottom.

#### Heading and status chips

- **File name:** hover over it to see the recorded source path. Under it is the path the file was actually found at (after any path remap).
- **Status chip:** **ok**, **needs review** or **error**.
- **corrected:** a person changed at least one value.
- **verified by *name*** or **not verified**.
- **score N:** shown when the template has an answer key.

#### Registration summary

The line "Registration: …" says how the page was lined up. It can include:

- the alignment method (for example "index points", or "aligned by hand (page corners)"),
- "turned 180°" when the sheet was turned,
- "residual 0.84 px", which is how far the marks were from where they should be after aligning (smaller is better),
- "index points 4/4 found".

For the matching picture, turn on **borders** and choose the **Original** view.

#### Score

When the template has scoring, you see:

- **Score 42 / 50**,
- a band chip when the template defines bands (for example a grade),
- counts chips for **correct**, **incorrect**, **unmarked**, **bonus** and **dropped** questions,
- a table of section scores with their maximum (questions in no section are listed as "Other questions").

The score is recalculated after every correction.

#### Warnings

Yellow notes may appear. They mean:

- **"Showing the stored aligned image…"**: the original scan file was not found, so a saved copy is shown. Set a [path remap](#the-path-remap-dialog) if the folder moved.
- **"The template changed since this sheet was read…"**: the exact template version was not kept, so the current template was used to draw the sheet.
- **"Re-reading now gives different values for: …"**: reading the file again now would give other values for those fields. This usually means the template or settings have changed since. Consider a [regrade](#the-regrade-dialog).
- **"… view: …"**: a full-colour or original view could not be made, and why.
- The sheet's error message, for sheets that could not be read.

#### Sheet-level items

Some checks apply to the whole sheet, for example **too_few_marks** ("(3 marked, minimum 20)"). They are listed with an **Accept** button, which confirms that the sheet really is like that (for example a mostly blank paper).

#### Fields

The heading **Fields (*n* flagged)** lists every output column, with flagged ones first. Each row has:

| Part | What it is |
|---|---|
| **Name** | The field, zone or column name. "check" after it means a cross-field check output, and "label" means a custom label (a grouped value). |
| **Value box** | The current value, which you can edit. Hover over a corrected value to see "read as: …" (what the machine read). |
| **Confidence** | A number from 0 to 1 on a coloured background, from red (unsure) through amber to green (sure). "–" when there is none. |
| **Accept** | Shown when the item is waiting for review. Click it when the value shown is right: this settles the item without changing it. |
| **Scan** / **Cancel scan** | For barcode and QR zones only (see below). |
| **was X** | The value before a person changed it. `∅` means it was blank. |
| Flag chips | The flags the item was read with, for example `multi_marked`, `low_confidence`, `not_found`. (The Review queue section explains every flag.) |
| Pink chips | Reasons a column of a grouped value needs a look, for example "roll_no: multi". |
| Reasons | Short notes from value checks, for example why a value failed validation. |

Click a row to select that field: it turns red on the canvas, and the canvas moves to show it if it is off screen.

#### Editing values

1. Click in the value box (or select the field and press **E**).
2. Type the new value. For a bubble field, type the bubble values. They are changed to capitals for you. Leave the box empty for a blank answer, or type several values (for example `AB`) for a double mark.
3. Press **Enter** (or click elsewhere) to save. Press **Esc** to put back the old value and leave the box.

If the value is not allowed, for example a letter that is not one of the field's bubbles, the box turns red, an error message appears, and the old value comes back.

**Grouped values** (custom labels such as a roll number made of several bubble columns) appear as a row that opens to show their columns. Groups that are flagged, corrected or have a column needing a look open by themselves, and a pink bar on the left marks a group with a problem column. You can either:

- type the whole value in the group's own box. It is split over the columns, and a space means "this column is blank"; or
- open the group and correct single columns.

The group's **Accept** also accepts its flagged columns.

How a grouped value shows empty, multi-marked and unclear columns (for example `␣`, `*` and `-` in `12␣4*-`) is set per group in the template editor. Flagged groups go to the review queue and are highlighted here.

#### Barcode and QR codes: "Scan a code"

Barcodes and QR codes can't be read by eye, so you draw a box around the code and the app decodes it:

1. Click **Scan** next to the barcode or QR zone. The button turns into **Cancel scan**, the pointer becomes a cross, and the status line says "SCAN *name*: drag a box around the code".
2. Drag a box around the code on the sheet. This works in the Aligned or the Original view. Leave a little white space around the code.
3. When you let go, the original scan is decoded at full resolution inside the box. The value is saved as a correction, and a message shows the value and the code type (and says so if more codes were in the box).

"No code found in that box" means you should try again with a slightly larger box. Press **Esc** or click **Cancel scan** to stop.

#### Checks

When the template has cross-field checks, a **Checks** heading lists each one with:

- its value,
- where it came from: "from *source*", or "typed by a person",
- **Accept** when it is waiting for review,
- its flags (for example `cross_check_failed`, `fallback_used`),
- validation reasons,
- a **sources** section you can open to see the values it compared.

You correct a check by typing its output value in the **Fields** list. A value typed for a check is kept when the rules run again and when the sheet is regraded.

#### Changes (history and audit)

**Changes (*n*)** opens to show every edit on this sheet, newest first: the field, old value → new value, who made it and when. The last 50 changes are shown here. Every correction, verification, regrade, manual alignment and deletion is also written to the server's audit log with the user's name.

**Who did what** opens the sheet's activity, newest first: who **scanned** it, who **reviewed** it (accepted values as read, in the queue, on this screen or in bulk), who **changed values**, who **regraded** it (with the settings), who **aligned it by hand** or **typed the values**, and who **verified** it or took the verification back, each with the time. Actions taken before this log existed are not listed. For sheets read in a job, "scanned" is the person who started the job.

After a regrade, a line at the bottom reads "Regraded by *name* with {…}" and shows the settings that were used.

### Undo and redo

**↶ Undo** and **↷ Redo** on the toolbar (Ctrl+Z, Ctrl+Y) work on the open sheet. Undo takes back the sheet's last change, whoever made it and wherever it was made: a value changed or accepted (here, in the Review queue or by bulk accept), a verification or unverify, a regrade, a hand alignment or typed values. A regrade or hand alignment is undone completely: the earlier values, settings, image and zone crops come back. Pointing at a button shows what it will undo or redo and who made that change, for example "Undo regrade by meena".

- Each sheet keeps its last 20 changes. Undoing again goes further back.
- Redo is possible until a new change is made on the sheet.
- Undo and redo are written to the sheet's **Who did what** list ("Undid" / "Redid", with the change and the person).
- While a regrade preview is shown, apply or discard it first.

### Verifying a sheet

**Verify ✓** (V or Enter) means "a person has checked every value on this sheet and it is right". With sign-in on, only administrators can verify. It:

- accepts every item still waiting for review on the sheet,
- marks the sheet **verified by *your name***,
- removes it from **Show › Flagged** (it now appears under **Not flagged** and **Reviewed**) and hides its ⚑ badge. The flags from the original read are still listed and still count in the accuracy figures,
- counts the sheet in the **Accuracy** figures,
- saves its corrected fields as training samples for the bubble model. The message may say "Verified · 12 training samples".

With **then next** ticked, the next sheet opens straight away. **Unverify** (or V again on a verified sheet) takes it back, and the sheet's flags show again.

Only verify a sheet after you have looked at all of it, not just the flagged fields. The accuracy figures assume that verified sheets are fully correct.

### Duplicates (primary key)

In the template editor, a field or group can be ticked as **Primary key**, for example the roll number. Sheets in the same job with the same key value are then treated as duplicates:

- **Show › Duplicates (same key)** lists them.
- The side panel of a duplicate shows a red chip, **Duplicate key: also on**, followed by buttons naming the other sheets. Click one to open that sheet.

A sheet whose key is blank, still flagged, or that failed to read is not counted. This stops every blank roll number being reported as a duplicate. Once you fix the key, the duplicate check uses the new value.

### Failed sheets (sheets that could not be aligned)

A sheet with status **error** (Show › **Errors**) could not be lined up with the template, for example because a corner marker was torn off or the timing marks were missing. If the image can't be aligned, the view switches to **Original** so you can see the scan. The side panel shows "This sheet could not be aligned." and four buttons:

| Button | What it does |
|---|---|
| **Align by page corners** | Switches to the Original view and asks you to click the four page corners in turn: top-left, top-right, bottom-right, bottom-left. The side panel and status line say which corner is next ("Click top-right (2 of 4) on the original"). After the fourth click the sheet is read again through those corners. |
| **Align by index points** | The same, but you click the template's index points by name, in the order shown. Only offered when the template has at least 4 index points (otherwise use page corners). |
| **Place the page by hand** | Lays the template over the scan for you to drag into place (see below). |
| **Type values…** | Opens "Type this sheet's values": one box per output column, filled in with whatever was read. Click **Save** to store them. The sheet is then marked as typed by hand and counts as reviewed (status **ok**). |
| **Cancel clicking** | Shown while you are clicking corners or index points. Stops without changes. |

Clicks must land on the **Original** view. In side-by-side mode, click in the Original pane. Each click is marked with a red circle and its name.

#### "Place the page by hand…"

This tool is offered on failed sheets, and for **any** sheet from the Regrade dialog (**Place the page by hand…**). Use it for a sheet that was read but is out of place, for example shifted by a row or turned the wrong way.

The view switches to **Original**, and the template is drawn over the scan:

- the timing marks as **red** boxes,
- the index points as **blue** boxes,
- the bubbles as **grey** boxes,
- the page outline as a dashed blue frame, with a round handle at each corner (labelled top-left, top-right, bottom-right, bottom-left) and a cross in the middle.

To place the page:

- **Drag a corner circle** to stretch that corner. The corner you drag turns red and becomes the "selected" one.
- **Drag the centre cross** to move the whole page.
- **Arrow keys** nudge the selected corner by 1 pixel, or by 10 pixels with **Shift**. If no corner is selected, or you last dragged the centre, the arrows move the whole page.
- **Turn image 90°** and **Turn image 180°** turn the scan itself (clockwise), so you can place the page on an upright picture. The page box starts over on the turned scan.
- **Turn template 180°** swaps which scan corner counts as the page's top-left, without turning the picture.
- **Turn template 90°** turns the page box a quarter turn at a time.
- **Reset** returns to the starting placement.
- **Cancel** (or **Esc**) leaves without changes.

When the red, blue and grey boxes sit on the printed marks, click **Regrade with this placement**. The sheet is read again through your placement, and a message says how many values changed. The placement is saved with the sheet, so later views, regrades and exports use it.

### The Regrade dialog

**Regrade…** (R) re-reads the original scan file of this one sheet with changed settings. The template itself is not changed. The dialog title is "Regrade this sheet".

The dialog has two groups:

- **Image: turn, mirror, straighten and line up the scan.** These change the scan itself before it is read. A picture on the right shows the original scan as it will be fed to the reader, and it follows the Turn, Mirror and Straighten choices as you change them.
- **Reading settings.** These change how the bubbles are read.

Each control starts at **Template setting** (or **As scanned**, **Not mirrored**, **The template's alignment**), which means "use what the template says". Only the controls you change are applied.

| Control | Choices | What it does | When to use it |
|---|---|---|---|
| **Turn the sheet** (`alignment.rotate`) | As scanned · 90° clockwise · Upside down (180°) · 90° anticlockwise | Turns the scan before reading. | A sheet fed upside down or sideways that was read wrongly. |
| **Mirror the sheet (after the turn)** (`alignment.flip`) | Not mirrored · Left to right · Top to bottom | Mirrors the scan (after any turn) before reading. | A scan saved mirrored (some phone apps and scanners), or a sheet photographed through the paper. |
| **Straighten (degrees, + = clockwise)** (`alignment.straighten`) | empty = 0; −45 to 45, in steps of 0.1 | Turns the scan by a small angle after the turn and mirror. The corners the turn uncovers are filled white. | A sheet fed in skewed that the template's alignment does not straighten (for example with **Line the image up by: Nothing**). With timing tracks or index points the alignment straightens the page by itself. |
| **Line the image up by** (`preProcessors`) | The template's alignment · Page edges only · Nothing: fit the whole image to the page | Replaces the template's alignment for this sheet. **Page edges only** finds the paper's outline. **Nothing** stretches the whole (turned, mirrored, straightened) scan onto the page. Clean-up steps the template sets in JSON are left out too. | A sheet whose marks are damaged or covered so the template's alignment fails or lands wrong. For an exact placement use **Place the page by hand…**. |
| **Colour removal** (`colorDropout`) | Template setting · Plain grey · Red channel (removes red print) · Green channel · Blue channel · Brightest channel (removes any colour) | Chooses how coloured print is removed before reading. | Pink or red printed sheets where the print was read as marks (try **Red channel** or **Brightest channel**), or where colour removal also removed coloured-pen marks (try **Plain grey**). |
| **Bubble threshold** (`threshold_params.mode`) | Template setting · Adaptive (per sheet) · Fixed level | Adaptive works out the dark/light cut for each sheet. Fixed uses one level for every sheet. | Use **Fixed level** for a sheet where the adaptive cut picked up smudges or missed light marks. |
| **Fixed level (0–255)** (`threshold_params.fixed_threshold`) | empty = template setting; 1–254 (hint 120) | With **Fixed level**, the grey level that counts as marked. Higher counts lighter marks as filled. | Light pencil: raise it. Smudges read as marks: lower it. Too high and erasures and shadows count as answers; too low and real marks are missed. |
| **Mark sensitivity (lower counts fainter marks)** (`threshold_params.MIN_JUMP`) | empty = template setting; 1–255 (hint 25) | How big a jump between light and dark counts as a real mark. | Lower it for faint marks, raise it if smudges or erasures count. Too low and noise becomes answers; too high and real marks are lost. |
| **Even out shadows** (`threshold_params.flatten_background`) | Template setting · On · Off | Evens out uneven lighting before reading. | Phone photos and scans with shadows or dark edges. |
| **Find the page outline (phone photos)** (`alignment.page_outline`) | Template setting · On · Off | Finds the paper's edges in the picture first. | Photos where the sheet does not fill the image. |
| **Fit blocks to their printed borders** (`alignment.rectify_on_border`) | Template setting · On · Off | Lines each block up with the border printed around it. | A block that reads a little off while the rest of the page is fine. |
| **Per-block perspective** (`alignment.block_perspective`) | Template setting · On · Off | Corrects each block's own tilt or perspective separately. | Bent or curled pages where one part of the sheet is skewed differently. |

#### Advanced (JSON overrides)

Open **Advanced (JSON overrides)** to give any other setting:

- **Template overrides (JSON)** is merged into the template, for example `{"colorDropout": {"enabled": false}}`.
- **Config overrides (JSON)** is merged into the reading settings, for example `{"review_params": {"min_confidence": 0.3}}`.

When both a control and the JSON set the same value, the JSON wins. If the sheet was regraded before, the dialog starts with the settings of that regrade. Settings the controls can show appear in the controls, and the rest appear in the JSON boxes (which then open by themselves). A message "Invalid JSON: …" means a box contains a typing mistake.

#### Ticks

| Tick | Default | What it does |
|---|---|---|
| **keep my corrections** | ticked | Puts corrections made by people back on top of the new read. Untick it to throw them away and see the pure machine read. |
| **use the current template version** | unticked | Every sheet remembers the template version it was read with, and normally a regrade uses that version. Tick this to use the template as it is **now**, for example after you fixed the template. |

#### Buttons

| Button | What it does |
|---|---|
| **Preview** | Re-reads the sheet without saving. A table lists each value or flag that would change (Field · Now · Regraded · Flags), or says "No value or flag changes." The canvas shows the new read, and the status line says "REGRADE PREVIEW (not saved)". While a preview is shown you cannot edit values. The side panel has **Apply** and **Discard** for the preview. |
| **Apply** | Re-reads the sheet and saves the new read and its settings onto the sheet at once (**Undo** takes it back): the values, the image, the list row and the review queue all show the regraded read, and opening the sheet (or the Regrade dialog) again later shows the saved values and settings. The old read is kept in the sheet's history, and a message says how many values changed. Verification is removed, because the values may have changed: check and verify the sheet again (or let an administrator do it in [Check changes](#check-changes-who-did-what)). |
| **Place the page by hand…** | Closes the dialog and starts [placing the page by hand](#place-the-page-by-hand). |

A regrade needs the original scan file. If the file was moved, set a [path remap](#the-path-remap-dialog) first. If the file's contents changed since it was read, the regrade is refused, so a different picture is never read under the old sheet's name.

### Check changes (who did what)

**Check changes** is its own tab, next to Results. When sign-in is on, only administrators see it; without sign-in everyone does. It has a **Template** and **Job** picker at the top, used by both of its parts.

The work flow it supports: administrators hand flagged sheets out to reviewers (other administrators or reviewers), the reviewers work through the sheets assigned to them, and an administrator checks what they changed and does the final verification, or sends a sheet back.

#### Hand out review work

- **Which sheets:** Flagged, not verified (the usual choice) · Not reviewed · Errors · Corrected · All sheets.
- **People:** names separated by commas. With sign-in on, the box suggests the accounts.
- **At most:** hand out only this many sheets (empty = all that match).
- **only sheets nobody has yet:** on by default, so assigning again only hands out the rest. Untick it to move sheets that someone already has.
- **Assign** splits the sheets into equal runs, in sheet order, between the people named. For example, 300 flagged sheets for "asha, bob, meena" gives each 100 consecutive sheets. Each sheet has one person; assigning it again moves it.

Below it, a table shows per person: **Assigned**, **Still to review** (sheets with items waiting), **Nothing left to review** (not verified yet) and **Verified**, and the number of flagged sheets nobody has yet. **Their changes** fills that person's name into the list below. **Take back** removes all their assignments in this template and job.

A reviewer finds their sheets in **Results › Assigned to › Me** (it also offers **Anyone** and **Nobody**). The side panel of an assigned sheet says who it is assigned to and by whom.

#### Changed sheets

This lists every sheet people changed after scanning that is not verified since its last change. A change is: values corrected, values accepted in review, a regrade, an alignment by hand or typed values.

Each row shows:

- the sheet's file, status and when it was last changed,
- **Scanned:** who scanned it, **Assigned to:** who has it, and **Changed:** everyone who changed it, with what they did,
- the changed settings (the regrade settings, the hand alignment, typed values) and each changed value as *first read* → *now*, with who changed it,
- **Open** (shows the sheet on the Results screen), **Verify** and **Send back**.

Above the table: a box to show only the changes of one person, **include sheets already verified** (to look back at checked sheets), and **‹ Newer** / **Older ›** for more pages (25 sheets each). **Verify ticked** verifies the ticked sheets and **Verify all on this page** every unverified sheet shown; both ask first. Verifying accepts the sheet's values as they are now, flagged ones included, exactly like **Verify ✓**. A verified sheet leaves the list until someone changes it again. Sheets that can't be verified (a failed sheet with no values) are named in the message.

**Send back** (or **Send ticked back**) returns sheets to review: their verification is taken back, and the items flagged when the sheet was read, plus every value someone changed, wait in the review queue again with their current values. It asks who to hand them to (leave it empty to keep the current person) and for an optional note. The Results side panel shows "Sent back to review by *name*: *note*". Undo on the sheet takes it back.

#### Who may verify

With sign-in on, only administrators can verify or unverify a sheet, on the Results screen (the **Verify ✓** button is greyed out for reviewers), here, or through the API. Reviewers correct and accept values; the administrators do the final check. Without sign-in everyone can verify.

Every step is in the sheet's **Who did what** list: assigned (to whom, by whom), sent back (with the note), and all the changes.

The same information is available to programs: `GET /activity/sheets`, `GET /scans/{id}/activity`, `GET /activity`, `POST /activity/verify`, `POST /activity/return`, `POST /assignments`, `POST /assignments/clear`, `GET /assignments/summary`, and `assigned=` on `GET /results`.

### The Accuracy dialog

**Accuracy** opens "Accuracy on verified sheets". It uses only sheets that someone verified, within the current **Template** and **Job** filters. It shows three figures:

- **auto-accepted fields needing no correction**: of all the fields the engine did *not* flag, the percentage nobody had to change. This is the main figure for "can I trust the unflagged answers?".
- **verified sheets**: how many sheets the figures are based on.
- **of flagged fields needed a correction**: of the flagged fields, the percentage that really were wrong. A low value means the flags are cautious, and most flagged items turn out fine.

Below them, a table breaks this down per field: Field · Auto-accepted · Needed no correction · Accuracy · Flagged · Flag precision. The worst fields come first. Rows below 100% are highlighted. These are the fields that were sometimes wrong without being flagged, so look at those fields' settings in the template.

If no sheet is verified yet, the dialog says "Verify sheets on this screen (V) to measure accuracy."

### The Path remap dialog

Every sheet remembers the full path of its scan file. The Results screen re-reads that file to draw the sheet, so if the scan folder was moved or renamed (or a drive letter changed), sheets show "The original file was not found" or fall back to a stored image. **Path remap** tells the app where the files are now. The files themselves are never changed.

- **All results:** one rule per line in the form `old folder=new folder`, for example `D:\scans\2025=E:\archive\2025`. This applies to every sheet.
- **Job *name*:** rules for one job only. This box appears when a job is chosen in the **Job** filter. Otherwise the dialog says "Pick a job in the filters to edit its own rules."
- **From OMR_PATH_REMAP:** rules set by the server administrator at start-up are shown here for information.

The job's rules are tried first, then **All results**, then the administrator's rules. Rules match whole folder names. For Windows paths, upper and lower case and the slash direction don't matter. Click **Save**, and the open sheet is redrawn from the new location.

### Deleting a sheet

**Delete…** asks "Delete *name*?" before doing anything. Deleting removes the sheet's result: its values, corrections, review items and the stored images. **It cannot be undone.**

- A scan file in a server folder is **not** touched. A copy that was uploaded is deleted too.
- The deletion is written to the audit log with your name.
- You can't delete a sheet while its job is still running: wait for the job to finish, or cancel it, first.

After deleting, the next sheet in the list opens.

### Keyboard shortcuts

Shortcuts work while the Results tab is open, no dialog is open, and you are not typing in a box.

| Key | Action |
|---|---|
| **J**, **↓** or **→** | Next sheet |
| **K**, **↑** or **←** | Previous sheet |
| **V** or **Enter** | Verify (or unverify) the sheet |
| **Ctrl+Z** | Undo the open sheet's last change |
| **Ctrl+Y** or **Ctrl+Shift+Z** | Redo the change Undo took back |
| **N** or **Tab** | Select the next flagged field on this sheet |
| **Shift+N** or **Shift+Tab** | Select the previous flagged field |
| **E** | Edit the selected field (puts the cursor in its value box) |
| **F** | Fit the page to the window |
| **+** or **=** | Zoom in |
| **-** | Zoom out |
| **O** | Overlay on/off |
| **S** | Side by side on/off |
| **B** | Borders on/off |
| **R** | Open the Regrade dialog |
| **Esc** | Cancel a barcode/QR scan, or leave "place the page by hand". In a value box: put the value back and leave the box. |
| **Enter** (in a value box) | Save the value |
| **Arrow keys** (while placing the page) | Nudge the selected corner or the whole page by 1 px. With **Shift**: 10 px. |
| Mouse wheel / drag | Zoom / pan |

### How to …

#### How to fix a misread sheet

1. Open the sheet (for example with **Show › Flagged**).
2. Press **N** to jump to the first flagged field. It is outlined in red, and its row is highlighted in the side panel.
3. Zoom in (**+** or the mouse wheel) and look at the marks. Switch **View** to **Aligned, full colour**, or turn on **side by side** with the Original, if you can't tell pencil from print.
4. Correct it:
   - click a wrongly read bubble to unmark it, and click the right one to mark it; or
   - press **E**, type the value, and press **Enter**; or
   - if the value shown is already right, click **Accept**.
5. Press **N** for the next flagged field and repeat.
6. Look over the rest of the sheet, then press **V** to verify. With **then next** ticked, the next sheet opens.

#### How to fix a sheet read upside down or a row off

1. Open the sheet and choose **View › Original as background**. Turn on **borders** (B).
2. Look at the dashed blue page outline and its **top-left** dot. If the dot is at the bottom of the page, the sheet was read upside down. If the bubble outlines sit one row above or below the printed bubbles, it was read a row off.
3. For an upside-down sheet: press **R**, set **Turn the sheet** to **Upside down (180°)**, click **Preview**, check the changes, then click **Apply**.
4. For a sheet a row off, or when turning does not help: press **R** and click **Place the page by hand…**. Use **Turn image 180°** (or **Turn image 90°**) if the scan is turned. Then drag the corners, or the centre cross, until the red timing-mark boxes and grey bubble boxes sit on the printed ones. Fine-tune with the arrow keys (Shift for bigger steps).
5. Click **Regrade with this placement**, check the values, and verify the sheet.

#### How to rescue a sheet that could not be read

1. Choose **Show › Errors** and open the sheet.
2. Click **Align by page corners** and click the four page corners on the original in the order asked. Or use **Align by index points** if the template has them, or **Place the page by hand**.
3. If the scan is too damaged to align, click **Type values…**, fill in the values from the paper, and click **Save**.

#### How to find all sheets with a flag

1. Set **Template** and, if you like, **Job**.
2. Choose a flag in **Flag**, for example `multi_marked`. To narrow it to one question, also choose it in **Flagged field**.
3. Set **Show** to **Flagged** to hide sheets that are already verified.
4. Step through with **J**. On each sheet, **N** jumps straight to the flagged field.
5. To hand the list to someone else, click **Export…**: the export starts with the same filters.

#### How to re-read a sheet after a template change

1. Fix the template in the template editor and save it.
2. On the Results screen, open a sheet the change should affect. A note "Re-reading now gives different values for: …" may already list the affected fields.
3. Press **R**, tick **use the current template version**, and leave **keep my corrections** ticked (untick it only if the corrections were made to work around the old template).
4. Click **Preview** and check the table of changes. Click **Apply** when it looks right.
5. Check the sheet again and verify it. Repeat for the other sheets the change affects; use the **Flagged field** or **Flag** filters to find them.

## 4. Templates and the template editor: layout, alignment, reading areas

A **template** tells UNIC OMR Scanner where everything sits on one kind of answer sheet: the page size, every block of bubbles and what each bubble means, any barcode, QR code, printed-text or handwriting areas, and how each scanned sheet is lined up with the layout before it is read. Every scan, job, review item and result belongs to one template.

This section covers the **Templates** tab and the layout parts of the template editor. The reading options (thresholds, review settings, learned models), validation rules, cross-field checks, scoring and colour removal are covered in the next section.

### The Templates tab

The tab has three panels.

#### Upload a template

Use this when you already have a template folder, for example one exported from another station or one a colleague made.

| Option | Default | What it does |
|---|---|---|
| **Name** | empty (the name is taken from the files) | The name shown everywhere in the app, e.g. "Final exam 2026". Names must be unique (capital and small letters count as the same). |
| **Files** | none | Choose `template.json` plus anything that belongs with it: marker or reference images, `config.json` (reading options), `evaluation.json` (answer key). You can also choose one `.zip` of the whole template folder instead. |
| **Upload** button | | Checks the files and adds the template. If something is wrong, the problems are listed in red under the button and nothing is added. |

#### Auto-generate from samples

The app can build a first draft of a template by itself from filled sheets.

| Option | Default | What it does |
|---|---|---|
| **Name** | empty ("generated") | Name of the new draft. |
| **Sample sheets** | none | About 20 filled sheets of the same form, as images or PDFs. One blank sheet among them helps. More variety in the answers gives better results. |
| **Labels (CSV or .xlsx)** | none (optional) | A spreadsheet with the true answers of the sample sheets. It needs a file-name column (the heading can be spelled "File Name", "file", and so on) plus one column per field (e.g. `roll1`, `q1`). An answer-string column such as `ANS` holding text like `CB A*D…` (a space means blank, `*` means several marks) is split into `q1`, `q2`, … automatically. With labels, the generator can work out which block is which question; without them it tries to read the bubble values printed on the form. |
| **Options (JSON)** | empty | Expert settings for the generator, written as JSON. The grey example text in the box is only a placeholder. Keys the generator understands include `page_size` (e.g. `[1700, 2200]` to force the page size), `rectify_on_border` (`true` to switch on printed-border fitting for boxed blocks), `color_dropout` (`false` to switch off colour removal), `read_printed_values` (`false` to stop it reading bubble values from the print), `hold_back_below` (default 0.5: unlabelled blocks with less confidence are only suggested, not added), `reference_index` (which sample, counting from 0, is used as the reference; default the cleanest one) and `verify_below` (default 0.99: anything that agrees less than this with your labels is listed for checking). Leave the box empty unless you know you need one of these. |
| **Generate draft** button | | Analyses the sheets ("Analysing N sheets…"), creates the draft and opens it in the editor. If the server has no template generator installed you see "Template generation is not installed on this server." |

The generator lines up the sheets, finds bubble grids, timing tracks, corner marks, barcodes, QR codes and character boxes, assigns field names and values, and then reads the samples again to check itself. It always produces at least an empty draft. A generated template starts with the status **draft**; it becomes **ready** the first time you save it without errors. What it was unsure about is listed in the editor (see "Checking a generated draft" below).

#### The template list

| Column | What it shows |
|---|---|
| **Name** | Click it to open the template in the editor. |
| **Id** | The template's internal id (fixed when it was created; it does not change when you rename). |
| **Status** | **ready** (green) or **draft** (amber, a generated template not yet saved). |
| **Blocks** | Number of bubble blocks. |
| **Zones** | Number of barcode, QR, text, handwriting and image areas. |
| **Scoring** | "yes" when the template has an answer key. |
| **Updated** | When it was last changed. |

Buttons on each row:

- **Edit** opens the editor.
- **Layout** opens a picture of the layout drawn over the reference image in a new browser tab. Handy to print or send to someone.
- **Download** saves the template as one file, *name*.omr-template.zip: the layout, reading options, answer key, reference image and other files, plus the template's name. To give the template to someone else, send them this file. On their system they choose it under **Upload** in the Templates tab (leave the name empty to keep the original name). If their system already has a template with that name, the imported one is called "*name* (2)". Alignment test sheets are not included.
- **Duplicate** asks for a new name (suggested: "<name> copy") and copies the layout, its reading options, answer key and reference image, then opens the copy. Scans and results of the original stay with the original. Use this to make a variant (another exam with the same form, a different answer key) without risking the original.
- **Rename** changes only the name. Past jobs, results and review items stay linked. You can also rename by clicking the title inside the editor.
- **Delete** removes the template after you confirm. Stored scans are kept. New jobs can no longer use the template, so only delete templates you are finished with.

**Refresh** (top right) reloads the list.

#### Template versions

You never manage versions by hand. Each time sheets are read, the app keeps a copy of the template exactly as it was at that moment, and every result remembers which copy it was read with. Editing a template therefore never changes how earlier results look or re-read. When you regrade sheets on the Results screen you can tick **use the current template version** to re-read them with the template as it is now instead.

### The editor at a glance

Opening a template shows the editor: a toolbar across the top, the page (the canvas) on the left and a side panel on the right. The side panel shows the **Page** settings when nothing is selected, a **Bubble block** panel when a block is selected, and a **Zone** panel when a zone is selected.

#### Toolbar

| Control | What it does |
|---|---|
| **← Back** | Back to the template list. Asks "Discard unsaved changes?" when you have not saved. |
| Title | The template's name. Click it to rename. |
| Status chip | **ready** or **draft**. |
| **Save** (Ctrl+S) | Saves the template, its reading options and answer key. Shows **Save •** while there are unsaved changes. The server checks the template first; if it is not valid nothing is saved and the problems are listed at the top of the side panel under **Validation errors**. |
| **Undo** (Ctrl+Z) / **Redo** (Ctrl+Shift+Z or Ctrl+Y) | Up to 200 steps. Changes made in Edit JSON or Scoring count as one step. |
| **+ Block** (B) | Then drag on the page to draw a new bubble block. |
| **+ Zone** (Z) | Then drag on the page to draw a new zone. The dropdown next to it picks the zone type: **barcode**, **qrcode**, **ocr**, **icr** or **image**. |
| **Duplicate** (Ctrl+D) | Copies the selected block or zone (see "Drawing and moving blocks"). |
| **Delete** (Del) | Deletes the selected block or zone. |
| **−** / **Fit** / **+** | Zoom out, fit the page in the window (F), zoom in. |
| **grid N px** | Snap grid for drawing, moving and resizing: 1, 2, **5** (default), 10 or 20 template pixels. Use 1 for fine work. |
| **edges** | On by default. Moving or resizing snaps to the edges and centres of other blocks and zones, shown with pink dashed guide lines. Untick it when the snapping gets in the way of small corrections. |
| Background | What is drawn under the layout: **Reference image** (the stored picture of the blank or sample form), **Last test scan** (the aligned image from your last Test read), **Blank page**, or **Upload background…** (choose an image; it is saved as the template's reference image). |
| **Columns…** | Output columns and groups (see "Output columns"). |
| **Colours…** | Sheet colours and colour removal (next section). |
| **Edit JSON** | Edit the template files directly (see "The JSON view"). |
| **Scoring…** | Answer key and marking scheme (next section). |
| **Test read…** | Saves the template, then reads one sample sheet you choose and draws the result over the layout (see "Test read"). |
| **?** | Shows the keyboard and mouse shortcuts over the page. Click the box to close it. |

Under the page a status line shows the mouse position in template pixels, the zoom level and the name of the block or zone under the mouse.

#### What the colours on the page mean

| Drawn as | Meaning |
|---|---|
| Thin blue box with bubble outlines | A bubble block. The bubble values (A, B, C… or 0–9) are written inside the bubbles once you zoom in far enough, and the field names along the side. A field name followed by `#3` means it is the 3rd column in a custom export order. |
| Thick dark-blue box with square handles | The selected block or zone. |
| Orange dashed box | An item the generator asked you to verify (see "Checking a generated draft"). |
| Amber box with ⚠ and "(values 1234567890)" | A digit block whose values run in a different order from the other digit blocks, e.g. 1–9, 0 while the rest are 0–9. Usually a mistake; check the field type. |
| Tinted box: orange = barcode, purple = QR code, green = OCR, blue = ICR, grey = image | Zones, labelled with name and type. |
| Orange numbered rectangles | Timing marks of an alignment track, with the track name and mark count. |
| Red numbered rectangle, e.g. `12×3` | A timing mark that was missed on some sample sheets (here mark 12, missed on 3 sheets). |
| Magenta box or circle with a name (P1, P2…) | An index point. |
| Dashed magenta circles | Suggested index points (only while **+ Add index point** is on). |
| Dashed orange rectangles | Printed boxes on the form (while picking a printed border, or when "Show them on the page" is ticked under Printed boxes without a block). |
| Green-filled bubbles, values beside fields, red outlines | Results of the last Test read: green = read as marked, red = the field was flagged for review. |

### Drawing and moving blocks and zones

- **Draw:** press **+ Block** (or B) or **+ Zone** (or Z), then drag a rectangle on the page. Press the button again or Esc to cancel.
- **New block:** starts as a 4-choice question block (`QTYPE_MCQ4`, A–D, one row per question). The number of rows is guessed from the height you drew, and the questions are numbered on from the highest existing `q` number (e.g. `q21..40`). Set the field type, labels and values in the side panel straight away.
- **New zone:** gets a name such as `barcode_1` and the type chosen in the toolbar dropdown.
- **Select:** click a block or zone. When boxes overlap, the smallest one under the mouse is picked. Double-click selects and jumps to the first box of the side panel. **Tab** / **Shift+Tab** step through all blocks and zones.
- **Move:** drag it, or use the arrow keys (1 px; with Shift 10 px). Quick arrow presses on the same object count as one undo step.
- **Resize:** drag one of the eight handles. Resizing a **block** stretches or squeezes the spacing between bubbles and rows; the bubbles keep their size. Resizing a **zone** changes its size.
- **Duplicate (Ctrl+D):** a copied block is placed to the right and its field labels continue the numbering (`q1..20` becomes `q21..40`). A copied zone is placed below the original.
- **Delete (Del or Backspace):** if grouped fields use the block's columns you are asked whether to **Remove from the group** (the group keeps its other columns) or **Delete the group**. If a zone is used in a cross-field check you are asked to confirm, and it is removed from the check.
- **Several blocks:** Shift+click blocks to select more than one (for grouping, see "Groups").
- **Pan:** drag on empty space, drag with the right or middle mouse button, or hold Space and drag. Shift + mouse wheel pans sideways.
- **Zoom:** mouse wheel (zooms around the mouse), + / − keys, or F to fit.

### Page settings

Shown in the side panel when nothing is selected.

| Option | Key | Default | What it does |
|---|---|---|---|
| **Page width**, **Page height** | `pageDimensions` | set when the template is made | Size every sheet is resized to before reading, in template pixels. All positions in the template are measured on this page. Changing it later does **not** move the blocks, so everything would be in the wrong place; only change it on a new template. |
| **Bubble width**, **Bubble height** | `bubbleDimensions` | set when made (20 × 20 if missing) | Default bubble size for every block. Set it to the size of one printed bubble. Too large and neighbouring print counts as pencil; too small and light marks at the edge are missed. |
| **Empty value** | `emptyValue` | nothing | What is written for a field where no bubble is marked. Leave empty unless your results system expects something like `-`. |
| **Colour removal (colorDropout)** with **Colours…** | `colorDropout` | grey (off) | Shows the current colour removal; the button opens the Colours panel (next section). |
| **Bubble threshold** | | adaptive | How marked bubbles are told from empty ones (next section). |

Below these a line counts blocks, fields and zones, followed by four foldable groups: **Block alignment**, **Grouped fields**, **Cross-field checks** (next section) and **Options** (open by default). **Options** holds **Output columns**, **Alignment** (the alignment method and track tools), the review settings, **Learned models**, **PDF input** and **Barcode advanced**, and **Advanced (JSON)**.

### Bubble block properties

Select a block to see the **Bubble block** panel. Every option has an ⓘ icon; its help line appears while the box is focused.

#### Basic

| Option | Key | Default | What it does |
|---|---|---|---|
| **Name** | (block name) | e.g. `MCQ_Block_1` | Name of the block, only used inside the editor and in messages. Output columns are named after the field labels, not the block. |
| **Field type** | `fieldType` | QTYPE_MCQ4 for new blocks | A preset of bubble values and layout. Choices: **QTYPE_INT** (0–9 in a column, e.g. roll-number digits), **QTYPE_INT_FROM_1** (1–9 then 0 in a column), **QTYPE_MCQ4** (A–D in a row), **QTYPE_MCQ5** (A–E in a row), **QTYPE_MCQ4_RTL** (D–A, right to left), **QTYPE_MCQ5_RTL** (E–A, right to left), or **Custom values** to type your own. A wrong type gives every answer of the block the wrong letter or digit; compare the drawn values with the printed sheet. |
| **Bubble values (comma separated)** | `bubbleValues` | (Custom values only) | What each bubble means, in order, e.g. `A,B,C,D` or `0,1,2,3,4,5,6,7,8,9`. Must have exactly as many values as there are bubbles per field. |
| **Direction (how values are laid out)** | `direction` | (Custom values only) vertical | **horizontal**: each field is a row of bubbles, fields stacked downwards (question rows). **vertical**: each field is a column of bubbles, fields side by side (roll-number digits). |
| **Field labels** | `fieldLabels` | e.g. `q1..20` | Names of the fields of this block, one per row or column, comma separated. Ranges like `q1..20` or `roll1..6` are expanded. The line under the box shows how many fields that gives. Renaming labels here also renames them in groups, validation rules and checks. If you shorten the list and a group used the dropped columns, you are asked what to do with the group. Labels must match the number of rows (or columns) actually printed. |

#### Position and spacing

| Option | Key | Default | What it does |
|---|---|---|---|
| **Origin x**, **Origin y** | `origin` | where you drew it | Left and top edge of the first bubble, in template pixels. Normally set by dragging; type values for exact placement. |
| **Bubbles gap** | `bubblesGap` | from the drawn size | Distance from the start of one bubble to the start of the next within one field. |
| **Labels gap** | `labelsGap` | from the drawn size | Distance from the start of one field (row or column) to the next. If the last rows of a block drift off their bubbles, this gap is slightly wrong. |
| **Bubble width**, **Bubble height** | `bubbleDimensions` | empty = the page's size | Bubble size for this block only, for forms with different bubble sizes. |
| **Empty value** | `emptyValue` | empty = the page's value | Value written for an unmarked field of this block. |

The line at the bottom shows the block's total size.

#### Alignment (per block)

The count next to the heading shows how many of these are set for the block.

| Option | Key | Default | What it does |
|---|---|---|---|
| **Fit to printed border** | `rectifyOnBorder` | page default | **on: snap onto the printed box**: after the page is aligned, find the rectangle printed around this block and fit the bubbles onto it. Corrects a block that sits slightly off, turned or skewed. **off**: never do this for this block. **page default**: follow **Use printed block borders** under Block alignment. |
| **Pick printed border…** button | | | Shows the rectangles printed on the reference image as dashed orange boxes; click the one around this block. The border is switched on and the gap is measured for you. A larger box around that one is taken as the outer frame. If the box you click does not enclose the block's bubbles you get a message and nothing changes. |
| **Border gap (px from bubbles to the box)** | `borderPadding` | empty = measured on each sheet | (Only when fitting is on.) Distance from the outer bubbles to the printed box. Filled in by Pick printed border. |
| **Outer frame gap (px, two-level search)** | `outerBorderPadding` | none (single level) | (Only when fitting is on.) Gap from the bubbles to a large outer frame printed around the block. The outer frame is found first, then the block's own border is searched inside it. Leave empty unless the block sits inside two nested boxes. |
| **Fit to bubble outlines** | `blockPerspective` | page default | For blocks without a printed box: fit the block onto its printed bubble outlines. **on**, **off** or **page default**. |

Safety limits apply: a printed border may move the bubbles by at most 0.4 of the bubble spacing. A bigger move is not applied and the sheet is flagged `border_slide` for review, so a border can never slide a block onto the neighbouring bubble. When the border is not found, the page alignment is kept and the block is flagged `rectify_failed` (shown in results; not sent to review unless you tick that flag in the review settings).

#### Output as one field and Validation

- **Output as one field**: joins all columns of the block into one output column, e.g. `rollno` from `roll1..roll10` (see "Groups").
- **Validation** / **Validation of each field**: rules the read value must follow (length, pattern, allowed values…). Covered in the next section.

### Block alignment (page-wide border settings)

The **Block alignment** group on the Page panel switches block-level corrections on for the whole template. A block's own setting overrides these.

| Option | Key | Default | What it does | When to change |
|---|---|---|---|---|
| **Use printed block borders** | `alignment.rectify_on_border` | off | Fit each block onto the rectangle printed around it after the page is aligned. A block that is not found keeps the page alignment and is flagged `rectify_failed`. | Forms where each block has its own printed box and sheets read slightly off in one area. |
| **Fit blocks without a box to their bubbles** | `alignment.block_perspective` | off | Fit blocks with no printed box onto their printed bubble outlines. A fit that moves too far or makes the bubbles fit worse is rejected. | Sheets with many `weak_mark` flags because bubbles sit a little off. On one large real set it cut weak-mark flags from 35 to 7 with no answer changes. |
| **Border search image** | `alignment.rectify_print_image` | Automatic (darkest channel) | Which image the borders are searched on when colour removal drops the printed form (pink or red forms). **Darkest channel** keeps coloured print dark; **Plain grey** is the ordinary grey copy. Bubbles are still read on the colour-removed image. | Only if borders are not found on coloured forms. |
| **Border search distance (px)** | `alignment.rectify_search_px` | 20 (2–100) | How far a block border may sit from where the template expects it. | Raise for sheets that are badly off; lower if a neighbouring box is picked up by mistake. |
| **Check bubbles fit after a block correction** | `alignment.verify_bubble_fit` | on | Rejects a block correction when the printed bubbles fit worse after it than before. | Keep it on. Turning it off can hide misaligned blocks. |
| **Find the page outline (phone photos)** | `alignment.page_outline` | off | Finds the page's four corners and flattens the photo before alignment. Flatbed scans without a visible page edge are left unchanged. | Phone photos with background around the paper. |
| **Turn every sheet** | `alignment.rotate` | As scanned | Turns every sheet 90°, 180° or 270° before anything else. | A whole batch fed sideways or upside down. For a few mixed sheets use **Detect upside-down sheets** (timing tracks); for one sheet use Regrade › Turn the sheet. |
| **Mirror every sheet** | `alignment.flip` | No mirror | Mirrors every sheet after the turn: **Left to right** or **Top to bottom**. | A batch saved mirrored. For one sheet use Regrade › Mirror the sheet. A mirrored sheet without this setting fails alignment or reads wrong answers. |
| **Preview block borders…** button | | | Opens the Block borders dialog (below). | |

The template can also turn every sheet by 90, 180 or 270 degrees before reading (`alignment.rotate`). This has no form control; set it in Edit JSON if a whole batch is always fed turned.

#### The Block borders dialog

Shows which printed borders are found on a sheet with the current (even unsaved) template.

| Control | What it does |
|---|---|
| **Sheet** | The reference image or one of the template's sample sheets. |
| **Add samples…** | Upload sample sheets that are kept with the template for previews and tests. The same samples are used by **Test on samples** and **Calibrate on samples** in the track tools. |
| **View** | **Print kept (borders)**: the image borders are searched on. **Colour removed (bubbles)**: the image the bubbles are read on. |
| **Border outlines** | Draw each block's detected border: green when found and used, red when it failed. |
| **Bubbles** | Draw the bubble positions that were read. |
| **Refresh** | Read the sheet again with the current template. |
| **Test on samples** | Run on every uploaded sample and list blocks whose border was not found ("N/M sheets fine"). |
| **Close** (or Esc) | Close the dialog. |

The side list shows each block with its border status (and "(bubbles)" when it was fitted to bubble outlines) and the reason when it failed, the page alignment's leftover error in pixels, any margin trimmed off the scan, and any sheet-level flags.

### Alignment: lining each sheet up with the template

Before any bubble is read, each scan is turned, shifted and stretched so that it matches the template page. Pick the method under **Options → Alignment**. The section title shows the current method, e.g. "Alignment · Timing tracks".

#### Alignment method

| Choice | Best for | Needs |
|---|---|---|
| **None (sheets are already straight)** | Scans that are already perfectly straight and cropped to the page. | Nothing. Any shift in the scan shifts every bubble. |
| **Timing tracks** | Forms with rows of solid black marks along the edges (most machine-read forms). The most precise and robust choice. | The tracks drawn in the track editor (or at least 4 index points). |
| **Index points only (no timing tracks)** | Forms without timing tracks that have a few distinct printed marks: corner squares of any size, dots, L-shaped corners. Works like corner markers, but each mark keeps its own size and shape and no marker picture is needed. | 4 or more index points (5 or more lets a wrong match be caught). Choosing it keeps index points already set, or fills in the marks the generator suggested; add more with **+ Add index point**. The panel shows how many points there are and warns below 4. Search radius and Max leftover error work as for tracks. |
| **Corner markers** | Forms with four identical printed squares or symbols near the corners. | A picture of the marker, uploaded with the template files. |
| **Page edges** | Scans that show background around the paper. | A visible paper edge. |
| **Match a reference image (features)** | Forms without marks, scanned fairly straight. | A reference image of the form. |
| **Match a reference image (ECC)** | Fine alignment of forms without marks, when sheets are only a little off. | A reference image of the form. |

After changing the method, run **Test read…** and pick **Last test scan** as the background to see the aligned sheet under the template. Clean-up steps such as blurs that were set in JSON are kept and listed as "Clean-up steps kept".

#### Timing tracks options

| Option | Key | Default | What it does | If set badly |
|---|---|---|---|---|
| **Search radius** | `searchRadius` | about half the smallest distance between two marks | How far (template px) a mark may sit from where the template expects it. | Too small: marks on shifted sheets are not found and the sheet fails. Too large: a mark can be matched to its neighbour. |
| **Minimum matched marks** | `minMatchedMarks` | 8 (or all marks, if fewer) | Fewer matched marks than this rejects the sheet. | Too low lets badly damaged sheets through; too high rejects sheets with a torn or punched track. |
| **Max leftover error** | `maxResidual` | 3.0 px | Average distance between where the fitted page puts the marks and where they were found. Above this the sheet is rejected. | Raise a little (4–6) for curled paper or phone photos that fail; keep low for flatbed scans. |
| **Bend to fit the marks (nonRigid)** | `nonRigid` | Off | **Off (straight perspective fit)**: one straight fit for the whole page. **Exact: every mark onto its template place**: after that fit, the page is stretched so every timing mark lands on its place, row by row and side to side; good for scanners that feed unevenly. **Smooth curve (curled paper, phone photos)**: a smooth bending correction. **Curve when marks cover the page**: the smooth curve, only when the marks are spread over the page. | Bending with few or badly placed marks can warp parts of the page; test on samples after changing. |

The Detect upside-down switch and the track tools sit just below (see "The track editor").

Expert options with no form control (set in **Edit JSON**, inside the timing-track settings):

| Key | Default | What it does |
|---|---|---|
| `sizeTolerance` | 0.5 | How much a mark's size may differ from the set mark size before it is ignored. |
| `earlyStop` | on | Stop trying other orientations once one fits cleanly (faster). 0° and 180° are still both checked unless index points decide. |
| `indexSeed` | off | Also start the track search from the index points' own fit. Can help sheets that are badly shifted. |
| `jointFit` | on | Fit the page to the index points, timing marks and printed block corners together. |
| `trimMargins` | on | Report and trim scan margins that lie outside the template page. |
| `maxRegionResidual` | off | Send a sheet to review (flag `alignment_residual`) when the leftover error in any part of the page is above this many pixels. |

#### Corner markers options

| Option | Key | Default | What it does |
|---|---|---|---|
| **Marker image** | `relativePath` | `omr_marker.jpg` | File name of the marker picture in the template folder. Upload the picture together with the template files. |
| **Marker width share** | `sheetToMarkerWidthRatio` | 17 | Sheet width divided by marker width. Set it to match your form; a wrong value makes markers hard to find. |
| **Min match score** | `min_matching_threshold` | 0.3 | Lowest similarity (0–1) accepted for a marker. Lower it if good sheets fail to find markers; raise it if other print is mistaken for a marker. |

#### Page edges options

| Option | Key | Default | What it does |
|---|---|---|---|
| **Edge kernel** | `morphKernel` | 10 | Size of the smoothing used to find the page edge. Raise it if text near the edge confuses the edge search. |

#### Match a reference image (features) options

| Option | Key | Default | What it does |
|---|---|---|---|
| **Reference image** | `reference` | `reference.png` | Image of a blank sheet in the template folder; scans are aligned to it. |
| **Max features** | `maxFeatures` | 1000 | Number of feature points compared. More is slower but steadier. |
| **Good matches share** | `goodMatchPercent` | 0.15 | Share (0–1) of the best matches kept. |
| **Only shift and rotate (2d)** | `2d` | off | Fit only a turn and a shift, no perspective. Safer for flatbed scans. |

#### Match a reference image (ECC) options

| Option | Key | Default | What it does |
|---|---|---|---|
| **Reference image** | `reference` | `reference.png` | Image of a blank sheet in the template folder. |
| **Motion model** | `motion` | full perspective | How much the sheet may be changed: **shift only**, **shift and rotate**, **affine**, **full perspective**. Use the simplest one that works. |

ECC also has JSON-only settings (`iterations` 50, `epsilon`, `scale` 0.5, `minCorrelation` 0.5).

### The track editor (timing tracks and index points)

The tools under the Alignment method let you see and define the marks a template aligns on.

| Control | What it does |
|---|---|
| **Show marks on the page** | Draws every timing mark (numbered) and index point; marks missed on a sample are red. Untick to unclutter the page. |
| Track list | Each track with its mark count and spacing ("pitch"), with **Remove**. |
| **Use detected tracks** | (Generated templates without tracks yet.) Fills in the tracks the generator found. |
| **+ Add track** | Drag a box over a whole strip of timing marks. The marks inside are found on the reference image and added as a track named `left`, `right`, `top` or `bottom` by position. |
| **Edit marks** | Click a mark to remove it; click empty space on a track to add a mark there (centred on the printed mark if one is found). A track needs at least 2 marks. |
| **Mark width**, **Mark height** | Size of one mark on a left/right track, in page pixels (top/bottom tracks use the same mark turned). Set from the first track you add. |
| **Detect upside-down sheets** (`detectOrientation`) | On by default. Tries the sheet turned 90, 180 and 270 degrees when it does not match. If your tracks look the same upside down, add an index point so the right way up wins. |

While a tool is active, a yellow bar says what to do; click **done** (or Esc) to finish.

#### Index points

Index points are extra printed marks (squares, corner markers, small dots, L-shaped corners) that are matched together with the tracks. Each keeps its own size and shape. They are used:

- **with tracks**, to decide which way up a sheet is and to stop a track being matched one mark off;
- **without tracks**, as the alignment itself: 4 points are the minimum, 5 or more is safer.

| Control | Key | Default | What it does |
|---|---|---|---|
| **+ Add index point** | | | Click a printed mark on the page, or drag a box around it. Small dots work too. Suggested marks are circled while this is on. Points are named P1, P2, … |
| Point row: **X**, **Y**, **W**, **H** | `center`, `size` | measured | Centre and size in template pixels; type to correct. |
| Shape | `shape` | any (or as measured) | **any**, **square** or **circle**. |
| **required** | `required` | on | A sheet where this mark cannot be found goes to review with the flag `index_point_missing`. Untick for a mark that is sometimes covered (e.g. by a sticker). |
| **Remove** | | | Deletes the point. |
| **Add at X Y W H** + **Add** | | | Adds a point at an exact position. The app looks for a printed mark there and tells you how far off it is; if none is found you must type W and H too. |
| **Calibrate on samples** | | | Saves, then measures every index point on the stored sample sheets and uses the median position and size. A table shows per point how often it was **Found**, the **Spread**, how far it **Moved**, and any problem. |
| **Use them as index points** | | | Shown when there are no tracks and the generator found 4 or more distinct marks near the edges. One click adds them all. |

With 4 or more index points and no tracks, the template switches to aligning on them and drops reference-image matching (you get a message). Warnings appear in red when the points are badly placed: fewer than 5 points with no tracks, points nearly on one line, all points on one side of the page, or points that look the same upside down. Fix these by adding a point elsewhere, ideally one without a twin at the opposite corner.

#### Per-sheet results and printed boxes

- **Marks matched per sample sheet** (after generation) or **Test on samples** (after you test): a table of each sample with **Marks found** (e.g. 58 / 60, red when some were missed), **Index points** ("all found" or "missing P2") and, after a test, the **Status** (including "turned 180°" when it was read upside down).
- **Test on samples**: saves, then re-runs alignment on the sample sheets kept with the template.
- **Printed boxes without a block**: rectangles on the form that hold bubbles but no block yet. **Show them on the page** draws them dashed orange; **Adopt as block** finds the bubble grid in that box and adds it as a block (check its labels and values afterwards); **Adopt all N** does them all.

### Corner markers vs index points vs timing tracks

| | Timing tracks | Index points | Corner markers |
|---|---|---|---|
| What is printed | Rows of evenly spaced solid marks along one or more edges | Any few distinct marks: squares, dots, corner symbols, each with its own size | Four identical symbols near the corners |
| How many reference points | Many (often 40–100) | 4 or more (5+ recommended), or a few added to tracks | 4 |
| Precision | Highest; can also bend the page to fit (Bend to fit the marks) | Good when spread over the page | Good on clean scans |
| Upside-down sheets | Detected, but symmetric tracks need an index point to decide | Decides orientation when the points are not symmetric | Not handled |
| Missing mark | Tolerated up to **Minimum matched marks** | A missing **required** point sends the sheet to review | Sheet fails |
| Set up with | **+ Add track** | **+ Add index point**, Calibrate on samples | Marker image file and Marker width share |
| Choose it when | The form has tracks | The form has no tracks but has a few printed marks, or to make tracks safer | The form only has four matching corner symbols and you have a picture of one |

A generated template may set four corner squares up as a short track called `corners` under **Timing tracks**; that works like corner markers but is matched like a track.

### Groups (grouped fields)

A **group** joins several bubble columns into one output column, e.g. a roll number written in six digit columns `roll1..roll6` becomes one column `rollno`.

**Ways to make a group:**

- On a block: open **Output as one field**, type the **Name of the joined field** and tick **Output as one field**. A validation rule is added at the same time (required, length = number of columns).
- Across blocks: Shift+click several blocks, then **Group as one field…**. In the dialog, set the name, reorder columns by dragging or with ↑ ↓, remove columns with ✕, add columns with **+ add a column…**, and keep **Also check it: required, no gaps, length = number of columns** ticked. Then **Create group**.
- In **Columns…**: tick columns and choose **Combine…**.

**What each column becomes** (one character per column):

| Option | Key | Default | What it does |
|---|---|---|---|
| **Empty column (no bubble marked)** | `groupOptions.<name>.empty` | space | What an unmarked column becomes. Keep a placeholder so later digits do not shift, or choose **skip the column (join the next one)**. Other choices: `_`, `-`, `*`, `?`, `0`, nothing, or **other character…**. |
| **Multi-marked column** | `groupOptions.<name>.multi` | `*` | Character used for a column with two or more marked bubbles. |
| **Column with an issue** | `groupOptions.<name>.issue` | `-` | Character used for a column with low confidence, a doubtful mark or a failed check. The value is still exported and the group goes to review. |

An example value is shown, e.g. `"12␣4*-"`. Templates made before placeholders existed use a **plain join**: an empty column simply disappears and a multi-marked column puts all its values in, so later digits shift. Click **Use one character per column** to switch; **Back to plain join** switches back.

**Primary key**: tick this on a group (or on a single-field block) to make it a column that identifies a sheet, such as the roll number. Sheets in a job with the same key are flagged as duplicates on the Results screen.

The **Grouped fields** list on the Page panel shows every group with its columns and placeholders, plus **Edit** (columns and order, placeholders and validation), **Rename**, **Delete** (the columns stay and are exported one by one) and, for a group whose columns no longer exist, **Fix** (drop the missing columns). A red banner at the top of the side panel lists such broken groups with **Fix (drop missing)** and **Delete group**.

### Output columns

#### Columns… dialog

Lists every column the template exports, in export order, with what it holds (e.g. "group · 12 col · digits", "bubble field · letters", "barcode zone", "check output").

| Button | What it does |
|---|---|
| **All** / **None** | Tick or untick every column. |
| **↑ Up** / **↓ Down** | Move the ticked columns together. This sets a custom export order. |
| **Combine…** | Join the ticked columns into one grouped field. Ticked groups are merged into the new one, which takes the place of the first column. Digits and letters can be combined; the result is text. |
| **Split** | Turn ticked groups back into separate columns. |

Zones and check outputs can be moved but not combined.

#### Output columns (under Options)

| Option | Key | Default | What it does |
|---|---|---|---|
| **Custom column order** | `outputColumns` | off | Off: every column, sorted by name. On: drag rows to reorder and untick a column to leave it out of exports. A left-out count is shown. |

### Zones: barcodes, QR codes, printed text, handwriting and images

Zones are rectangles read by something other than bubble detection. Choose the type in the toolbar before drawing, or change it in the zone panel.

| Type | Reads |
|---|---|
| **barcode** | 1-D barcodes (Code 128, Code 39 and 93, Codabar, EAN, UPC, ITF, DataBar) and 2-D codes such as PDF417 and Data Matrix. |
| **qrcode** | QR codes. |
| **ocr** | Printed text, e.g. a serial number. |
| **icr** | Handwritten characters written one per box. Needs a handwriting model (Learned models, next section); without one these zones always go to review. |
| **image** | Saves a picture of the area, e.g. the candidate's photo or signature. |

#### Options for every zone

| Option | Key | Default | What it does |
|---|---|---|---|
| **Name** | (zone name) | e.g. `barcode_1` | The output column name. Renaming also renames it in validation rules, checks and fallbacks. Must not match a field label. |
| **Type** | `type` | as drawn | barcode, qrcode, ocr, icr or image. |
| **x**, **y**, **Width**, **Height** | `origin`, `dimensions` | as drawn | Position and size in template pixels. Leave some margin around the text or barcode; a zone that clips the code fails to read. |
| **Pattern the value must match** | `pattern` | Any value | Values that do not match go to review. Presets: **Digits only**, **Letters only**, **Letters then digits**, **Letters and digits**, **Digits and spaces**, or **Custom…** (a regular expression such as `[0-9]{7}`). |
| **Min confidence** | `minConfidence` | 0.6 | Reads less confident than this (0–1) go to review. Raise for important IDs; lower if good reads keep going to review. |
| **Empty value** | `emptyValue` | nothing | Written when the zone reads nothing. |
| **Colour removal for this zone** | `colorDropout` | same as the page | Read this zone from a differently processed image: **none (plain grey)**, **red channel**, **green channel**, **blue channel** or **brightest channel**. Typical use: plain grey for a barcode on a red-dropout sheet. |
| **Validation** | `validate` | none | Value rules (next section). |

#### Barcode and QR code options

| Option | Key | Default | What it does |
|---|---|---|---|
| **Accepted formats (none ticked = all)** | `formats` | all | Only accept these code types. Ticking the right one avoids misreading other codes printed nearby. |
| **If the barcode fails, read this zone** | `fallbackZone` | nothing (no fallback) | (Barcode only.) When the barcode gives nothing, use this OCR or ICR zone instead, e.g. the printed digits under the barcode. Creates a check named after the barcode. |
| **Clean-up before comparing** | `fallbackNormalize` | trim spaces | (With a fallback.) How both values are cleaned before comparing: none, trim spaces, digits only, strip leading zeros, ignore case, letters and digits only. |
| **Send to review when the fallback is used** | `reviewOnFallback` | on | (With a fallback.) Sheets whose value came from the fallback zone also go to the review queue. |

**Barcode advanced** (per barcode zone):

| Option | Key | Default | What it does |
|---|---|---|---|
| **Decoders, in order** | `engines` | zxing, builtin, opencv, pyzbar | Decoders tried in order until one reads. Untick to skip one; ↑ moves one earlier. |
| **Use pyzbar (ZBar) when installed** | `pyzbar` | off | Adds the ZBar decoder; only used when it is installed. |
| **Code 39 check digit** | `code39Checksum` | off | Require and strip the Code 39 check digit. Only tick it if your codes carry one, or every read fails. |
| **Code 39 full ASCII** | `code39Extended` | auto | Decode Code 39 full-ASCII pairs (e.g. `+A` as `a`): auto, on or off. |
| **ITF check digit** | `itfChecksum` | off | Require the check digit on Interleaved 2 of 5 codes. |
| **ITF minimum length** | `itfMinLength` | default (6) | Shortest ITF code accepted; shorter partial reads are rejected. |

The template-wide **Barcode advanced** section under Options sets the same **Decoders, in order** and **Use pyzbar (ZBar) when installed** for every barcode zone, plus **Review reads by fallback decoders** (off): sends barcodes read by any decoder other than the first (zxing) to review.

#### OCR and ICR options

| Option | Key | Default | Applies to | What it does |
|---|---|---|---|---|
| **Allowed characters** | `whitelist` | any | OCR, ICR | Characters the reader may return, e.g. `0123456789` for a serial number. Strongly recommended for number fields. |
| **Character boxes** | `characterBoxes` | empty | ICR | Number of equal-width boxes, one handwritten character each. Must match the printed boxes. |
| **Layout (page segmentation mode)** | `psm` | One line (default) | OCR | What layout the text reader expects: **One line** (a single printed field), **One word** (a short code with no spaces), **Block of lines** (an address or name block), **Find text anywhere** (text whose position varies, e.g. digits under a pasted sticker), **Raw line, no layout checks** (a line that keeps being split wrongly). |
| **Read only when needed** | `lazy` | on when the zone is a barcode fallback | OCR, ICR used by a check or fallback | Skip this zone unless a check needs it. Saves time on every sheet. |
| **Text direction** | `direction` | Horizontal (normal) | OCR, ICR | Turn the crop before reading: **Vertical, reading bottom to top**, **Vertical, reading top to bottom**, **Upside down**, or **Auto: try all four (slower)**. Use a vertical setting for text printed sideways. |
| **Crop as read** | | | OCR, ICR | A preview of the zone turned the way the reader sees it. With a test scan as background it shows exactly what was read; with Auto it shows the direction the last test read chose. |
| **PaddleOCR model** | `paddleModel` | config default | OCR, ICR | **mobile (fast)** or **server (most accurate, slower)**. Options the server does not have are marked "(not installed)". |
| **PaddleOCR recogniser** | `paddleLang` | default | OCR, ICR | **English**, **Main (multilingual, handwriting)** or **Devanagari (Hindi)**. |
| **Language** | `lang` | config default (eng) | OCR | Tesseract language for this zone. Only languages installed on the server are listed; **eng+hin** reads English and Hindi. |
| **OCR engine** | `engine` | config default | OCR | **Tesseract** or **PaddleOCR** for this zone. |
| **Fallback engine** | `fallbackEngine` | config default | OCR | Second engine, tried when the first read is empty, below Min confidence or fails the pattern. The better valid read wins; if the two engines disagree the zone goes to review. **none** turns it off. |
| **Retry other layout modes** | `psmRetry` | config default (on) | OCR | If the read is empty or fails the pattern, try other layout modes and keep the best valid result. |
| **Expected format for Tesseract (user patterns)** | `userPatterns` | taken from the pattern | OCR | Hints for Tesseract, comma separated: `\d` digit, `\A` capital, `\a` small letter, `\*` repeat. Left empty, simple patterns such as `[0-9]{7}` are converted automatically. |
| **Min character confidence** | `minCharConfidence` | off | OCR | Send the zone to review when any single character is below this confidence (0–1), so one doubtful digit is checked by hand. |

If PaddleOCR is not available on the server, a note says so and zones set to it use Tesseract.

#### Image zone options (photo, signature)

| Option | Key | Default | What it does |
|---|---|---|---|
| **File name** | `saveFilename` | `{file}_{zone}.png` | Name of the saved crop. `{file}` is the sheet's file name, `{zone}` the zone name, `{page}` the PDF page. |
| **Longest side (px)** | `maxSide` | full size | Shrinks the saved crop so its longer side is at most this many pixels (minimum 16). |
| **Embed image in results (base64)** | `embedBase64` | off | Off: the crop is saved as a file and the result holds its name. On: a small copy is also stored inside the result record and API responses. Only small crops are embedded. |

### Test read

**Test read…** saves the template, reads one sample sheet you pick (image or PDF) and shows the result over the layout. The sheet is stored like any single scan, so it also appears on the Results screen.

- The background switches to **Last test scan**: the sheet as it looked after alignment. If the printed bubbles do not sit under the template's bubbles, the alignment or the block positions need work.
- Marked bubbles are filled green, read values are written beside each field, zones show `= value` (∅ when empty), and flagged fields are drawn red.
- The side panel shows a **Test read** box with the status, score (if there is an answer key), and the flagged items; click an item to select it on the page. **Clear test overlay** removes it.
- The overlay hides itself as soon as you change the layout, because it shows the saved layout.

### Checking a generated draft

A generated template opens with a report in the side panel:

| Part | What to do |
|---|---|
| **Needs verification (N)** | Items the generator was unsure about (blocks, labels, zones; every zone is listed). Click one to select it and centre it on the page, check it against the printed sheet, fix it, then click **Verified**. **Verify all** marks everything as checked. **Show verified (N)** lists the checked ones with **Undo** / **Undo all**. Unchecked items are drawn as orange dashed boxes. |
| **Warnings (N)** | Layout warnings. **Confirm** clears one after you have checked it; **Confirm all** clears all. Cleared warnings are kept under "N confirmed warning(s)" with **Show them again**. |
| **Information** | Notes needing no action, e.g. "scans are cropped to the paper … whole image used". |
| **Suggested blocks (N)** | Blocks found with weak evidence and left out. Click one to centre the page on it; **Add as block** adds it. |

Work through the whole list before scanning real batches.

### The JSON view

**Edit JSON** opens a full-screen editor with three tabs:

- **template.json**: the layout (page size, blocks, zones, alignment, groups). Required.
- **config.json**: reading options and models. Empty means the app's defaults.
- **evaluation.json**: answer key and marking scheme. Empty means no scoring.

It has line numbers and colouring, and shows problems as you type: JSON mistakes straight away with the line and column, then, a moment later, the app's own rules explained in plain words (for example "Unknown option … check the spelling, or remove it"). Click a problem to jump to its line. A tab with problems shows a count; a changed tab shows •.

| Button / key | What it does |
|---|---|
| **Format** (Ctrl+Shift+F) | Re-indents the current file. |
| **Download** | Saves the current file to your computer. |
| **Upload…** | Replaces the tab's text with a file from your computer (not used until you click Apply). |
| **Apply** (Ctrl+Enter or Ctrl+S) | Puts the changes into the editor as one undo step. Disabled while any file has problems. You still need to **Save** the template afterwards. |
| **Cancel** / **✕** (Esc) | Closes; asks first if you changed something. |

The **Advanced (JSON)** section at the bottom of Options offers small boxes for the same data: `preProcessors` (alignment and clean-up steps), `customLabels` (groups), `outputColumns`, `config.json` and `evaluation.json`. Use it for expert settings that have no form control.

### Keyboard and mouse shortcuts in the editor

They work while the Templates tab is open and you are not typing in a box (Ctrl+S works even while typing).

| Key / action | Does |
|---|---|
| Ctrl+S | Save |
| Ctrl+Z | Undo |
| Ctrl+Shift+Z or Ctrl+Y | Redo |
| Ctrl+D | Duplicate the selection |
| Del or Backspace | Delete the selection |
| Arrow keys | Move the selection 1 px (Shift: 10 px) |
| B / Z | Draw a block / a zone |
| Esc | Cancel drawing or a tool; otherwise clear the selection |
| Tab / Shift+Tab | Select the next / previous block or zone |
| F | Fit the page |
| + (or =) / − | Zoom in / out |
| Mouse wheel | Zoom at the mouse |
| Shift + wheel | Pan sideways |
| Drag empty space, right-drag, middle-drag or Space+drag | Pan |
| Drag an object / a handle | Move / resize |
| Shift+click | Add or remove a block from a multi-selection |
| Double-click | Select and jump to its first setting |
| Enter in a box | Apply the typed value |

### How to make a template for a new sheet

**From samples (quickest):**

1. Scan about 20 filled sheets of the form (plus one blank, if you have it).
2. Optionally prepare a labels spreadsheet with the file names and the true answers of those sheets.
3. On the Templates tab, under **Auto-generate from samples**, type a **Name**, choose the **Sample sheets** and the **Labels** file, and click **Generate draft**.
4. In the editor, work through **Needs verification**: click each item, compare with the printed sheet, fix it, click **Verified**. Check **Warnings** and **Suggested blocks** too.
5. Check alignment under **Options → Alignment**: the method should match the form, and **Marks matched per sample sheet** should show all marks found.
6. Group roll-number columns (**Output as one field**) and tick **Primary key** on the roll number.
7. **Save**, then **Test read…** a few sheets that were not among the samples.

**By hand:**

1. Upload a template that is close, or **Duplicate** an existing one; or upload a clean scan of the blank form as the background of a new template (**Upload background…**).
2. Set **Page width** / **Page height** and the default **Bubble width** / **Bubble height** on the Page panel.
3. Choose the **Alignment method** and set it up (tracks, index points or markers, see below).
4. Press B and drag a box from the top-left bubble to the bottom-right bubble of a block. Set **Field type** and **Field labels**, then drag the corner handles until every drawn bubble sits on a printed bubble. Fine-tune **Bubbles gap** and **Labels gap** if needed.
5. Use Ctrl+D for blocks that repeat; the labels continue automatically.
6. Add zones for barcodes, text or photos with Z.
7. **Save** and run **Test read…** on a real filled sheet. Look at the green marks and the read values.

### How to add timing tracks

1. Under **Options → Alignment**, choose **Timing tracks**.
2. Click **+ Add track** and drag a box over the whole strip of marks on one edge. Repeat for each track (usually left and right, sometimes top and bottom).
3. Check the numbers: every printed mark should have an orange rectangle. Use **Edit marks** to remove false marks or add missed ones.
4. Check **Mark width** / **Mark height** against the printed marks.
5. Click **Test on samples** (add samples first via **Preview block borders… → Add samples…** if the template has none). All marks should be found on every sheet.

### How to add index points

1. Make sure the Alignment method is **Timing tracks** (with or without tracks).
2. Click **+ Add index point**. Suggested marks are circled.
3. Click each printed mark you want to use, or drag a box around a small one. Spread them over the page, at least one in each corner area, and include one that has no twin at the opposite corner so upside-down sheets can be told apart.
4. Without tracks, add at least 4 (5 or more is safer). The template then aligns on them.
5. Click **Calibrate on samples** to settle each point's position and size, and look at the table for points that are often not found.
6. Untick **required** on any point that is sometimes covered or missing on good sheets.
7. **Save** and **Test on samples**.

### How to fix a block that reads off by a little

1. **Test read…** a sheet where the block reads wrongly, and keep **Last test scan** as the background.
2. Zoom in on the block (wheel or +). If **all** blocks are off by the same amount, the problem is alignment, not the block: check the tracks or index points and run **Test on samples**.
3. If only this block is off on every sheet, select it and nudge it with the arrow keys (1 px at a time; set **grid** to 1 px and untick **edges** for drag work). If the first rows fit but the last rows drift, adjust **Labels gap** (or **Bubbles gap** across the row) in small steps.
4. If the block is off by different amounts on different sheets and it has a printed box around it, set **Fit to printed border** to on (or click **Pick printed border…** and click the box). If it has no box, set **Fit to bubble outlines** to on.
5. Use **Preview block borders…** to check that the border is found (green) on the reference and on samples, and **Test on samples** to check all of them.
6. **Save** and **Test read…** again.

### How to read a roll number as one value

1. Select the roll-number block (field type QTYPE_INT, labels such as `roll1..10`).
2. Open **Output as one field**, type `rollno` as **Name of the joined field** and tick **Output as one field**.
3. Under **What each column of rollno becomes**, keep a space (or choose a character) for empty columns so digits do not shift, and keep `*` for multi-marked columns.
4. Tick **Primary key** so duplicate roll numbers in a job are flagged.
5. **Save**.

## 5. Template editor: reading options, review, checks, scoring, colours

This section covers the template editor's settings: how bubbles are judged as marked or empty, which doubtful answers go to the review queue, how values are checked, how sheets are scored, and how coloured print is removed. The layout itself (blocks, zones, alignment, groups) is described in the previous section.

Most of these settings are saved with the template. They take effect for every sheet read **after** you click **Save** in the editor. Sheets that were already read keep their old result until you regrade them on the Results screen (**Regrade…**).

A few words used throughout:

- **Fill share** (or fill ratio): how much of the inside of a bubble is dark, from 0 (completely empty) to 1 (completely filled in).
- **Confidence**: how sure the reader is about a bubble, from 0 (a coin toss) to 1 (certain).
- **Flag**: a short name attached to a field that says what looked wrong, for example `weak_mark`. Some flags send the field to the review queue.
- **config.json**: the template's tuning file. Every option in this section either has a control in the editor or is set by typing it into config.json (see [Settings that only exist in config.json](#settings-that-only-exist-in-configjson)). An empty config.json means "use the defaults".

### Where to find the settings

| Where in the editor | What is there |
|---|---|
| **Page** panel (shown when nothing is selected) | **Bubble threshold** (adaptive or fixed), **Colour removal (colorDropout)** with a **Colours…** button |
| **Page** panel › **Cross-field checks** | The list of checks, **+ Add check** |
| **Page** panel › **Options** › **Review thresholds** | Every review slider and the list of flags that send a field to review, with a fill histogram |
| **Page** panel › **Options** › **Learned models** | **Bubble model** and **Handwriting model** |
| **Page** panel › **Options** › **Fast pass** | The quick first read at reduced size and its two "clear-cut" limits |
| **Page** panel › **Options** › **PDF input** | **PDF render DPI** and **PDF pages** |
| **Page** panel › **Options** › **Barcode advanced** | Barcode decoders used by every barcode zone |
| **Page** panel › **Options** › **Advanced (JSON)** | The raw config.json and evaluation.json boxes |
| A selected block, zone or group | Its **Validation** section; a zone also has **Colour removal for this zone** |
| Toolbar **Colours…** | The Colours panel (colour removal with live preview) |
| Toolbar **Scoring…** | The Scoring panel (answer key and marking scheme) |
| Toolbar **Edit JSON** | template.json, config.json and evaluation.json in a full-screen editor |
| Toolbar **Test read…** | Saves the template, reads one sample sheet and overlays the result. Needed for the fill histogram |

Every option with an **i** icon has a help line. Click the icon, or click into the control, to see it under the control.

---

### Bubble threshold (Page panel)

The reader decides for each bubble whether it is marked. By default it works out the dividing line between "marked" and "empty" separately for each sheet and each row, so a light scan and a dark scan are both read correctly.

| Option | Key | Default | What it does |
|---|---|---|---|
| **Bubble threshold** | `threshold_params.mode` | **adaptive (per sheet and per row)** | *Adaptive* finds the marked/unmarked split on each sheet and each row. *Fixed intensity line* uses the same line on every sheet. |
| **Dark below (0–255)** (only with Fixed) | `threshold_params.fixed_threshold` | 120 | A pixel darker than this counts as pencil. 0 is black, 255 is white. |
| **Min filled share (0–1)** (only with Fixed) | `threshold_params.fixed_min_fill_ratio` | 0.12 | A bubble is marked when at least this share of its inside is darker than **Dark below**. |

**When to use Fixed.** Only when your printing and scanning are very consistent (the same scanner, the same settings, the same paper) and you want completely predictable results. With Fixed, the flag `ambiguous_threshold` is never raised; confidence is how far the bubble's fill is from the **Min filled share** line.

**What goes wrong if set badly.**
- **Dark below** too high (for example 200): grey paper, shadows and the printed bubble outline count as pencil, and empty bubbles read as marked.
- **Dark below** too low (for example 60): light pencil does not count, and real marks read as empty.
- **Min filled share** too low: stray dots and the printed letter inside the bubble mark it. Too high: half-filled bubbles are missed.

If you are unsure, stay with **adaptive**.

#### Even out shadows (flatten background)

| Key | Default | What it does |
|---|---|---|
| `threshold_params.flatten_background` | on (`true`) | Before bubbles are measured, the page is divided by a smooth estimate of the paper, so shadows, folds and uneven light do not shift the marked/empty line. |

There is no tick box for this in the editor; set it in config.json, or try it on one sheet first with **Regrade…** on the Results screen (option **Even out shadows**). Leave it on. Turn it off only if a regrade comparison clearly shows better reads without it, for example on very clean flatbed scans where it makes no difference anyway.

#### Threshold constants (config.json only)

These fine-tune the adaptive threshold. They are expert settings; change one at a time and compare on a few sample sheets with **Test read…** or a Regrade preview.

| Key | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| `threshold_params.MIN_JUMP` | 25 | 10–100 | The smallest jump in darkness between the lightest marked and the darkest empty bubble that counts as a real split. The Regrade dialog calls it **Mark sensitivity (lower counts fainter marks)**. | Lower (for example 18) for light pencil marks. Higher (for example 35) when smudges or erasures are read as marks. Too low: dirt and erasures count as marks. Too high: light marks are missed and more rows fall back to the page-wide line. |
| `threshold_params.CONFIDENT_SURPLUS` | 5 | 0–20 | Extra margin above `MIN_JUMP` before a row's own split is trusted. Below it, the row uses the page-wide line instead (or is flagged `ambiguous_threshold`). | Rarely. Higher makes the reader lean on the page-wide line more. |
| `threshold_params.MIN_GAP` | 30 | 10–100 | For fields with only one or two bubbles: when their darkness differs by less than this, the page-wide line decides. | Rarely; only for sheets with many one- or two-bubble fields (yes/no boxes). |
| `threshold_params.JUMP_DELTA` | 30 | 10–100 | Used to look for a second possible split on the page. It has almost no effect on results. | Leave as is. |
| `threshold_params.PAGE_TYPE_FOR_THRESHOLD` | `white` | `white`, `black` | The fallback line used when a page has no clear split: `white` for normal light paper. | Only for white-on-black sheets. |
| `threshold_params.GAMMA_LOW` | 0.7 | 0–1 | Darkening applied to the page only when the old `alignment_params.auto_align` option is on. | Leave as is. |

---

### Review thresholds (Options › Review thresholds)

The reader never just guesses quietly. Each field it is unsure about gets a flag, and flags you choose send the field to the **Review queue**. These settings decide how strict that is. Higher sensitivity means fewer silent mistakes but more work in the review queue.

At the top of the section is a **fill histogram**. After you run **Test read…** on a sample sheet, it shows how full every bubble on that sheet was: green bars for bubbles read as marked, grey bars for empty ones, along a scale from 0 (empty) to 1 (full). Two dashed lines show the current **weak mark** and **missed mark** settings. A good sheet has the grey bars bunched near the left, the green bars bunched near the right, and both lines in the gap between them. Until you run a test read, the section shows "Run "Test read…" on a sample sheet to see its bubble fill histogram here."

Each slider has a number box next to it; you can drag or type. The word *default* next to a slider means you have not changed it. Moving a slider back to its default value removes the setting from config.json.

| Option | Key | Default | What it does | When to change | If set badly |
|---|---|---|---|---|---|
| **Min confidence** | `review_params.min_confidence` | 0.35 | A field whose least certain bubble is below this confidence is flagged `low_confidence`. | Raise (0.45–0.5) for high-stakes exams where you want more checking. Lower (0.25) when the queue is full of fields that turn out correct. | Too high: a flood of reviews. Too low: doubtful reads are accepted without a look. |
| **Weak mark below** | `review_params.min_marked_fill_ratio` | 0.25 | A bubble read as marked but filled less than this is flagged `weak_mark` (half-filled bubble, a tick, a dot, or a template sitting slightly off). | Lower (0.15) if students habitually fill bubbles lightly and those are always right. Raise (0.35) to look at every partial fill. | Too high: every normal mark goes to review. Too low: ticks and dots are accepted as answers without review. |
| **Possible missed mark above** | `review_params.max_unmarked_fill_ratio` | 0.6 | A bubble read as empty but filled more than this is flagged `possible_missed_mark` (a real mark the reader did not count, or a bad erasure). | Lower (0.45) on sheets where students erase badly, so heavily erased bubbles are checked. | Too low: every erasure and printed letter goes to review. Too high (near 1): missed marks pass silently. |
| **Confidence margin** | `review_params.confidence_margin` | 20 (range 1–80) | How far (in darkness, 0–255) a bubble must be from the marked/empty line to count as fully confident. It changes how confidence is calculated, and so how **Min confidence** behaves. | Larger = stricter (more fields count as unsure). Change only together with **Min confidence**. | Too large: almost every field is `low_confidence`. Too small: everything counts as confident. |
| **Keep darker mark when ahead by** | `review_params.keep_darker_gap` | 0 (off) | When two or more bubbles in a field are marked and the darkest is filled at least this much more than the next one, only the darkest counts. The field gets the flag `kept_darker_mark`. | Only if your exam rules say a lightly erased first answer should be ignored when the student clearly darkened another. A typical value is 0.3. | Too small: a real double mark is quietly turned into a single answer. Many exam rules count every double mark as wrong; check your rules before using this. |
| **Strict double marks from** | `review_params.strict_double_mark` | 0 (off) | When exactly one bubble is marked, any other bubble in the field filled at least this much also counts as marked, so the field becomes a double mark (flags `faint_second_mark` and `multi_marked`). 0.30 suits pen dots and ticks. | When students make a second faint mark that the reader ignores, and your rules say that is a double mark. A field already settled by **Keep darker mark** is left alone. | Too low: printed letters, dust and erasures turn good answers into double marks. In testing on 800 real sheets, 0.30 caught 45 missed double marks but also sent 76 pen dots or ticks to review. |
| **Min marked bubbles per sheet** | `review_params.min_marked_bubbles` | 0 (off) | When a whole sheet has fewer marked bubbles than this, the sheet is flagged `too_few_marks` and sent to review. | When you use colour removal and some students may have used a pen of the same colour as the print (their marks disappear with it). Also catches blank or wrong-side scans. Set it a little below the fewest marks a real sheet can have (for example the roll number digits). | Too high: sheets from students who left many questions blank all go to review. |
| **Flags that send a field to review** | `review_params.review_flags` | see below | Tick the problems that put a field in the review queue. Unticked flags are still recorded and shown on the Results screen; they just do not create review work. | See the table below. | Unticking a flag means those problems are accepted without anyone looking. |

The review rules apply to every bubble field, roll numbers and other grouped fields included.

#### Flags you can tick

| Tick box (as shown) | Flag | Ticked by default | What it means |
|---|---|---|---|
| two or more bubbles marked | `multi_marked` | yes | More than one bubble is marked in a single-answer field. |
| nothing marked | `empty` | no | Nothing is marked. Blank answers are normal, so this is off; tick it only if every field must be answered (for example a roll number). |
| marked/unmarked split unclear | `ambiguous_threshold` | yes | The row had no clear gap between dark and light bubbles. |
| low confidence | `low_confidence` | yes | The least certain bubble is below **Min confidence**. |
| weak mark | `weak_mark` | yes | A marked bubble is filled less than **Weak mark below**. |
| possible missed mark | `possible_missed_mark` | yes | An empty bubble is filled more than **Possible missed mark above**. |
| bubble model disagrees | `model_disagrees` | yes | A learned bubble model (if you use one) reads the bubble differently. |
| printed border not found | `rectify_failed` | no | A block set to fit its printed border could not find the border. |
| printed border would move the block a lot | `border_slide` | yes | Fitting a block to its printed border would have moved it by almost a whole bubble; the read is kept but checked. |
| double mark settled by darker bubble | `kept_darker_mark` | no | **Keep darker mark when ahead by** turned a double mark into one answer. Tick it to check those decisions. |
| faint second mark | `faint_second_mark` | no | **Strict double marks from** turned a single mark into a double mark. The field is also `multi_marked`, so it already goes to review while that flag is ticked. |


Some sheet-level problems always go to review whatever you tick here: `poor_image` (see the image-quality gate below), `too_few_marks`, and alignment problems. Zone, validation and check flags (for example `not_found`, `pattern_mismatch`, `validation_failed`, `cross_check_failed`) follow their own settings, described further down and in the zones section.

#### Image-quality gate (config.json only)

Before a sheet is read, three quick measurements catch photos that cannot be read reliably. A sheet below any of the minimums gets the sheet flag `poor_image` and goes to review. The defaults are deliberately low, so only truly unreadable photos are caught.

| Key | Default | What is measured | When to change |
|---|---|---|---|
| `review_params.min_sharpness` | 10 | How sharp the page is (blur). | Raise slightly if blurred phone photos get through and are misread. |
| `review_params.min_contrast` | 40 | The difference between the darkest and lightest parts of the page (0–255). | Raise if washed-out photos get through; lower if faint but readable scans are flagged. |
| `review_params.min_bubble_px` | 5 | How many pixels wide a bubble is in the original photo. Too few pixels cannot be read. | Raise if far-away phone photos are misread. |

Set any of them to 0 to turn that measurement off. Set too high, many good sheets go to review as `poor_image`.

#### Registration check (config.json only)

| Key | Default | What it does |
|---|---|---|
| `review_params.min_grid_fit` | 0 (off) | Compares where the template puts the bubbles with the printed bubble outlines on the sheet. A sheet that matches worse than this (−1 to 1) is flagged `registration_suspect`. Off by default because it also flagged some good real sheets. Forms without printed bubble outlines must leave it off. |

---

### Learned models (Options › Learned models)

A learned model is an optional extra reader trained on your own scans. Without one, the classical reader described above is used, and that is the normal setup.

| Option | Key | Default | What it does |
|---|---|---|---|
| **Bubble model** | `ml_params.bubble_model_path` | none (classical reading) | A model that double-checks every bubble. The list shows the .onnx models uploaded with the template and those in the server's models folder ("(server)"). |
| **Handwriting model** | `ml_params.icr_model_path` | none | A model that reads handwritten characters in ICR zones. Without a trained model, ICR zones fall back to the built-in text reader (see `ocr_params.icr_engine` in the zones section) or go to review. |
| *(config.json only)* Bubble model role | `ml_params.bubble_model_role` | `second_opinion` | `second_opinion`: the classical reader decides; agreement raises confidence, and a disagreement sends the field to review (`model_disagrees`). `decide`: the model decides. |

If the server does not have the model runtime installed, the panel says "onnxruntime is not installed on this server: models are ignored." If no models are found it says so and tells you to upload one with the template files.

Keep the role on `second_opinion` unless measurements on your own labelled sheets show the model makes fewer silent mistakes. A poorly trained model set to `decide` can change correct answers without sending anything to review.

---

### PDF input (Options › PDF input)

These are the template's defaults for PDF files. The Scan and New Job screens can override them for each batch.

| Option | Key | Default | What it does | When to change |
|---|---|---|---|---|
| **PDF render DPI** | `pdf_params.pdf_dpi` | auto (the PDF's own resolution) | Resolution PDF pages are turned into images at. Choices: auto, 150, 200, 300, 400, 600. | Pick 200–300 when a PDF was made from vector print or when auto gives tiny images. Higher values are slower and use more memory. Too low (150 on small bubbles) makes marks hard to read. |
| **PDF pages** | `pdf_params.pdf_page` | 1 | Which pages of each PDF to read: `1`, `2-4`, `3-` (from page 3 to the end), `all`, or a list such as `1,3`. | When each PDF holds several sheets, use `all`. If you type something else, the editor says "Pages look like 1, 2-4, 3- or all". |

---

### Barcode advanced (Options › Barcode advanced)

Template-wide defaults for every barcode zone. A zone can override them in its own **Barcode advanced** section (see the zones section).

| Option | Key | Default | What it does |
|---|---|---|---|
| **Decoders, in order** | `barcode_params.engines` | zxing, builtin, opencv, pyzbar (all ticked) | The decoders tried one after another until one reads the code. Untick to skip one; use ↑ to try a decoder earlier. At least one must stay ticked ("Keep at least one decoder"). |
| **Use pyzbar (ZBar) when installed** | `barcode_params.pyzbar` | off | Adds the ZBar decoder. Only used when it is installed on the server. |
| **Review reads by fallback decoders** | `barcode_params.review_fallback_decodes` | off | Sends barcodes read by any decoder other than the first (zxing) to review. Turn on when a misread barcode would be costly and you want to see every code that was hard to read. |

---

### Settings that only exist in config.json

You set these in **Options › Advanced (JSON)** (the box "config.json (tuning, models; empty = defaults)") or with **Edit JSON**. Type only the settings you change; everything else keeps its default. If the text is not valid JSON, the editor refuses form changes with "config.json below is not valid JSON; fix it first".

Example config.json with a few changes:

```json
{
  "threshold_params": { "MIN_JUMP": 20 },
  "review_params": { "min_marked_bubbles": 10, "min_contrast": 30 },
  "ml_params": { "bubble_model_role": "decide" }
}
```

| Key | Default | What it does |
|---|---|---|
| `threshold_params.flatten_background` | on | Even out shadows (above). |
| `threshold_params.MIN_JUMP`, `CONFIDENT_SURPLUS`, `MIN_GAP`, `JUMP_DELTA`, `PAGE_TYPE_FOR_THRESHOLD`, `GAMMA_LOW` | 25, 5, 30, 30, white, 0.7 | Threshold constants (above). |
| `review_params.min_sharpness`, `min_contrast`, `min_bubble_px` | 10, 40, 5 | Image-quality gate (above). |
| `review_params.min_grid_fit` | 0 (off) | Registration check (above). |
| `ml_params.bubble_model_role` | `second_opinion` | Learned bubble model role (above). |
| `dimensions.processing_width`, `processing_height` | 666, 820 | Internal processing size. Leave as is. |
| `outputs.show_image_level`, `save_image_level`, `save_detections`, `filter_out_multimarked_files` | 0, 0, on, off | Debug images and old command-line output options. Leave as is. |

Alignment fine-tuning (`alignment_params`), OCR (`ocr_params`) and other zone settings are described in the layout and zones section.

The fast pass settings (`speed_params`) moved to their own panel: see "Fast pass" below.

---

### Fast pass (Options › Fast pass)

Reads each sheet at a smaller size first and keeps that quick read only when the sheet is clear-cut; every other sheet is read again at full size. The panel title shows "· on" when it is switched on.

| Option | Key | Default | What it does |
|---|---|---|---|
| **Fast first pass at reduced size** | `speed_params.fast_pass` | off | Switches the quick first read on. |
| **Fast pass size** | `speed_params.fast_scale` | Half size | Half size (checked on real batches) or Quarter size (fastest, but small bubbles often fail the check and are re-read anyway). |
| **Keep fast read when empty bubbles are at most** | `speed_params.fast_clear_empty` | 0.25 | A quick read is kept only if every empty bubble is filled at most this much. Lower = stricter. |
| **…and marked bubbles at least** | `speed_params.fast_clear_marked` | 0.6 | …and every marked bubble is filled at least this much, nothing needs review and every zone was read. Higher = stricter. |

**About the fast pass.** It only applies to templates aligned with timing tracks (with or without **Bend to fit the marks**: Exact and Smooth curve both work) and without the features it cannot check at the small size: fitting to printed borders, OCR, handwriting or image zones, a bubble model, the old auto-align option or the registration check. Those templates always read at full size. On 800 real sheets it kept 72% of quick reads with no answer differences and cut time per sheet by about a quarter; on 199 Jhansi sheets with Exact and with Smooth curve it kept 163 quick reads, all identical to a full read. It is called a test mode because it is off by default: try it on a batch you have already read, compare the exports, and only then use it for live work. If you loosen `fast_clear_empty` or `fast_clear_marked` (bring them closer together), more doubtful sheets are kept from the quick read and accuracy can suffer.

---

### Value validation

Validation checks that a value has the right shape: a roll number has 10 digits, a booklet code is A to D, a centre number is one of a known list. It works on any output: a bubble field, a grouped field such as a multi-column roll number, a zone, or the output of a check.

You find it as a **Validation** section in the side panel when you select a block ("Validation of each field" for a block with several fields), a zone, or a group ("Validation of <group name>"). The section title shows how many rules are set, for example "2 rules", or "none". When one rule applies to several fields at once, the panel says "Applies to each of the N fields"; if they had different rules before, editing sets the same rule on all.

| Option | Key | Default | What it does |
|---|---|---|---|
| **Required** | `required` | off | The value must not be blank. Without it, a blank value passes and no other rule is checked. |
| **Length** | `length` | any length | *any length*, *exactly* N characters, or *between* a minimum and maximum (either box may be left empty). |
| **Allow gaps** | `allowGaps` | on | Blank columns inside the value are allowed (for example `12 45`). Untick to flag a missing middle digit. |
| **Allow empty ends** | `allowEmptyEnds` | on | Blank columns at the start or end are allowed (for example a 4-digit roll number in 6 columns). Untick when every column must be filled. |
| **Leading zeros** | `leadingZeros` | keep (0123 is fine) | *forbid*: a value starting with 0 fails. |
| **Pattern** | `pattern` | Any value | The whole value must match. Presets: Any value, Digits only, Letters only, Letters then digits, Letters and digits, Digits and spaces. **Custom…** takes a regular expression, for example `[0-9]{7}`; an invalid one is refused with "That pattern is not a valid regular expression". |
| **Allowed values** | `allowed` | anything | The value must be one of these. Type a value and press Enter (several separated by commas); click × on a chip to remove it. |
| **Range** | `range` | none | Numeric minimum and maximum. The value is compared as a number but stays text, so leading zeros are kept. |
| **On fail** | `onFail` | send to review | What happens when a value fails: *send to review*, *blank the value*, *blank it and send to review*, or *only mark it in the results*. |
| **Remove all rules** (button) | | | Clears every rule of this field. |

A failing value gets the flag `validation_failed`, with the reason (for example "gap at position 2") shown in the review queue and on the Results screen. *Blank the value* writes the template's empty value instead (a zone's own empty value for zones).

Gap and empty-end checks look at each column of a grouped field, and each character box of a handwriting zone, so a blank middle digit is caught even if blanks are joined away in the output.

**Risks.** A rule that is too strict (wrong length, a pattern that does not allow a real format) sends good sheets to review or, with *blank the value*, wipes good values. Test it on a few sample sheets. Prefer *send to review* while you are setting a template up.

---

### Cross-field checks (Page panel › Cross-field checks)

A check compares several reads of the same value and decides what to output. Typical uses: a barcode with the printed digits under it as a fallback, a QR code that must match a barcode, or a handwritten number compared with its bubbled roll number.

The list shows each check's name, its output column, the fields in order of preference, the clean-up and the conflict rule, with **Edit** and **Delete** buttons. **+ Add check** opens the dialog "New cross-field check".

| Option | Key | Default | What it does |
|---|---|---|---|
| **Check name** | `name` | (required) | Shown in the review queue and on the Results screen. Must be unique ("A check named '…' exists"). |
| **Fields to compare** | `sources` | (at least one) | Tick every field, group, zone or other check output that holds the same value. Use the **filter…** box to find names. Each name shows its kind (group, field, column, barcode, ocr, check…). |
| **Which field wins when they differ** | order of `sources` | the order you ticked | Drag rows (or use ↑ ↓) to order them. The first field that has a value is used. |
| **Clean-up before comparing** | `normalize` | none | Applied to every value before comparing, and to the output: *none*, *trim spaces*, *digits only*, *strip leading zeros*, *ignore case*, *letters and digits only*. |
| **When values disagree** | `onConflict` | use the preferred field and send to review | *use the preferred field* (no review), *use the preferred field and send to review*, or *mark as error (blank, send to review)*. |
| **When the preferred field is missing** | `onMissing` | use the next field | *use the next field*, or *use the next field and send to review*. |
| **Also send to review when the fallback is used** | `reviewOnFallback` | off | Review every sheet whose value did not come from the first field. |
| **Ignore fields that failed their own validation** | `skipInvalid` | on | A field whose own validation failed is treated as missing. |
| **Ignore fields already flagged for review** | `skipFlagged` | off | A field that is already in the review queue is treated as missing. |
| **Output column name** | `output` | the check's name | The column that gets the check's value. It may be one of the compared fields, which is then replaced. |

Click **Save check** to keep it or **Cancel**.

A check adds the flags `cross_check_failed` (values disagree), `fallback_used` (the value came from a later field) or `all_sources_missing` (no field had a value; this goes to review by default). When a check settles a value without needing review, the review flags of the fields it compared are cleared, so you do not review the same value twice.

Checks can use other checks' outputs. A barcode zone's **If the barcode fails, read this zone** option creates a check named after the barcode automatically (see the zones section).

**Risks.** *use the preferred field* without review means a disagreement is resolved silently; use it only when the first field is known to be reliable. Choosing the wrong winner order puts the less reliable read in the output.

---

### Scoring (toolbar › Scoring…)

The Scoring panel holds the answer key and the marking scheme (saved as the template's evaluation.json). Without it, sheets are read but not scored. The panel opens full-screen with the questions and answer key on the left and the marking options on the right.

Changes are only a draft until you click **Apply** at the bottom; then click **Save** in the editor to keep them. **Ctrl+Z** in the editor undoes the whole scoring change in one step. **Cancel**, **✕** or **Esc** close the panel; if you changed something it asks "Close without applying the scoring changes?". Problems (for example a mark that is not a number) are listed at the bottom in red and **Apply** stays disabled until they are fixed. Before applying, the server also checks the answer key; if it refuses, the message appears at the bottom.

**Remove scoring** (top right) removes the answer key and marking scheme after a confirmation: sheets are then read but not scored.

#### Questions

| Option | Default | What it does |
|---|---|---|
| **Score the sheets** | ticked | Untick to keep the answer key but read sheets without scoring them (`grade: false`). |
| **Graded questions** | fields whose names start with q and a number, otherwise every field | The fields scored as questions, for example `q1..50`. Roll numbers and other fields stay out. Names that are not fields of the template are refused with a message. |
| **Add a block:** buttons | | One button per block; adds all that block's fields to the graded questions. |
| **Only grade these (question ranges)** | empty = all | An optional limit, for example `q1..50`. Useful when the key comes from a master sheet or a CSV that also holds other fields. |

You can write ranges as `q1..50` (or `q1...50`) and separate items with commas or spaces.

When the answer key lives in a separate CSV file, the panel says "Answers come from the file …" and offers **Load into the grid** to edit those answers here.

#### Answer key

| Control | What it does |
|---|---|
| Paste box and **Fill** | Type or paste answers in question order, starting at the first ticked question (or the first question). One letter per question (`ACBDDA…`), or separated by spaces or commas (`A, B, AB, C`). `A|B` accepts either answer; `-` (or `_`, `?`, `.`, `*`) leaves a question unchanged. Extra answers are ignored with a message. |
| **Import CSV / Excel…** | Reads a .csv, .tsv, .txt, .xlsx or .xlsm file: rows of *question,answer*, a header row of question names with answers below, or one row or column of answers in order. In a file, `A,B` in one cell means either answer. |
| **Scan a master sheet…** | Reads a sheet (image or PDF) filled in with the correct answers, using the saved layout, and puts the answers in the grid. It reports how many were set, how many were blank, and which questions to check because the reader was unsure. If the editor has unsaved changes it warns that the last saved layout is used. |
| **Clear answers** | Empties the whole grid (after a confirmation). |

The grid has one row per graded question:

| Column | What it does |
|---|---|
| (tick box) | Selects the row. **Shift**-click selects a range. Ticked rows are used by **Fill**, by ordered imports and by **+ Section from ticked rows**. |
| **Q** | The question name. |
| **Correct answer** | Click an option to make it the answer; click it again to clear. **Ctrl**-click (**Cmd**-click on a Mac) to accept several answers ("any of A, C"). A question without an answer shows "no answer" and is not scored. |
| **Type** | *Key*: one or several correct options. *Weighted*: you type marks per option, for example `A=1, B=2, AB=3`; fractions such as `3/2` are allowed. |
| **Drop** | 0 marks for everyone, and not counted in the maximum score (for example a misprinted question). |
| **Bonus** | Full marks for everyone, whatever they marked. |
| **Section** | The section the question belongs to (set under **Sections**). |

#### Marking scheme

| Option | Default | What it does |
|---|---|---|
| **Correct** / **Wrong** / **Unmarked** | +4 / −1 / 0 for a new answer key | Marks for a correct, a wrong and an unmarked answer. Negative marks and fractions are allowed, for example `-1/4`. Anything that is not a number or fraction is refused. |
| **Presets:** | | One-click schemes: **+4 / −1 / 0**, **+1 / −1/4 / 0**, **+1 / −1/3 / 0**, **+1 / 0 / 0**. |

#### Sections

A section is a range of questions with its own marking scheme. Per-section scores appear on the Results screen and as export columns.

| Option | What it does |
|---|---|
| **Name** | Section name (spaces become underscores). Must be unique. |
| **Questions** | The questions in it, for example `q1..10`. A question can belong to only one section. |
| **Correct** / **Wrong** / **Unmarked** | Marks for this section. |
| **Bonus** | Full marks to everyone for every question in the section (Wrong and Unmarked are then greyed out). |
| **✕** | Removes the section. |
| **+ Section from ticked rows** | Makes a section from the ticked rows of the answer key, with the current main scheme. |
| **+ Empty section** | Adds a section to fill in by hand. |

Questions in no section use the main marking scheme and appear as "Other questions" in score summaries.

#### Special questions

| Option | Default | What it does |
|---|---|---|
| **Score multi-marked answers** | Use the answer key (weights, else wrong) | What happens when a student marks more than one bubble (for example AB for a one-letter answer). *Use the answer key*: weighted marks if AB is listed for that question, otherwise wrong. *Score as wrong*, *Score as unmarked*, or *By the weighted table only*. |
| **Dropped questions** | none | Same as the Drop column, as a list. |
| **Bonus for everyone** | none | Same as the Bonus column, as a list. |

#### Score legend

Optional bands such as Fail below 40, Pass from 40, Distinction from 75. Add one with **+ Band**, then fill **From score** and **Label**; **✕** removes it. The band appears next to the score on the Results screen and in exports.

#### Preview

**Score a sample sheet…** reads a sample sheet with the saved layout and scores it with your draft, before you run a batch; nothing is stored. It shows the score, the band, counts of correct, incorrect, unmarked, bonus and dropped answers, per-section scores, and a table of every question with what was marked, the answer, the verdict and the marks. **Score again** rescores the same sheet after you change the draft; **Another sheet…** loads a different one.

---

### Colours (toolbar › Colours…)

Many answer sheets are printed in a light colour (pink, red, orange, light blue) so the print can be removed and only the students' marks remain. Colour removal (`colorDropout` in the template) says how a colour scan is turned into the grey image the reader uses. Without it, sheets are read in plain grey. The **Page** panel shows the current setting under **Colour removal (colorDropout)** ("grey (off)" when there is none) with a **Colours…** button.

#### The Colours panel

1. **Sample sheet**: choose a colour scan of a filled sheet (image or PDF). The **Before** view shows it (for a PDF only the preview is shown).
2. **Main colours** lists the colours found, with their hex code, share of the sheet and a guess of what they are, plus the paper's share. Next to each print colour is **Remove**, which applies a suggested setting that removes that colour while black, pencil and blue pen stay dark. **Remove (weak)** means it is the best available but the contrast to pens is weak: check the marks in the preview.
3. Or click the **Before** view to pick a colour with the eyedropper. Moving the mouse over it shows the colour under the pointer. After a click, a line explains how light the picked colour becomes (255 is white) and how dark pens stay.
4. Adjust the settings under **Colour removal** and compare with **After: what the reader sees**. The preview updates a moment after each change.
5. Click **Use for template**, then **Save** in the editor. **Close** (or **Esc**, or a click outside the panel) leaves without changing the template. The line "Template now:" shows the setting currently in the template.

| Option | Default | What it does |
|---|---|---|
| **Mode** | Grey (no colour removal) | *Grey*: no colour removal. *Red channel*: removes red, pink and orange print; black, pencil and blue pen stay dark. *Green channel*. *Blue channel*: removes blue and cyan print. *Brightest channel*: removes every strong colour, **blue pen too**. *One colour*: removes colours close to the one you pick. |
| **Colour to remove** (One colour only) | #E8618C | Pick with the colour picker or type a hex code like `#E8618C`. |
| **Tolerance** (One colour only) | 60 (5–150) | How different a colour may be from the picked one and still be removed. Higher removes more shades. |
| **Strength** (any mode except Grey) | 1 (0–1) | Blends plain grey (0) with the full removal (1). |

**What goes wrong if set badly.**
- Students who used a pen of the same colour as the print lose their marks with it. Use **Min marked bubbles per sheet** in Review thresholds to catch these sheets.
- *Brightest channel* removes blue pen as well as print; do not use it if students may answer in blue pen.
- *One colour* with a tolerance that is too high starts removing pen and pencil; too low leaves part of the print, which can then read as marks.
- Removing the print also removes printed timing marks or borders in that colour, which can break alignment. Check the **After** view.

#### Colour removal for one zone

A selected zone has **Colour removal for this zone**: *same as the page*, *none (plain grey)*, *red channel*, *green channel*, *blue channel*, *brightest channel*. Use it for a barcode or text printed in the dropout colour, for example plain grey for a red barcode on a red-dropout sheet, so the zone can still be read.

---

### How to …

#### How to read sheets with light pencil marks

1. Open the template and run **Test read…** on a typical light sheet.
2. Open **Options › Review thresholds** and look at the histogram. If many green (marked) bars sit near the weak-mark line, students mark lightly.
3. Lower **Weak mark below** a little (for example from 0.25 to 0.18) so normal light marks are not all sent to review.
4. If light marks are read as empty (the grey bars reach far to the right, or the Results show missed answers), lower the mark sensitivity: in config.json set `"threshold_params": {"MIN_JUMP": 18}`. Try it first on a few sheets with **Regrade…** on the Results screen, option **Mark sensitivity**, and **Preview**.
5. Keep **Even out shadows** on.
6. **Save**, then regrade the affected job.

#### How to handle students who erase badly

1. Lower **Possible missed mark above** (for example from 0.6 to 0.45) so heavily erased bubbles that still look dark are sent to review as `possible_missed_mark`.
2. Make sure **possible missed mark** and **two or more bubbles marked** are ticked in **Flags that send a field to review**.
3. If your rules say a clearly darker answer wins over a light erasure, set **Keep darker mark when ahead by** to about 0.3 and tick **double mark settled by darker bubble** so you can check those decisions at first.
4. If erasures are being read as marks, raise the mark sensitivity slightly (`MIN_JUMP` 30–35) and compare with a Regrade preview.

#### How to catch double marks

1. Keep **two or more bubbles marked** ticked (it is by default).
2. If students make a faint second mark that the reader ignores, set **Strict double marks from** to 0.30.
3. Run **Test read…** on a few sheets and check the queue size. If dots and ticks flood the review queue, raise it to 0.35–0.4.
4. In **Scoring… › Special questions**, choose how a double mark is scored in **Score multi-marked answers** (for example **Score as wrong**).

#### How to set up pink or red printed sheets

1. Click **Colours…** in the editor toolbar.
2. Load a filled sample sheet in colour under **Sample sheet**.
3. In **Main colours**, click **Remove** next to the pink or red print colour (or click the print in the **Before** view). Usually this picks *Red channel*.
4. Check the **After** view: the print should be almost gone, pencil and pen marks dark. If some print remains, try *One colour* and raise **Tolerance**.
5. Click **Use for template**, then **Save**.
6. If a barcode or printed number is in the same colour, select that zone and set **Colour removal for this zone** to *none (plain grey)*.
7. In **Review thresholds**, set **Min marked bubbles per sheet** to a small number (for example the number of roll number digits) to catch students who used a pen of the print colour.
8. Run **Test read…** on a sample sheet to confirm.

#### How to set up negative marking

1. Click **Scoring…**.
2. Under **Questions**, set **Graded questions** (for example `q1..100`), or use the **Add a block:** buttons.
3. Fill the answer key: paste the answers into the box and click **Fill**, use **Import CSV / Excel…**, or **Scan a master sheet…**. Check any question marked "no answer".
4. Under **Marking scheme**, click a preset such as **+4 / −1 / 0** or **+1 / −1/4 / 0**, or type your own (fractions like `-1/3` are fine).
5. If parts of the paper are marked differently, tick their rows in the grid (Shift-click for a range) and click **+ Section from ticked rows**, then set that section's marks.
6. Mark misprinted questions with **Drop** or **Bonus**, and choose **Score multi-marked answers**.
7. Click **Score a sample sheet…** and check the total and the per-question table.
8. Click **Apply**, then **Save** in the editor. Regrade any job read before the change.

#### How to make sure a roll number is complete and valid

1. Select the roll number group (or block).
2. In its **Validation** section, tick **Required**, set **Length** to *exactly* the number of digits, untick **Allow gaps** and **Allow empty ends**, and set **Pattern** to *Digits only*.
3. Leave **On fail** at *send to review*.
4. Run **Test read…** on a sheet with a missing digit to see the `validation_failed` flag.

#### How to use the printed number when a barcode does not read

1. Draw an OCR zone over the printed number under the barcode.
2. Select the barcode zone and choose the OCR zone in **If the barcode fails, read this zone** (this creates a check for you).
3. Or create the check by hand with **+ Add check**: tick the barcode zone and the OCR zone, put the barcode first, choose **Clean-up before comparing** *digits only*, and **When values disagree** *use the preferred field and send to review*.
4. **Save check**, then **Save**.

### Keyboard and mouse shortcuts in these panels

| Where | Key | What it does |
|---|---|---|
| Scoring panel | **Esc** | Close (asks first if there are unapplied changes) |
| Scoring answer grid | **Ctrl**-click (Cmd-click) an option | Accept several correct answers |
| Scoring answer grid | **Shift**-click a row's tick box | Select a range of rows |
| Editor, after Apply in Scoring | **Ctrl+Z** | Undo the whole scoring change |
| Colours panel | **Esc** | Close without changing the template |
| Validation › Allowed values | **Enter** | Add the typed value(s) |
